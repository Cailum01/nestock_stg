[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [switch]$Apply,

    [ValidateRange(1, 60)]
    [int]$TimeoutMinutes = 25
)

$ErrorActionPreference = "Stop"
$database = "(default)"
$collectionGroups = @(
    "identity_attempt_limits",
    "invite_attempts",
    "invite_limits",
    "bootstrap_challenges"
)

function Invoke-GcloudJson {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $raw = & gcloud @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud read-only verification failed (exit $LASTEXITCODE)."
    }
    if ([string]::IsNullOrWhiteSpace(($raw | Out-String))) {
        return @()
    }
    return @($raw | ConvertFrom-Json)
}

$project = Invoke-GcloudJson @("projects", "describe", $ProjectId, "--format=json")
if ($project.name -ne "Nestock STG") {
    throw "Refusing TTL changes: selected project is not the expected STG project."
}

$databases = Invoke-GcloudJson @(
    "firestore", "databases", "list",
    "--project=$ProjectId", "--format=json"
)
$defaultDatabase = @(
    $databases | Where-Object {
        $_.name -like "*/databases/(default)" -and $_.type -eq "FIRESTORE_NATIVE"
    }
)
if ($defaultDatabase.Count -ne 1) {
    throw "Refusing TTL changes: expected exactly one Native-mode default database."
}

function Get-Policies {
    param([Parameter(Mandatory = $true)][string]$CollectionGroup)

    Invoke-GcloudJson @(
        "firestore", "fields", "ttls", "list",
        "--project=$ProjectId",
        "--database=$database",
        "--collection-group=$CollectionGroup",
        "--format=json"
    )
}

$plans = @()
foreach ($group in $collectionGroups) {
    $policies = @(Get-Policies -CollectionGroup $group)
    if ($policies.Count -gt 1) {
        throw "Refusing TTL changes: multiple TTL policies exist for $group."
    }
    if ($policies.Count -eq 1) {
        $policy = $policies[0]
        $field = ($policy.name -split "/fields/")[-1]
        if ($field -ne "expires_at") {
            throw "Refusing TTL changes: $group already uses a different TTL field."
        }
        $plans += [pscustomobject]@{
            CollectionGroup = $group
            State           = $policy.ttlConfig.state
            Action          = "existing"
        }
    }
    else {
        $plans += [pscustomobject]@{
            CollectionGroup = $group
            State           = "NOT_CONFIGURED"
            Action          = "enable expires_at"
        }
    }
}

if (-not $Apply) {
    $plans | Format-Table -AutoSize
    Write-Output "Dry run only. Re-run with -Apply to enable approved TTL policies."
    return
}

foreach ($plan in $plans) {
    if ($plan.Action -ne "enable expires_at") {
        continue
    }
    $output = & gcloud firestore fields ttls update expires_at `
        "--collection-group=$($plan.CollectionGroup)" `
        "--database=$database" `
        "--project=$ProjectId" `
        --enable-ttl --async 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "TTL enablement failed for $($plan.CollectionGroup) (exit $LASTEXITCODE)."
    }
    Write-Output "TTL enablement requested for $($plan.CollectionGroup)."
}

$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
do {
    $remaining = @()
    foreach ($group in $collectionGroups) {
        $policies = @(Get-Policies -CollectionGroup $group)
        if (
            $policies.Count -ne 1 -or
            (($policies[0].name -split "/fields/")[-1] -ne "expires_at") -or
            $policies[0].ttlConfig.state -ne "ACTIVE"
        ) {
            $remaining += $group
        }
    }
    if ($remaining.Count -eq 0) {
        Write-Output "All approved Firestore TTL policies are ACTIVE."
        return
    }
    if ((Get-Date) -ge $deadline) {
        throw "TTL policies are not ACTIVE before timeout: $($remaining -join ', ')."
    }
    Start-Sleep -Seconds 20
} while ($true)
