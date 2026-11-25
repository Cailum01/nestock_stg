[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$Project = 'nestocke-stg'
$Region = 'us-central1'
$Service = 'nestock-stg-api'
$GitHubRepository = 'Cailum01/nestock_stg'
$GoogleClientIdVariable = 'NESTOCK_GOOGLE_CLIENT_ID'
$RetiredAzureHost = 'nestock-api.ambitiousgrass-c37169f5.eastasia.azurecontainerapps.io'

function Invoke-QuietNative {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CommandName,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    $output = @(& $CommandName @Arguments 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }

    return ($output -join [Environment]::NewLine).Trim()
}

function Invoke-Gcloud {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $output = @(& gcloud @Arguments 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud failed (exit code $LASTEXITCODE). Check gcloud installation and authentication."
    }

    return ($output -join [Environment]::NewLine).Trim()
}

function Test-ApkContainsText {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ApkPath,
        [Parameter(Mandatory = $true)]
        [string]$Text
    )

    $archive = [System.IO.Compression.ZipFile]::OpenRead($ApkPath)
    try {
        foreach ($entry in $archive.Entries) {
            if ($entry.Length -eq 0) {
                continue
            }

            $entryStream = $entry.Open()
            $memoryStream = New-Object System.IO.MemoryStream
            try {
                $entryStream.CopyTo($memoryStream)
                $bytes = $memoryStream.ToArray()
            }
            finally {
                $memoryStream.Dispose()
                $entryStream.Dispose()
            }

            $utf8 = [System.Text.Encoding]::UTF8.GetString($bytes)
            if ($utf8.IndexOf($Text, [System.StringComparison]::Ordinal) -ge 0) {
                return $true
            }

            $utf16 = [System.Text.Encoding]::Unicode.GetString($bytes)
            if ($utf16.IndexOf($Text, [System.StringComparison]::Ordinal) -ge 0) {
                return $true
            }
        }

        return $false
    }
    finally {
        $archive.Dispose()
    }
}

function ConvertTo-StgReleaseVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version,
        [Parameter(Mandatory = $true)]
        [string]$Source
    )

    if ($Version -notmatch '^(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)\+(?<build>\d+)$') {
        throw "$Source must use major.minor.patch+build format."
    }

    try {
        return [pscustomobject]@{
            Text  = $Version
            Major = [long]$Matches.major
            Minor = [long]$Matches.minor
            Patch = [long]$Matches.patch
            Build = [long]$Matches.build
        }
    }
    catch {
        throw "$Source contains a version/build number outside the supported numeric range."
    }
}

