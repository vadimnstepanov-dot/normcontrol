param(
    [string]$PythonExecutable = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $projectRoot '.venv'

function Test-PythonVersion([string]$Executable, [string[]]$PrefixArguments = @()) {
    $version = & $Executable @PrefixArguments -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0) { return $false }
    return $version -in @('3.11', '3.12')
}

if (-not (Test-Path -LiteralPath $venv)) {
    $created = $false
    if ($PythonExecutable) {
        $resolvedPython = (Resolve-Path -LiteralPath $PythonExecutable -ErrorAction Stop).Path
        if (-not (Test-PythonVersion $resolvedPython)) {
            throw 'PythonExecutable must point to Python 3.11 or 3.12 x64.'
        }
        & $resolvedPython -m venv $venv
        $created = $LASTEXITCODE -eq 0
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($version in @('-3.12', '-3.11')) {
            & py $version -m venv $venv
            if ($LASTEXITCODE -eq 0) { $created = $true; break }
        }
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $resolvedPython = (Get-Command python).Source
        if (Test-PythonVersion $resolvedPython) {
            & $resolvedPython -m venv $venv
            $created = $LASTEXITCODE -eq 0
        }
    }
    if (-not $created) { throw 'Python 3.11 or 3.12 x64 was not found. Install it or pass -PythonExecutable C:\path\to\python.exe.' }
}

$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Failed to create the virtual environment with Python 3.11 or 3.12.'
}
if (-not (Test-PythonVersion $python)) {
    throw 'The existing .venv does not use Python 3.11 or 3.12. Remove or rename .venv and run the installer again.'
}

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Failed to upgrade pip.' }
& $python -m pip install -r (Join-Path $projectRoot 'sto_rag\nc5\requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Failed to install local engine dependencies.' }
& $python -m pip install -r (Join-Path $projectRoot 'normcontrol-web\requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Failed to install portal dependencies.' }

$data = Join-Path $projectRoot 'sto_rag\data\nc5'
New-Item -ItemType Directory -Force -Path $data | Out-Null
$config = Join-Path $data 'config.json'
if (-not (Test-Path -LiteralPath $config)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot 'config\config.example.json') -Destination $config
}

Write-Output 'Installation completed.'
Write-Output ('Configuration: ' + $config)
Write-Output 'Start llama.cpp, then run scripts\start-local.ps1.'
