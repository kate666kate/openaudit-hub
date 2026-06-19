param(
  [Parameter(Mandatory = $true)]
  [string[]]$Url,
  [string]$EnvFile = ".env"
)

$root = Split-Path -Parent $PSScriptRoot
$urlsFile = Join-Path $root "config/lhci/urls.txt"
$envPath = Join-Path $root $EnvFile

$cleanUrls = $Url |
  ForEach-Object { $_ -split "," } |
  ForEach-Object { $_.Trim() } |
  Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

if (-not $cleanUrls.Count) {
  Write-Error "Please provide at least one URL."
  exit 1
}

foreach ($target in $cleanUrls) {
  if ($target -notmatch '^https?://') {
    Write-Error "URL must start with http:// or https://: $target"
    exit 1
  }
}

Set-Content -Path $urlsFile -Value $cleanUrls

if (Test-Path $envPath) {
  $lines = Get-Content $envPath
  $written = @()
  $updated = $false

  foreach ($line in $lines) {
    if ($line -match '^DEFAULT_TARGET_URL=') {
      $written += "DEFAULT_TARGET_URL=$($cleanUrls[0])"
      $updated = $true
    } else {
      $written += $line
    }
  }

  if (-not $updated) {
    $written += "DEFAULT_TARGET_URL=$($cleanUrls[0])"
  }

  Set-Content -Path $envPath -Value $written
}

Write-Host "Updated Lighthouse target list:"
$cleanUrls | ForEach-Object { Write-Host " - $_" }
Write-Host ""
Write-Host "Next: powershell -ExecutionPolicy Bypass -File .\scripts\run-lighthouse.ps1"
