$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $projectRoot '.venv'

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Python Launcher (py.exe) was not found. Install Python 3.11 or 3.12 x64.'
}

if (-not (Test-Path -LiteralPath $venv)) {
    & py -3.12 -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        & py -3.11 -m venv $venv
    }
}

$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Failed to create the virtual environment with Python 3.11 or 3.12.'
}

& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $projectRoot 'sto_rag\nc5\requirements.txt')
& $python -m pip install -r (Join-Path $projectRoot 'normcontrol-web\requirements.txt')

$data = Join-Path $projectRoot 'sto_rag\data\nc5'
New-Item -ItemType Directory -Force -Path $data | Out-Null
$config = Join-Path $data 'config.json'
if (-not (Test-Path -LiteralPath $config)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot 'config\config.example.json') -Destination $config
}

Write-Output 'Installation completed.'
Write-Output ('Configuration: ' + $config)
Write-Output 'Start llama.cpp, then run scripts\start-local.ps1.'
