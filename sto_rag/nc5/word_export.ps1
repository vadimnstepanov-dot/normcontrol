param([Parameter(Mandatory=$true)][string]$Source,[Parameter(Mandatory=$true)][string]$Destination,[ValidateSet('docx','pdf')][string]$Format='pdf')
$ErrorActionPreference='Stop'
$sourcePath=(Resolve-Path -LiteralPath $Source).Path
$outputPath=[IO.Path]::GetFullPath($Destination)
$workspacePath=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if (-not $outputPath.StartsWith($workspacePath+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) {throw 'Output must be within the project workspace'}
if ($sourcePath -eq $outputPath) {throw 'Original cannot be overwritten'}
function Get-Sha256([string]$Path) {
  $sha=[Security.Cryptography.SHA256]::Create();$stream=[IO.File]::OpenRead($Path)
  try {return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','')}
  finally {$stream.Dispose();$sha.Dispose()}
}
$before=Get-Sha256 $sourcePath
New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($outputPath)) -Force | Out-Null
$word=$null;$doc=$null
try {
  $word=New-Object -ComObject Word.Application
  $word.Visible=$false;$word.DisplayAlerts=0;$word.AutomationSecurity=3
  $word.Options.UpdateLinksAtOpen=$false
  $doc=$word.Documents.Open($sourcePath,$false,$true,$false)
  if ($Format -eq 'pdf') {$doc.ExportAsFixedFormat($outputPath,17)} else {$doc.SaveAs2($outputPath,16)}
} finally {
  # Windows PowerShell 5 requires Word's by-reference optional arguments to be
  # passed explicitly; PowerShell 7 accepts both forms.
  $doNotSave=0
  if ($doc) {$doc.Close([ref]$doNotSave);[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($doc)}
  if ($word) {$word.Quit([ref]$doNotSave);[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($word)}
}
if ((Get-Sha256 $sourcePath) -ne $before) {throw 'Source checksum changed'}
Write-Output $outputPath
