param(
  [Parameter(Mandatory = $true)]
  [string]$TargetUrl,
  [ValidateSet("website", "sitemap")]
  [string]$Scanner = "website",
  [string]$OutputName = ""
)

if ([string]::IsNullOrWhiteSpace($OutputName)) {
  $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $safeScanner = $Scanner.ToLowerInvariant()
  $OutputName = "/reports/oobee-$safeScanner-$stamp.zip"
}

docker compose run --rm oobee -c $Scanner -u $TargetUrl -o $OutputName
