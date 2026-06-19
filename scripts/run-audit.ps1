param(
  [Parameter(Mandatory = $true)]
  [string[]]$Url,
  [switch]$SkipCi,
  [switch]$IncludeLinks,
  [string]$ComposeFile = "docker-compose.yml"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env"

Write-Host "Updating target URL list..."
& (Join-Path $PSScriptRoot "set-target.ps1") -Url $Url

Write-Host ""
Write-Host "Generating full Google Lighthouse HTML/JSON report..."
& (Join-Path $PSScriptRoot "run-lighthouse-report.ps1") -ComposeFile $ComposeFile

if (-not $SkipCi) {
  if ((Test-Path $envFile) -and ((Get-Content $envFile -Raw) -notmatch 'LHCI_BUILD_TOKEN=replace-with-project-build-token')) {
    Write-Host ""
    Write-Host "Uploading Lighthouse scores to Lighthouse CI..."
    & (Join-Path $PSScriptRoot "run-lighthouse.ps1") -ComposeFile $ComposeFile
  } else {
    Write-Host ""
    Write-Host "Skipping Lighthouse CI upload because LHCI_BUILD_TOKEN is not configured."
  }
}

if ($IncludeLinks) {
  Write-Host ""
  Write-Host "Running broken-link check for the first target..."
  & (Join-Path $PSScriptRoot "run-linkcheck.ps1") -TargetUrl $Url[0]
}

Write-Host ""
Write-Host "Audit complete. Open http://localhost:9090 to review the Hub."
