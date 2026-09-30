param([Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$Distro)
$ErrorActionPreference = 'Stop'
# A foreground Linux process keeps WSL alive after short commands finish.
$keeperArguments = '-d ' + $Distro + ' -- sleep infinity'
$keeperProcess = Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" | Where-Object { $_.CommandLine -like ('*' + $keeperArguments + '*') }
if (-not $keeperProcess) {
    Start-Process -FilePath 'wsl.exe' -ArgumentList $keeperArguments -WindowStyle Hidden | Out-Null
}
