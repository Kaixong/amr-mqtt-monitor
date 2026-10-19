# One-click stop from Windows: stops the services and shuts WSL down (frees its memory).
#   powershell -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu-24.04\home\kuma\amr-mqtt-monitor\scripts\windows\stop.ps1
# Note: `wsl --shutdown` stops every WSL distro, not only this project.
param([string]$Distro = 'Ubuntu-24.04', [string]$Project = '/home/kuma/amr-mqtt-monitor')

$env:WSL_UTF8 = 1
wsl -d $Distro --cd $Project -- ./run.sh stop

# the Windows-side sampler reads its files over \\wsl.localhost, which would boot WSL
# again: wait until it has seen the "stop" heartbeat and exited
for ($i = 0; $i -lt 20; $i++) {
    $left = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
              Where-Object CommandLine -match 'EncodedCommand').Count
    if ($left -eq 0) { break }
    Start-Sleep -Seconds 1
}
wsl --shutdown
Write-Output "stopped; WSL state:"
wsl -l -v
