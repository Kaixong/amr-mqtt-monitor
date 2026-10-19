# One-click start from Windows: boots WSL, starts every service, opens the dashboard.
#   powershell -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu-24.04\home\kuma\amr-mqtt-monitor\scripts\windows\start.ps1
# -NoBrowser: don't open the dashboard.
param([switch]$NoBrowser, [string]$Distro = 'Ubuntu-24.04',
      [string]$Project = '/home/kuma/amr-mqtt-monitor')

$env:WSL_UTF8 = 1

# WSL stops the VM once no wsl.exe is attached; keep one hidden wsl.exe alive
if (-not (Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" |
          Where-Object CommandLine -match 'sleep infinity')) {
    Start-Process wsl.exe -ArgumentList '-d', $Distro, '--', 'sleep', 'infinity' -WindowStyle Hidden
    Start-Sleep -Seconds 5
}

wsl -d $Distro --cd $Project -- ./run.sh start
if ($LASTEXITCODE -ne 0) { Write-Warning "some services failed to start (see $Project/logs)" }

# check 127.0.0.1, not "localhost": Windows tries ::1 first, and the ports are published on
# IPv4 127.0.0.1 only, so ::1 hangs until the timeout (browsers fall back to IPv4 by themselves)
$ready = $false
for ($i = 0; $i -lt 30 -and -not $ready; $i++) {
    try { Invoke-WebRequest http://127.0.0.1:8080 -UseBasicParsing -TimeoutSec 2 | Out-Null; $ready = $true }
    catch { Start-Sleep -Seconds 2 }
}
Write-Output "dashboard reachable: $ready"
if ($ready -and -not $NoBrowser) { Start-Process 'http://localhost:8080' }
