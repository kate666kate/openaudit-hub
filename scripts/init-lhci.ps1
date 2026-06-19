param(
  [string]$ProjectName = "",
  [string]$CodeHostingUrl = "",
  [string]$BaseBranch = "main",
  [string]$ServerBaseUrl = "http://localhost:9001",
  [string]$EnvFile = ".env"
)

$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root $EnvFile
$exampleEnvPath = Join-Path $root ".env.example"

if (-not (Test-Path $envPath)) {
  if (-not (Test-Path $exampleEnvPath)) {
    Write-Error "Missing .env and .env.example"
    exit 1
  }

  Copy-Item $exampleEnvPath $envPath
}

$rawEnv = Get-Content $envPath -Raw
$envMap = @{}
foreach ($line in ($rawEnv -split "`r?`n")) {
  if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
  $parts = $line -split '=', 2
  $envMap[$parts[0]] = $parts[1]
}

if ([string]::IsNullOrWhiteSpace($ProjectName)) {
  $ProjectName = if ($envMap.ContainsKey("LHCI_PROJECT_NAME")) { $envMap["LHCI_PROJECT_NAME"] } else { "OpenAudit Website" }
}

if ([string]::IsNullOrWhiteSpace($CodeHostingUrl)) {
  $CodeHostingUrl = if ($envMap.ContainsKey("LHCI_CODE_HOSTING_URL")) { $envMap["LHCI_CODE_HOSTING_URL"] } else { "https://github.com/your-org/your-repo" }
}

$username = if ($envMap.ContainsKey("LHCI_BASIC_AUTH_USERNAME")) { $envMap["LHCI_BASIC_AUTH_USERNAME"] } else { "admin" }
$password = if ($envMap.ContainsKey("LHCI_BASIC_AUTH_PASSWORD")) { $envMap["LHCI_BASIC_AUTH_PASSWORD"] } else { "change-me" }

$baseUrl = $ServerBaseUrl.TrimEnd("/")
$authBytes = [System.Text.Encoding]::UTF8.GetBytes("${username}:${password}")
$authHeader = [Convert]::ToBase64String($authBytes)
$headers = @{
  Authorization = "Basic $authHeader"
  "Content-Type" = "application/json"
}

$body = @{
  name = $ProjectName
  externalUrl = $CodeHostingUrl
  baseBranch = $BaseBranch
  slug = ""
} | ConvertTo-Json

Write-Host "Creating LHCI project on $baseUrl ..."

try {
  $project = Invoke-RestMethod -Method Post -Uri "$baseUrl/v1/projects" -Headers $headers -Body $body
} catch {
  Write-Error "LHCI project initialization failed: $($_.Exception.Message)"
  exit 1
}

if (-not $project.token) {
  Write-Error "LHCI server responded, but no build token was returned."
  exit 1
}

$updates = @{
  "LHCI_BUILD_TOKEN" = [string]$project.token
  "LHCI_PROJECT_NAME" = [string]$project.name
  "LHCI_CODE_HOSTING_URL" = $CodeHostingUrl
}

if ($project.adminToken) {
  $updates["LHCI_PROJECT_ADMIN_TOKEN"] = [string]$project.adminToken
}

$lines = Get-Content $envPath
$written = @()
$seen = @{}
foreach ($line in $lines) {
  if ($line -match '^\s*#' -or $line -notmatch '=') {
    $written += $line
    continue
  }

  $parts = $line -split '=', 2
  $key = $parts[0]
  if ($updates.ContainsKey($key)) {
    $written += "$key=$($updates[$key])"
    $seen[$key] = $true
  } else {
    $written += $line
  }
}

foreach ($key in $updates.Keys) {
  if (-not $seen.ContainsKey($key)) {
    $written += "$key=$($updates[$key])"
  }
}

Set-Content -Path $envPath -Value $written

Write-Host "Created project $($project.name) ($($project.id))"
Write-Host "Saved LHCI tokens to $envPath"
Write-Host "Next step: powershell -ExecutionPolicy Bypass -File .\scripts\run-lighthouse.ps1"