function Get-ReleaseGateBlockers {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CandidateVersion
    )

    $blockers = @()
    $script:StgReleaseSummary = 'Latest published stable STG release could not be verified.'

    try {
        $branch = Invoke-QuietNative -CommandName 'git' -Arguments @(
            '-C', $RepositoryRoot, 'branch', '--show-current'
        ) -FailureMessage 'Could not read the current Git branch.'
        if ($branch -ne 'stg') {
            $blockers += "Checkout must be on branch 'stg' (current branch: '$branch')."
        }

        $status = Invoke-QuietNative -CommandName 'git' -Arguments @(
            '-C', $RepositoryRoot, 'status', '--porcelain=v1', '--untracked-files=all'
        ) -FailureMessage 'Could not verify Git working-tree cleanliness.'
        if (-not [string]::IsNullOrWhiteSpace($status)) {
            $changedPathCount = @(
                $status -split '\r?\n' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
            ).Count
            $blockers += "Git working tree is not clean ($changedPathCount changed, deleted, or untracked path(s))."
        }

        $originUrl = Invoke-QuietNative -CommandName 'git' -Arguments @(
            '-C', $RepositoryRoot, 'remote', 'get-url', 'origin'
        ) -FailureMessage 'Could not read the origin remote.'
        if ($originUrl -notmatch 'github\.com[:/]Cailum01/nestock_stg(?:\.git)?$') {
            $blockers += "origin must point to GitHub repository $GitHubRepository."
        }

        $head = Invoke-QuietNative -CommandName 'git' -Arguments @(
            '-C', $RepositoryRoot, 'rev-parse', '--verify', 'HEAD'
        ) -FailureMessage 'Could not read the current commit.'
        $trackingHead = Invoke-QuietNative -CommandName 'git' -Arguments @(
            '-C', $RepositoryRoot, 'rev-parse', '--verify', 'refs/remotes/origin/stg'
        ) -FailureMessage 'Local refs/remotes/origin/stg is missing.'
        $remoteStg = Invoke-QuietNative -CommandName 'git' -Arguments @(
            '-C', $RepositoryRoot, 'ls-remote', '--exit-code', 'origin', 'refs/heads/stg'
        ) -FailureMessage 'Could not verify the current remote origin/stg head.'
        $remoteHead = ($remoteStg -split '\s+')[0]

        if ($head -ne $trackingHead) {
            $blockers += 'HEAD does not match the local origin/stg tracking ref.'
        }
        if ($head -ne $remoteHead) {
            $blockers += 'HEAD is not the current remote origin/stg commit.'
        }
        if ($trackingHead -ne $remoteHead) {
            $blockers += 'The local origin/stg tracking ref is stale.'
        }
    }
    catch {
        $blockers += "Git release-source verification failed: $($_.Exception.Message)"
    }

    try {
        $releaseJson = Invoke-QuietNative -CommandName 'gh' -Arguments @(
            'release', 'list',
            '--repo', $GitHubRepository,
            '--limit', '1000',
            '--json', 'tagName,publishedAt,isDraft,isPrerelease'
        ) -FailureMessage 'Could not query GitHub STG releases with gh.'
        $releases = @($releaseJson | ConvertFrom-Json)
        $latestRelease = @(
            $releases |
                Where-Object {
                    -not $_.isDraft -and
                    -not $_.isPrerelease -and
                    $_.tagName -match '^stg-v\d+\.\d+\.\d+$'
                } |
                Sort-Object publishedAt -Descending
        ) | Select-Object -First 1

        if ($null -eq $latestRelease) {
            throw 'No published stable stg-vMAJOR.MINOR.PATCH release was found.'
        }

        $script:StgReleaseSummary = "Latest stable STG release: $($latestRelease.tagName)."
        $releasePubspecEncoded = Invoke-QuietNative -CommandName 'gh' -Arguments @(
            'api',
            "repos/$GitHubRepository/contents/frontend/pubspec.yaml?ref=$($latestRelease.tagName)",
            '--jq', '.content'
        ) -FailureMessage 'Could not read pubspec.yaml at the latest STG release tag.'
        $releasePubspecBytes = [System.Convert]::FromBase64String(
            ($releasePubspecEncoded -replace '\s', '')
        )
        $releasePubspec = [System.Text.Encoding]::UTF8.GetString($releasePubspecBytes)
        if ($releasePubspec -notmatch '(?m)^version:\s*([^\s#]+)') {
            throw 'The latest STG release tag has no readable Flutter version.'
        }
        $releaseVersionText = $Matches[1].Trim("'`"")
        $candidate = ConvertTo-StgReleaseVersion -Version $CandidateVersion -Source 'Current pubspec.yaml version'
        $released = ConvertTo-StgReleaseVersion -Version $releaseVersionText -Source 'Latest STG release pubspec version'
        $script:StgReleaseSummary += " Source version: $releaseVersionText."

        $versionComparison = 0
        foreach ($part in @('Major', 'Minor', 'Patch')) {
            if ($candidate.$part -gt $released.$part) {
                $versionComparison = 1
                break
            }
            if ($candidate.$part -lt $released.$part) {
                $versionComparison = -1
                break
            }
        }
        if ($versionComparison -le 0) {
            $blockers += "Current app version $CandidateVersion must be newer than released version $releaseVersionText."
        }
        if ($candidate.Build -le $released.Build) {
            $blockers += "Current Android build number ($($candidate.Build)) must exceed released build number ($($released.Build))."
        }
    }
    catch {
        $blockers += "GitHub release-version verification failed: $($_.Exception.Message)"
    }

    return $blockers
}

