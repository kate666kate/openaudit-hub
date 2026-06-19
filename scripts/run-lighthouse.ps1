param(
  [string[]]$Url,
  [string]$ComposeFile = "docker-compose.yml"
)

$root = Split-Path -Parent $PSScriptRoot
$urlsFile = Join-Path $root "config/lhci/urls.txt"
$envFile = Join-Path $root ".env"

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

if (-not (Test-Path $envFile)) {
  Write-Error "Missing .env. Create it first, then run .\scripts\init-lhci.ps1"
  exit 1
}

$envRaw = Get-Content $envFile -Raw
if ($envRaw -match 'LHCI_BUILD_TOKEN=replace-with-project-build-token') {
  Write-Error "LHCI_BUILD_TOKEN is still a placeholder. Run .\scripts\init-lhci.ps1 first."
  exit 1
}

function New-LhciHash {
  $seed = "$(Get-Date -Format o)-$([guid]::NewGuid().ToString())"
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($seed)
  $sha1 = [System.Security.Cryptography.SHA1]::Create()
  try {
    return (($sha1.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "")
  } finally {
    $sha1.Dispose()
  }
}

$hashMatch = [regex]::Match($envRaw, 'LHCI_BUILD_CONTEXT__CURRENT_HASH=(.+)')
if (-not $hashMatch.Success -or [string]::IsNullOrWhiteSpace($hashMatch.Groups[1].Value)) {
  $env:LHCI_BUILD_CONTEXT__CURRENT_HASH = New-LhciHash
} elseif ($hashMatch.Groups[1].Value.Trim() -eq "0000000000000000000000000000000000000000") {
  $env:LHCI_BUILD_CONTEXT__CURRENT_HASH = New-LhciHash
}

$branchMatch = [regex]::Match($envRaw, 'LHCI_BUILD_CONTEXT__CURRENT_BRANCH=(.+)')
if (-not $branchMatch.Success -or [string]::IsNullOrWhiteSpace($branchMatch.Groups[1].Value)) {
  $env:LHCI_BUILD_CONTEXT__CURRENT_BRANCH = "main"
}

$commitMessageMatch = [regex]::Match($envRaw, 'LHCI_BUILD_CONTEXT__COMMIT_MESSAGE=(.+)')
if (-not $commitMessageMatch.Success -or [string]::IsNullOrWhiteSpace($commitMessageMatch.Groups[1].Value)) {
  $env:LHCI_BUILD_CONTEXT__COMMIT_MESSAGE = "OpenAudit manual Lighthouse run"
}

$commitTimeMatch = [regex]::Match($envRaw, 'LHCI_BUILD_CONTEXT__COMMIT_TIME=(.+)')
if (-not $commitTimeMatch.Success -or [string]::IsNullOrWhiteSpace($commitTimeMatch.Groups[1].Value)) {
  $env:LHCI_BUILD_CONTEXT__COMMIT_TIME = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
} elseif ($commitTimeMatch.Groups[1].Value.Trim() -eq "2026-06-11T09:00:00.000Z") {
  $env:LHCI_BUILD_CONTEXT__COMMIT_TIME = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
}

$authorMatch = [regex]::Match($envRaw, 'LHCI_BUILD_CONTEXT__AUTHOR=(.+)')
if (-not $authorMatch.Success -or [string]::IsNullOrWhiteSpace($authorMatch.Groups[1].Value)) {
  $env:LHCI_BUILD_CONTEXT__AUTHOR = "OpenAudit <openaudit@local>"
}

$avatarMatch = [regex]::Match($envRaw, 'LHCI_BUILD_CONTEXT__AVATAR_URL=(.+)')
if (-not $avatarMatch.Success -or [string]::IsNullOrWhiteSpace($avatarMatch.Groups[1].Value)) {
  $env:LHCI_BUILD_CONTEXT__AVATAR_URL = "https://www.gravatar.com/avatar/00000000000000000000000000000000.jpg?d=identicon"
}

docker compose -f $ComposeFile run --rm --no-deps lhci-collector
