param([Parameter(Mandatory=$true)][string]$Source,[Parameter(Mandatory=$true)][string]$Destination,[ValidateSet('read','anchors','review')][string]$Action='read',[string]$Plan)
$ErrorActionPreference='Stop'
$sourcePath=(Resolve-Path -LiteralPath $Source).Path
$outputPath=[IO.Path]::GetFullPath($Destination)
$workspacePath=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if (-not $outputPath.StartsWith($workspacePath+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase) -or $sourcePath -eq $outputPath) {throw 'Output must be a new workspace file'}
function Digest([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function SaveJson($Value,[string]$Path) { [IO.File]::WriteAllText($Path,($Value|ConvertTo-Json -Depth 30 -Compress),(New-Object Text.UTF8Encoding($false))) }
function FinalText([xml]$Xml) {
    $ns=New-Object Xml.XmlNamespaceManager($Xml.NameTable)
    $ns.AddNamespace('w','http://schemas.openxmlformats.org/wordprocessingml/2006/main')
    $p=$Xml.SelectSingleNode('//w:body/w:p',$ns)
    if (-not $p) {throw 'Paragraph XML unavailable'}
    $pieces=foreach($n in $p.SelectNodes('.//w:t[not(ancestor::w:del or ancestor::w:moveFrom)] | .//w:tab | .//w:br | .//w:cr',$ns)) {
        if($n.LocalName -eq 't') {$n.InnerText} elseif($n.LocalName -eq 'tab') {"`t"} else {"`n"}
    }
    if(-not $pieces) {return ''}
    return [string]::Join('',[string[]]$pieces)
}
$before=Digest $sourcePath
New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($outputPath)) -Force | Out-Null
$word=$null;$doc=$null;$oldUser=$null;$oldInitials=$null;$oldPagination=$null
try {
    $word=New-Object -ComObject Word.Application
    $oldUser=$word.UserName;$oldInitials=$word.UserInitials
    $oldPagination=$word.Options.Pagination
    $word.Visible=$false;$word.DisplayAlerts=0;$word.AutomationSecurity=3;$word.Options.UpdateLinksAtOpen=$false
    $word.ScreenUpdating=$false
    if($Action -ne 'review') {$word.Options.Pagination=$false}
    $doc=$word.Documents.Open($sourcePath,$false,$true,$false)
    if($Action -eq 'review' -and $doc.ProtectionType -ne -1) {throw 'Protected document requires owner action'}
    if($Action -eq 'read') {
        $timer=[Diagnostics.Stopwatch]::StartNew()
        $doc.ActiveWindow.View.Type=1
        $flat=$doc.WordOpenXML
        SaveJson @{adapter='native-doc-v1';source_sha256=$before;flat_xml=$flat;paragraphs=@();paragraph_count=$doc.Content.Paragraphs.Count;comments=$doc.Comments.Count;revisions=$doc.Revisions.Count;seconds=$timer.Elapsed.TotalSeconds} $outputPath
    } elseif($Action -eq 'anchors') {
        $timer=[Diagnostics.Stopwatch]::StartNew()
        $doc.ActiveWindow.View.Type=1
        $request=Get-Content -LiteralPath $Plan -Raw -Encoding UTF8|ConvertFrom-Json
        if($request.source_sha256 -ne $before -or $request.anchors.Count -gt 100) {throw 'Anchor request identity/limit'}
        $rows=New-Object Collections.Generic.List[object]
        $unmapped=New-Object Collections.Generic.List[string]
        foreach($item in $request.anchors) {
            if($request.paragraph_count -ne $doc.Content.Paragraphs.Count -or $item.paragraph -lt 1 -or $item.paragraph -gt $doc.Content.Paragraphs.Count) {$unmapped.Add([string]$item.locator);continue}
            $p=$doc.Content.Paragraphs.Item([int]$item.paragraph);$range=$p.Range
            $raw=[string]$range.Text;$visible=$raw.TrimEnd([char[]]@([char]13,[char]7)).Replace([char]11,[char]10)
            $editable=$range.Revisions.Count -eq 0 -and $range.Fields.Count -eq 0
            if($visible -cne [string]$item.text) {$visible=FinalText ([xml]$range.WordOpenXML);$editable=$false}
            if($visible -cne [string]$item.text) {$unmapped.Add([string]$item.locator);continue}
            $rows.Add(@{locator=[string]$item.locator;start=[int]$range.Start;end=[int]$range.End;raw=$raw;text=$visible;editable=$editable})
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($range);[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($p)
        }
        SaveJson @{adapter='native-doc-v1';source_sha256=$before;anchors=@($rows.ToArray());unmapped=@($unmapped.ToArray());seconds=$timer.Elapsed.TotalSeconds} $outputPath
    } else {
        $saved=Get-Content -LiteralPath $Plan -Raw -Encoding UTF8|ConvertFrom-Json
        if($saved.adapter -ne 'native-doc-v1' -or $saved.source_sha256 -ne $before -or $saved.working_sha256 -ne $before) {throw 'Review source/adapter identity'}
        $operations=@($saved.operations)
        if($operations.Count -gt 10000) {throw 'Operation limit'}
        # Validate every range before producing any changes.
        foreach($op in $operations) {
            if($op.type -notin @('comment','replace','insert','delete') -or $op.native_start -lt 0 -or $op.native_end -lt $op.native_start -or $op.native_end -gt $doc.Content.End) {throw 'Invalid native operation'}
            $p=$doc.Range([int]$op.paragraph_start,[int]$op.paragraph_end)
            $p.TextRetrievalMode.IncludeFieldCodes=$false
            if([string]$p.Text -cne [string]$op.paragraph_raw) {throw 'Paragraph changed'}
            $range=$doc.Range([int]$op.native_start,[int]$op.native_end)
            if($op.type -ne 'comment') {
                if($op.validation -ne 'verified_source_and_specialist' -or $range.Fields.Count -gt 0 -or $range.Revisions.Count -gt 0 -or [string]$range.Text -cne [string]$op.original) {throw 'Edit not verified'}
                if([string]$op.proposed -match '[\r\n\t\x00-\x08\x0b\x0c\x0e-\x1f]') {throw 'Unsupported replacement'}
            }
        }
        $oldComments=$doc.Comments.Count;$oldRevisions=$doc.Revisions.Count
        $word.UserName='NormControl';$word.UserInitials='NC'
        # Save the same format directly; there is no DOCX intermediary.
        $doc.SaveAs2([string]$outputPath,0)
        $doc.TrackRevisions=$true
        foreach($op in $operations) {
            $range=$doc.Range([int]$op.native_start,[int]$op.native_end)
            [void]$doc.Comments.Add($range,[string]$op.comment)
        }
        $edits=0
        foreach($op in @($operations | Where-Object {$_.type -ne 'comment'} | Sort-Object native_start -Descending)) {
            $range=$doc.Range([int]$op.native_start,[int]$op.native_end)
            $range.Text=[string]$op.proposed;$edits++
        }
        $doc.Save()
        if($doc.Comments.Count -ne $oldComments+$operations.Count -or $doc.Revisions.Count -lt $oldRevisions -or ($edits -gt 0 -and $doc.Revisions.Count -le $oldRevisions)) {throw 'Review validation failed'}
        $report=@{edits=$edits;comments=$operations.Count;existing_comments=$oldComments;existing_revisions=$oldRevisions;revisions=$doc.Revisions.Count;source_sha256=$before}
        $zero=0;$doc.Close([ref]$zero);[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($doc);$doc=$null
        $report.output_sha256=Digest $outputPath
        SaveJson $report ($outputPath+'.report.json')
    }
} finally {
    $doNotSave=0
    if($doc) {$doc.Close([ref]$doNotSave);[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($doc)}
    if($word) {$word.Options.Pagination=$oldPagination;$word.UserName=$oldUser;$word.UserInitials=$oldInitials;$word.Quit([ref]$doNotSave);[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($word)}
}
if((Digest $sourcePath) -ne $before) {throw 'Original changed'}

