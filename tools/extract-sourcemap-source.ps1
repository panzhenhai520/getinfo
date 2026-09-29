param(
  [Parameter(Mandatory = $true)]
  [string]$MapPath,

  [Parameter(Mandatory = $true)]
  [string]$SourcePattern
)

$json = Get-Content -LiteralPath $MapPath -Raw | ConvertFrom-Json
for ($i = 0; $i -lt $json.sources.Count; $i++) {
  $source = [string]$json.sources[$i]
  if ($source -like $SourcePattern) {
    "===== $source ====="
    [string]$json.sourcesContent[$i]
  }
}
