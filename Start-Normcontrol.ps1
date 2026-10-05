$ErrorActionPreference = 'Stop'
$startupMutex = New-Object System.Threading.Mutex($false, 'Local\NormControlStartup')
if (-not $startupMutex.WaitOne(0)) { $startupMutex.Dispose(); exit }
try {
$reviewRoot = $PSScriptRoot
$reviewPython = 'C:\LM\normcontrol-v5\venv\Scripts\python.exe'
$reviewData = Join-Path $reviewRoot 'sto_rag\data\nc5'
$env:PYTHONPATH = Join-Path $reviewRoot 'sto_rag'
New-Item -ItemType Directory -Path $reviewData -Force | Out-Null
# Systemd services alone do not keep the WSL distribution alive.
# Keep a foreground Linux process attached while the local stack is running.
$wslArguments = '-d NormControl -- sleep infinity'
$wslKeeper = Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like ('*' + $wslArguments + '*') }
if (-not $wslKeeper) {
    $wslProcess = Start-Process -FilePath 'wsl.exe' -ArgumentList $wslArguments -WindowStyle Hidden -RedirectStandardOutput (Join-Path $reviewData 'wsl-keepalive.stdout.log') -RedirectStandardError (Join-Path $reviewData 'wsl-keepalive.stderr.log') -PassThru
    $wslProcess.Id | Set-Content -LiteralPath (Join-Path $reviewData 'wsl-keepalive.pid')
}
function Start-ReviewComponent($name, $arguments) {
    $pidPath = Join-Path $reviewData ($name + '.pid')
    if (Test-Path -LiteralPath $pidPath) {
        $storedPid = [int](Get-Content -LiteralPath $pidPath -Raw)
        $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$storedPid" -ErrorAction SilentlyContinue
        if ($existing -and $existing.CommandLine -like ('*' + $arguments + '*')) { return }
    }
    $process = Start-Process -FilePath $reviewPython -ArgumentList $arguments -WorkingDirectory $reviewRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $reviewData ($name+'.stdout.log')) -RedirectStandardError (Join-Path $reviewData ($name+'.stderr.log')) -PassThru
    $process.Id | Set-Content -LiteralPath $pidPath
}
$coreMarker = Join-Path $reviewData 'wsl-core-active.json'
if (Test-Path -LiteralPath $coreMarker) {
    Start-ReviewComponent 'host-bridge' '-X utf8 -m native_core.host_bridge C:/LM/normcontrol-v5/native-core/host-bridge.json'
    Start-ReviewComponent 'core-forwarder' '-X utf8 -m native_core.forwarder C:/LM/normcontrol-v5/native-core/forwarder.json'
    $coreStarter = Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*systemctl start normcontrol-core.service*' }
    if (-not $coreStarter) {
        Start-Process -FilePath 'wsl.exe' -ArgumentList '-d NormControl -u root -- systemctl start normcontrol-core.service' -WindowStyle Hidden -RedirectStandardOutput (Join-Path $reviewData 'core-start.stdout.log') -RedirectStandardError (Join-Path $reviewData 'core-start.stderr.log') | Out-Null
    }
    Start-ReviewComponent 'linux-monitor' '-X utf8 -m native_core.windows_monitor C:/LM/normcontrol-v5/native-core/monitor.json C:/LM/normcontrol-v5/worker.env'
    Start-ReviewComponent 'sleep-guard' '-X utf8 -m nc5.sleep_guard http://127.0.0.1:8096 --core-config C:/LM/normcontrol-v5/native-core/monitor.json'
    exit
}
$uiAvailable = $false
try { $uiAvailable = (Invoke-RestMethod 'http://127.0.0.1:8096/health' -TimeoutSec 2).version -eq 5 } catch {}
if (-not $uiAvailable) { Start-ReviewComponent 'ui' '-X utf8 sto_rag/normcontrol_v5.py serve' }
$gatewayAvailable = Get-NetTCPConnection -LocalPort 8098 -State Listen -ErrorAction SilentlyContinue
if (-not $gatewayAvailable) { Start-ReviewComponent 'gateway' '-X utf8 -m nc5.gateway C:/LM/normcontrol-v5/gateway/gateway.json' }
Start-ReviewComponent 'worker' '-X utf8 -m nc5.launch_worker C:/LM/normcontrol-v5/worker.env'
Start-ReviewComponent 'llm-sidecar' '-X utf8 sto_rag/llm_sidecar.py C:/LM/normcontrol-v5/worker.env'
Start-ReviewComponent 'sleep-guard' '-X utf8 -m nc5.sleep_guard http://127.0.0.1:8096'
} finally {
    $startupMutex.ReleaseMutex()
    $startupMutex.Dispose()
}
