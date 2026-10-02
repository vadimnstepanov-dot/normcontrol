param(
    [string]$PythonExecutable = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    [string]$RuntimeDirectory = (Join-Path $env:LOCALAPPDATA 'NormControl'),
    [string]$WslDistribution = 'NormControl'
)
$ErrorActionPreference = 'Stop'
$reviewRoot = $PSScriptRoot
$reviewPython = $PythonExecutable
$reviewData = Join-Path $reviewRoot 'sto_rag\data\nc5'
$env:PYTHONPATH = Join-Path $reviewRoot 'sto_rag'
if (-not (Test-Path -LiteralPath $reviewPython -PathType Leaf)) {
    throw "Python not found: $reviewPython. Run scripts/install-windows.ps1 or supply -PythonExecutable."
}
New-Item -ItemType Directory -Path $reviewData -Force | Out-Null
function Quote-ReviewArgument([string]$value) {
    if ($value.Contains('"')) { throw 'A command argument must not contain a quote.' }
    return '"' + $value + '"'
}
function Start-ReviewComponent($name, $arguments) {
    $pidPath = Join-Path $reviewData ($name + '.pid')
    if (Test-Path -LiteralPath $pidPath) {
        $storedPid = [int](Get-Content -LiteralPath $pidPath -Raw)
        $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$storedPid" -ErrorAction SilentlyContinue
        if ($existing -and $existing.CommandLine.Contains($arguments)) { return }
    }
    $process = Start-Process -FilePath $reviewPython -ArgumentList $arguments -WorkingDirectory $reviewRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $reviewData ($name+'.stdout.log')) -RedirectStandardError (Join-Path $reviewData ($name+'.stderr.log')) -PassThru
    $process.Id | Set-Content -LiteralPath $pidPath
}
$workerEnvPath = Join-Path $RuntimeDirectory 'worker.env'
if (-not (Test-Path -LiteralPath $workerEnvPath -PathType Leaf)) { throw "Configure $workerEnvPath using config/worker.env.example." }
$workerEnvArgument = Quote-ReviewArgument $workerEnvPath
$coreMarker = Join-Path $reviewData 'wsl-core-active.json'
if (Test-Path -LiteralPath $coreMarker) {
    $nativeConfig = Join-Path $RuntimeDirectory 'native-core'
    foreach ($configName in @('host-bridge.json', 'forwarder.json', 'monitor.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $nativeConfig $configName) -PathType Leaf)) { throw "Missing native-core configuration: $configName." }
    }
    # A foreground process keeps WSL alive even when all services are daemonized.
    $distroArgument = Quote-ReviewArgument $WslDistribution
    $wslArguments = '-d ' + $distroArgument + ' -- sleep infinity'
    $wslKeeper = Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine.Contains($wslArguments) }
    if (-not $wslKeeper) {
        $wslProcess = Start-Process -FilePath 'wsl.exe' -ArgumentList $wslArguments -WindowStyle Hidden -RedirectStandardOutput (Join-Path $reviewData 'wsl-keepalive.stdout.log') -RedirectStandardError (Join-Path $reviewData 'wsl-keepalive.stderr.log') -PassThru
        $wslProcess.Id | Set-Content -LiteralPath (Join-Path $reviewData 'wsl-keepalive.pid')
    }
    Start-ReviewComponent 'host-bridge' ('-X utf8 -m native_core.host_bridge ' + (Quote-ReviewArgument (Join-Path $nativeConfig 'host-bridge.json')))
    Start-ReviewComponent 'core-forwarder' ('-X utf8 -m native_core.forwarder ' + (Quote-ReviewArgument (Join-Path $nativeConfig 'forwarder.json')))
    $coreArguments = '-d ' + $distroArgument + ' -u root -- systemctl start normcontrol-core.service'
    $coreStarter = Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine.Contains($coreArguments) }
    if (-not $coreStarter) {
        Start-Process -FilePath 'wsl.exe' -ArgumentList $coreArguments -WindowStyle Hidden -RedirectStandardOutput (Join-Path $reviewData 'core-start.stdout.log') -RedirectStandardError (Join-Path $reviewData 'core-start.stderr.log') | Out-Null
    }
    Start-ReviewComponent 'linux-monitor' ('-X utf8 -m native_core.windows_monitor ' + (Quote-ReviewArgument (Join-Path $nativeConfig 'monitor.json')) + ' ' + $workerEnvArgument)
    Start-ReviewComponent 'sleep-guard' '-X utf8 -m nc5.sleep_guard http://127.0.0.1:8096'
    exit
}
$uiAvailable = $false
try { $uiAvailable = (Invoke-RestMethod 'http://127.0.0.1:8096/health' -TimeoutSec 2).version -eq 5 } catch {}
if (-not $uiAvailable) { Start-ReviewComponent 'ui' '-X utf8 sto_rag/normcontrol_v5.py serve' }
$gatewayAvailable = Get-NetTCPConnection -LocalPort 8098 -State Listen -ErrorAction SilentlyContinue
if (-not $gatewayAvailable) {
    $gatewayPath = Join-Path $RuntimeDirectory 'gateway\gateway.json'
    if (-not (Test-Path -LiteralPath $gatewayPath -PathType Leaf)) { throw "Configure the Windows gateway at $gatewayPath, or start only the standalone CLI." }
    Start-ReviewComponent 'gateway' ('-X utf8 -m nc5.gateway ' + (Quote-ReviewArgument $gatewayPath))
}
Start-ReviewComponent 'worker' ('-X utf8 -m nc5.launch_worker ' + $workerEnvArgument)
Start-ReviewComponent 'llm-sidecar' ('-X utf8 sto_rag/llm_sidecar.py ' + $workerEnvArgument)
Start-ReviewComponent 'sleep-guard' '-X utf8 -m nc5.sleep_guard http://127.0.0.1:8096'