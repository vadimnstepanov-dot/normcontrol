param([Parameter(Mandatory=$true)][string]$Document, [Parameter(Mandatory=$true)][string]$OutputPath)
$ErrorActionPreference = 'Stop'
$sourceHash = (Get-FileHash -LiteralPath $Document -Algorithm SHA256).Hash.ToLower()
$wordApp = New-Object -ComObject Word.Application
$wordApp.Visible = $false
$wordApp.DisplayAlerts = 0
$wordApp.AutomationSecurity = 3
$wordDoc = $null
try {
    $wordDoc = $wordApp.Documents.Open($Document, $false, $true, $false)
    $rows = [System.Collections.Generic.List[object]]::new()
    foreach ($para in $wordDoc.Paragraphs) {
        if ($para.OutlineLevel -lt 10) {
            $rows.Add([PSCustomObject]@{text=$para.Range.Text; label=$para.Range.ListFormat.ListString; outline=([int]$para.OutlineLevel - 1)})
        }
    }
    if ((Get-FileHash -LiteralPath $Document -Algorithm SHA256).Hash.ToLower() -ne $sourceHash) { throw 'Source changed during numbering verification' }
    [PSCustomObject]@{sha256=$sourceHash; paragraphs=$rows} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutputPath -Encoding utf8
    Write-Output ('Verified headings: '+$rows.Count)
} finally {
    if ($null -ne $wordDoc) { $wordDoc.Close(0) }
    $wordApp.Quit(0)
}