$gcloudCommand = Get-Command gcloud -ErrorAction SilentlyContinue
if ($null -eq $gcloudCommand) {
    throw 'gcloud is not available on PATH.'
}

$flutterCommand = Get-Command flutter -ErrorAction SilentlyContinue
if ($null -eq $flutterCommand) {
    throw 'Flutter is not available on PATH.'
}

$activeProject = Invoke-Gcloud -Arguments @('config', 'get-value', 'project')
if ($activeProject -ne $Project) {
    throw "Active gcloud project must be '$Project' (current project: '$activeProject')."
}

$serviceJson = Invoke-Gcloud -Arguments @(
    'run', 'services', 'describe', $Service,
    "--region=$Region",
    "--project=$Project",
    '--format=json'
)
try {
    $serviceConfig = $serviceJson | ConvertFrom-Json
}
catch {
    throw 'Could not parse the Cloud Run service configuration.'
}

$apiBaseUrl = [string]$serviceConfig.status.url
if ([string]::IsNullOrWhiteSpace($apiBaseUrl)) {
    throw 'Cloud Run did not return a service URL.'
}

$apiUri = $null
if (-not [System.Uri]::TryCreate($apiBaseUrl, [System.UriKind]::Absolute, [ref]$apiUri) -or
    $apiUri.Scheme -ne 'https' -or
    -not $apiUri.Host.EndsWith('.run.app', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'The configured Cloud Run endpoint is not an approved HTTPS run.app URL.'
}
$apiBaseUrl = $apiUri.GetLeftPart([System.UriPartial]::Authority).TrimEnd('/')

$containers = @($serviceConfig.spec.template.spec.containers)
if ($containers.Count -eq 0) {
    throw 'Cloud Run service has no configured container.'
}
$clientIdEntries = @(
    $containers[0].env | Where-Object { $_.name -eq $GoogleClientIdVariable }
)
if ($clientIdEntries.Count -eq 0 -or
    [string]::IsNullOrWhiteSpace([string]$clientIdEntries[0].value)) {
    throw "Cloud Run service is missing a non-empty $GoogleClientIdVariable value."
}
$googleClientId = ([string]$clientIdEntries[0].value).Trim()

try {
    $openApi = Invoke-RestMethod -Method Get -Uri "$apiBaseUrl/openapi.json" -TimeoutSec 20
}
catch {
    throw 'Could not fetch live STG /openapi.json.'
}
if ($null -eq $openApi.paths -or
    $openApi.paths.PSObject.Properties.Name -notcontains '/identity/google') {
    throw 'Live STG OpenAPI does not expose /identity/google.'
}

$frontendPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'frontend'
$pubspecPath = Join-Path $frontendPath 'pubspec.yaml'
if (-not (Test-Path -LiteralPath $pubspecPath -PathType Leaf)) {
    throw "Flutter project manifest not found: $pubspecPath"
}
$pubspec = Get-Content -LiteralPath $pubspecPath -Raw
if ($pubspec -notmatch '(?m)^version:\s*([^\s#]+)') {
    throw 'Could not read the Flutter app version from pubspec.yaml.'
}
$appVersion = $Matches[1].Trim("'`"")
$apkPath = Join-Path $frontendPath 'build\app\outputs\flutter-apk\app-release.apk'
$releaseGateBlockers = @(Get-ReleaseGateBlockers -CandidateVersion $appVersion)

$flutterVersionOutput = @(& flutter --version 2>$null)
if ($LASTEXITCODE -ne 0) {
    throw 'Flutter is on PATH but `flutter --version` failed.'
}

Write-Host "STG API: $apiBaseUrl"
Write-Host "Flutter app version: $appVersion"
Write-Host "Release APK output: $apkPath"
Write-Host $script:StgReleaseSummary

if ($releaseGateBlockers.Count -eq 0) {
    Write-Host 'Release gate: PASS (clean current stg checkout; version and build exceed the latest STG release).'
}
else {
    Write-Host 'Release gate: BLOCKED'
    foreach ($blocker in $releaseGateBlockers) {
        Write-Host "- $blocker"
    }
}

if ($DryRun) {
    if ($releaseGateBlockers.Count -gt 0) {
        Write-Host 'DryRun blocked: preflight checks completed, but no APK was built.'
        throw 'DryRun blocked by the release gate; no APK was built.'
    }
    Write-Host 'DryRun passed: release gate, gcloud, live OpenAPI, and Flutter checks succeeded. No APK was built.'
    return
}

if ($releaseGateBlockers.Count -gt 0) {
    throw 'Release gate is blocked; no APK build was started.'
}

$flutterArguments = @(
    'build', 'apk', '--release',
    "--dart-define=API_BASE_URL=$apiBaseUrl",
    "--dart-define=MEDIA_API_BASE=$apiBaseUrl",
    "--dart-define=$GoogleClientIdVariable=$googleClientId",
    '--dart-define=AGENT_TRANSPORT=http'
)
$null = New-Item -ItemType Directory -Path (Split-Path -Parent $apkPath) -Force
$previousApkPath = $null
$keepPreviousApkBackup = $false
if (Test-Path -LiteralPath $apkPath -PathType Leaf) {
    $previousApkPath = "$apkPath.previous-$([guid]::NewGuid().ToString('N'))"
    Copy-Item -LiteralPath $apkPath -Destination $previousApkPath
}

try {
    $null = Push-Location $frontendPath
    try {
        $buildOutput = @(& flutter @flutterArguments 2>&1)
        $buildExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($buildExitCode -ne 0) {
        throw "Flutter release build failed (exit code $buildExitCode). See captured Flutter output above."
    }
    if (-not (Test-Path -LiteralPath $apkPath -PathType Leaf)) {
        throw "Flutter reported success but the expected APK was not created: $apkPath"
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (-not (Test-ApkContainsText -ApkPath $apkPath -Text $apiBaseUrl)) {
        throw 'APK verification failed: the GCP API URL was not found in the built APK.'
    }
    if (-not (Test-ApkContainsText -ApkPath $apkPath -Text $googleClientId)) {
        throw 'APK verification failed: the Google client ID was not found in the built APK.'
    }
    if (Test-ApkContainsText -ApkPath $apkPath -Text $RetiredAzureHost) {
        throw 'APK verification failed: the retired Azure API host is embedded in the built APK.'
    }
}
catch {
    $buildFailure = $_
    if ($null -ne $buildOutput -and $buildOutput.Count -gt 0) {
        Write-Host 'Captured Flutter output (Google client ID redacted):'
        foreach ($line in $buildOutput) {
            $outputLine = [string]$line
            if (-not [string]::IsNullOrEmpty($googleClientId)) {
                $outputLine = $outputLine.Replace($googleClientId, '[REDACTED]')
            }
            Write-Host $outputLine
        }
    }

    if ($null -ne $previousApkPath -and (Test-Path -LiteralPath $previousApkPath -PathType Leaf)) {
        try {
            [System.IO.File]::Copy($previousApkPath, $apkPath, $true)
            Write-Warning 'Build failed; the previous APK was restored.'
        }
        catch {
            $keepPreviousApkBackup = $true
            throw "Build failed and the previous APK could not be restored. Backup preserved at '$previousApkPath'."
        }
    }
    elseif (Test-Path -LiteralPath $apkPath -PathType Leaf) {
        Remove-Item -LiteralPath $apkPath -Force -ErrorAction SilentlyContinue
    }
    throw $buildFailure
}
finally {
    if ($null -ne $previousApkPath -and -not $keepPreviousApkBackup -and
        (Test-Path -LiteralPath $previousApkPath -PathType Leaf)) {
        Remove-Item -LiteralPath $previousApkPath -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Release APK built and verified: $apkPath"
Write-Host "Version: $appVersion"
Write-Host 'Verified: GCP API URL and Google OAuth client ID are embedded; retired Azure host is absent.'
