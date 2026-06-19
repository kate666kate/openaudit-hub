param(
  [string[]]$TargetUrl = @(),
  [string]$OutputName = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$reportDir = Join-Path $root "outputs/reports"
$targetsFile = Join-Path $root "config/lhci/urls.txt"
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null

function Get-SiteKey([string]$Url) {
  try {
    $hostName = ([Uri]$Url).Host.ToLowerInvariant()
  } catch {
    $hostName = $Url.ToLowerInvariant().Replace("https://", "").Replace("http://", "").Trim("/")
  }
  return ($hostName -replace '[^a-z0-9]+', '-').Trim('-')
}

if (-not $TargetUrl -or $TargetUrl.Count -eq 0) {
  if (-not (Test-Path $targetsFile)) {
    throw "No TargetUrl provided and config/lhci/urls.txt was not found."
  }
  $TargetUrl = Get-Content $targetsFile | Where-Object {
    -not [string]::IsNullOrWhiteSpace($_) -and -not $_.TrimStart().StartsWith("#")
  }
}

foreach ($target in $TargetUrl) {
  $target = $target.Trim()
  if ([string]::IsNullOrWhiteSpace($target)) {
    continue
  }

  $siteKey = Get-SiteKey $target
  $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $currentOutputName = $OutputName
  if ([string]::IsNullOrWhiteSpace($currentOutputName)) {
    $currentOutputName = "/reports/linkcheck-$siteKey-$stamp.txt"
  }

  $localOutput = Join-Path $root ($currentOutputName.Replace("/reports/", "outputs/reports/").Replace("/", "\"))
  Write-Host "Running LinkChecker for $target ..."
  docker compose run --rm linkchecker --verbose $target 2>&1 |
    Where-Object {
      "$_" -notmatch "writing to uninitialized or closed file" -and
      "$_" -notmatch "URLs are still active.*After a timeout"
    } |
    Tee-Object -FilePath $localOutput
  Write-Host "Saved $localOutput"
}
