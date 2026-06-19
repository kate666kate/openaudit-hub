param(
  [string[]]$Url,
  [string]$ComposeFile = "docker-compose.yml"
)

$root = Split-Path -Parent $PSScriptRoot
$urlsFile = Join-Path $root "config/lhci/urls.txt"

if ($Url -and $Url.Count -gt 0) {
  & (Join-Path $PSScriptRoot "set-target.ps1") -Url $Url
  if (-not $?) {
    exit 1
  }
}

if (-not (Test-Path $urlsFile)) {
  Write-Error "Missing config file: $urlsFile"
  exit 1
}

docker compose -f $ComposeFile run --rm --build lighthouse-report
