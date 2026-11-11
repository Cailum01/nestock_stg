[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$Subscription,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$ResourceGroup,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$AccountName,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$DatabaseName,

    [ValidateNotNullOrEmpty()]
    [string]$ContainerName = "household-data",

    [switch]$Apply,

    [switch]$ConfirmExistingTtlReviewed
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI (az) is required."
}

function Invoke-AzCli {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    $output = & az @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        $details = $output -join [Environment]::NewLine
        throw "Azure CLI command failed: az $($Arguments -join ' ')`n$details"
    }

    return $output
}

function Get-ContainerDefaultTtl {
    $output = Invoke-AzCli -Arguments @(
        "cosmosdb",
        "sql",
        "container",
        "show",
        "--subscription",
        $Subscription,
        "--resource-group",
        $ResourceGroup,
        "--account-name",
        $AccountName,
        "--database-name",
        $DatabaseName,
        "--name",
        $ContainerName,
        "--query",
        "resource.defaultTtl",
        "--output",
        "json",
        "--only-show-errors"
    )

    $json = $output -join [Environment]::NewLine
    if ([string]::IsNullOrWhiteSpace($json)) {
        return $null
    }

    return ConvertFrom-Json -InputObject $json
}

$currentTtl = Get-ContainerDefaultTtl
if ($currentTtl -eq -1) {
    Write-Output "Verified $ContainerName defaultTtl is already -1."
    return
}
if ($null -ne $currentTtl) {
    throw "Refusing to replace existing defaultTtl '$currentTtl'; review the current policy first."
}

Write-Output "Current $ContainerName defaultTtl is null; desired value is -1."
if (-not $Apply) {
    Write-Output "No changes made. Review existing per-record TTL values, then rerun with -Apply -ConfirmExistingTtlReviewed."
    return
}
if (-not $ConfirmExistingTtlReviewed) {
    throw "Review existing per-record TTL values before applying; then pass -ConfirmExistingTtlReviewed."
}
if (-not $PSCmdlet.ShouldProcess("$AccountName/$DatabaseName/$ContainerName", "Set defaultTtl to -1")) {
    return
}

$null = Invoke-AzCli -Arguments @(
    "cosmosdb",
    "sql",
    "container",
    "update",
    "--subscription",
    $Subscription,
    "--resource-group",
    $ResourceGroup,
    "--account-name",
    $AccountName,
    "--database-name",
    $DatabaseName,
    "--name",
    $ContainerName,
    "--ttl",
    "-1",
    "--output",
    "none",
    "--only-show-errors"
)

$updatedTtl = Get-ContainerDefaultTtl
if ($updatedTtl -ne -1) {
    throw "TTL update could not be verified; expected defaultTtl -1, received '$updatedTtl'."
}

Write-Output "Verified $ContainerName defaultTtl is -1."
