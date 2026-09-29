param(
  [Parameter(Mandatory = $true)]
  [string]$Path
)

$text = [System.IO.File]::ReadAllText($Path)
$pattern = '/(?:api|v1|custom)/[^''"`\s,)}]+'
[regex]::Matches($text, $pattern) |
  ForEach-Object { $_.Value } |
  Sort-Object -Unique
