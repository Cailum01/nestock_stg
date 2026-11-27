[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Project = 'nestocke-stg'
$Region = 'us-central1'
$Service = 'nestock-stg-api'
$GoogleClientIdVariable = 'NESTOCK_GOOGLE_CLIENT_ID'
$FrontendDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) 'frontend'
$ApkPath = Join-Path $FrontendDirectory 'build/app/outputs/flutter-apk/app-debug.apk'

function Test-ByteSequence {
    param(
        [byte[]]$Haystack,
        [byte[]]$Needle
    )

    if ($Needle.Length -eq 0 -or $Haystack.Length -lt $Needle.Length) {
        return $false
    }

    for ($start = 0; $start -le $Haystack.Length - $Needle.Length; $start++) {
        $matches = $true
        for ($offset = 0; $offset -lt $Needle.Length; $offset++) {
            if ($Haystack[$start + $offset] -ne $Needle[$offset]) {
                $matches = $false
                break
            }
        }
        if ($matches) {
            return $true
        }
    }
    return $false
}

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw 'Google Cloud CLI (gcloud) is required to build the STG debug APK.'
}
if (-not (Get-Command flutter -ErrorAction SilentlyContinue)) {
    throw 'Flutter is required to build the STG debug APK.'
}

$serviceJson = & gcloud run services describe $Service `
    --project=$Project --region=$Region --format=json 2>$null
if ($LASTEXITCODE -ne 0) {
    throw 'Could not read the STG Cloud Run build configuration.'
}

try {
    $cloudService = ($serviceJson -join "`n") | ConvertFrom-Json
    $clientIdEntries = @(
        foreach ($container in @($cloudService.spec.template.spec.containers)) {
            foreach ($entry in @($container.env)) {
                if ($entry.name -eq $GoogleClientIdVariable) {
                    $entry
                }
            }
        }
    )
    $clientId = [string]$clientIdEntries[0].value
    $apiBaseUrl = [string]$cloudService.status.url
} catch {
    throw 'The STG Cloud Run build configuration could not be parsed.'
}

if ($clientIdEntries.Count -ne 1 -or [string]::IsNullOrWhiteSpace($clientId)) {
    throw "Cloud Run does not provide a usable $GoogleClientIdVariable; refusing to build."
}
if ([string]::IsNullOrWhiteSpace($apiBaseUrl)) {
    throw 'Cloud Run did not provide its API URL; refusing to build.'
}

$apiUri = $null
if (-not [Uri]::TryCreate($apiBaseUrl, [UriKind]::Absolute, [ref]$apiUri) -or
    $apiUri.Scheme -ne 'https') {
    throw 'Cloud Run returned an invalid HTTPS API URL; refusing to build.'
}
if (-not (Test-Path (Join-Path $FrontendDirectory 'pubspec.yaml'))) {
    throw 'The Flutter frontend directory is missing.'
}

$flutterArguments = @(
    'build',
    'apk',
    '--debug',
    "--dart-define=API_BASE_URL=$apiBaseUrl",
    "--dart-define=MEDIA_API_BASE=$apiBaseUrl",
    "--dart-define=$GoogleClientIdVariable=$clientId"
)

Push-Location $FrontendDirectory
try {
    $buildOutput = @(& flutter @flutterArguments 2>&1)
    $buildExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($buildExitCode -ne 0) {
    foreach ($line in $buildOutput) {
        $safeLine = [string]$line
        if (-not [string]::IsNullOrEmpty($clientId)) {
            $safeLine = $safeLine.Replace($clientId, '[REDACTED]')
        }
        Write-Host $safeLine
    }
    throw "Flutter debug APK build failed (exit code $buildExitCode)."
}
if (-not (Test-Path $ApkPath)) {
    throw 'Flutter completed without producing the expected debug APK.'
}

Add-Type -AssemblyName System.IO.Compression
$archive = [System.IO.Compression.ZipFile]::OpenRead($ApkPath)
try {
    $kernelEntry = $archive.GetEntry('assets/flutter_assets/kernel_blob.bin')
    if ($null -eq $kernelEntry) {
        throw 'The debug APK does not contain its compiled Flutter kernel; refusing verification.'
    }

    $kernelStream = $kernelEntry.Open()
    $kernelMemory = New-Object System.IO.MemoryStream
    try {
        $kernelStream.CopyTo($kernelMemory)
        $kernelBytes = $kernelMemory.ToArray()
    } finally {
        $kernelMemory.Dispose()
        $kernelStream.Dispose()
    }
} finally {
    $archive.Dispose()
}

$clientIdBytes = [System.Text.Encoding]::UTF8.GetBytes($clientId)
$apiUrlBytes = [System.Text.Encoding]::UTF8.GetBytes($apiBaseUrl)
if (-not (Test-ByteSequence -Haystack $kernelBytes -Needle $clientIdBytes) -or
    -not (Test-ByteSequence -Haystack $kernelBytes -Needle $apiUrlBytes)) {
    throw 'The built debug APK is missing the expected Cloud Run configuration; refusing verification.'
}

Write-Host "Built and verified STG debug APK: $ApkPath"
