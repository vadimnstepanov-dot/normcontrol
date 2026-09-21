$ErrorActionPreference = 'Stop'
$stoRoot = Split-Path -Parent $PSScriptRoot
$stoOut = Join-Path $PSScriptRoot 'data\word_numbering'
New-Item -ItemType Directory -Force -Path $stoOut | Out-Null
$stoWord = New-Object -ComObject Word.Application
$stoWord.Visible = $false
$stoWord.DisplayAlerts = 0
$stoWord.AutomationSecurity = 3
try {
    foreach ($stoFile in Get-ChildItem -LiteralPath $stoRoot -Filter '*.docx') {
        if ($stoFile.Name.StartsWith('~$')) { continue }
        $stoDoc = $null
        try {
            $stoDoc = $stoWord.Documents.Open($stoFile.FullName, $false, $true, $false)
            $stoRows = [System.Collections.Generic.List[object]]::new()
            foreach ($stoPara in $stoDoc.Paragraphs) {
                $stoRange = $stoPara.Range
                $stoRows.Add([PSCustomObject]@{text=$stoRange.Text; label=$stoRange.ListFormat.ListString})
            }
            $stoRecord = [PSCustomObject]@{sha256=(Get-FileHash -LiteralPath $stoFile.FullName -Algorithm SHA256).Hash.ToLower(); paragraphs=$stoRows}
            $stoRecord | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $stoOut ($stoFile.BaseName+'.json')) -Encoding utf8
            Write-Output ($stoFile.Name + ': ' + $stoRows.Count + ' paragraphs')
        } finally { if ($null -ne $stoDoc) { $stoDoc.Close(0) } }
    }
} finally { $stoWord.Quit(0) }
