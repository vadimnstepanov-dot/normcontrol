$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Run scripts\install-windows.ps1 first.'
}
$env:PYTHONPATH = Join-Path $projectRoot 'sto_rag'
& $python (Join-Path $projectRoot 'sto_rag\normcontrol_v5.py') probe
if ($LASTEXITCODE -ne 0) { throw 'The local LLM API is unavailable.' }
& $python (Join-Path $projectRoot 'sto_rag\normcontrol_v5.py') serve
