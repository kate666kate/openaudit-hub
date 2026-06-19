param(
  [string[]]$Url = @(),
  [int]$Limit = 500
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot "crawl-sitemaps.py"
$argsList = @($script, "--limit", "$Limit")

foreach ($item in $Url) {
  if (-not [string]::IsNullOrWhiteSpace($item)) {
    $argsList += @("--url", $item)
  }
}

python @argsList

Write-Host ""
Write-Host "Sitemap crawl complete. Open http://localhost:9090/modules/sitemaps to review results."
