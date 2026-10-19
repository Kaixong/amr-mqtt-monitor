"""Publish the Windows host's resource usage to MQTT.

Runs inside WSL, but measures Windows itself (not the WSL VM). A hidden, detached
powershell.exe sampler (CIM classes, locale-independent) writes stats.json into a shared
folder; this script publishes it. The sampler must be detached: a Windows process started
directly through WSL interop is killed when the wsl.exe console that launched it closes.

Lifecycle (no clocks compared across WSL/Windows, so clock drift doesn't matter):
  - sysmon.py rewrites `heartbeat` every loop; the sampler exits if it stops changing,
    or right away when it reads "stop" (written by sysmon.py on a clean shutdown)
  - the sampler writes its PID to `sampler.pid` and exits if another sampler takes over
  - sysmon.py (re)launches a sampler whenever stats.json stops changing

Topics:
  system/<host>/stats   JSON, retained
  system/<host>/online  "online" / "offline" (retained, Last Will)
"""
import argparse
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.mqttutil import add_args, connect, make_client  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "logs" / "sysmon"
HEARTBEAT_TIMEOUT = 15  # s, sampler exits if heartbeat unchanged this long
STALE_RELAUNCH = 10     # s, relaunch sampler if stats.json unchanged this long

PS_SAMPLER = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$dir = '__DIR__'
$interval = __INTERVAL__
$hbFile = Join-Path $dir 'heartbeat'
$pidFile = Join-Path $dir 'sampler.pid'
$outFile = Join-Path $dir 'stats.json'
$tmpFile = Join-Path $dir 'stats.json.tmp'
[IO.File]::WriteAllText($pidFile, "$PID")

$cpuName = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name.Trim()
$osName = (Get-CimInstance Win32_OperatingSystem).Caption
$seq = 0
$lastHb = $null; $lastHbChange = Get-Date

while ($true) {
  $t0 = Get-Date
  # stop if superseded by a newer sampler, or if sysmon.py stopped beating
  if ((Get-Content $pidFile -Raw).Trim() -ne "$PID") { exit }
  $hb = Get-Content $hbFile -Raw
  if ("$hb".Trim() -eq 'stop') { exit }   # sysmon.py shut down cleanly
  if ($hb -ne $lastHb) { $lastHb = $hb; $lastHbChange = $t0 }
  elseif (($t0 - $lastHbChange).TotalSeconds -gt __HB_TIMEOUT__) { exit }

  $p = Get-CimInstance Win32_PerfFormattedData_Counters_ProcessorInformation
  $total = ($p | Where-Object Name -eq '_Total').PercentProcessorUtility
  $cores = @($p | Where-Object { $_.Name -notmatch '_Total' } |
    Sort-Object { [int]($_.Name -split ',')[1] } |
    ForEach-Object { [Math]::Min(100, [int]$_.PercentProcessorUtility) })
  $os = Get-CimInstance Win32_OperatingSystem
  $disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
    @{ name = $_.DeviceID; total = [int64]$_.Size; free = [int64]$_.FreeSpace } })
  $dio = Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk -Filter "Name='_Total'"
  $net = @(Get-CimInstance Win32_PerfFormattedData_Tcpip_NetworkInterface)
  $bat = Get-CimInstance Win32_Battery | Select-Object -First 1
  $seq++
  $o = [ordered]@{
    seq      = $seq
    host     = $env:COMPUTERNAME
    os       = $osName
    cpu_name = $cpuName
    cpu      = [Math]::Min(100, [int]$total)
    cores    = $cores
    mem_total = [int64]$os.TotalVisibleMemorySize * 1024
    mem_used  = ([int64]$os.TotalVisibleMemorySize - [int64]$os.FreePhysicalMemory) * 1024
    disks    = $disks
    disk_busy  = [Math]::Min(100, [int]$dio.PercentDiskTime)
    disk_read  = [int64]$dio.DiskReadBytesPersec
    disk_write = [int64]$dio.DiskWriteBytesPersec
    net_rx   = [int64](($net | Measure-Object BytesReceivedPersec -Sum).Sum)
    net_tx   = [int64](($net | Measure-Object BytesSentPersec -Sum).Sum)
    battery  = if ($bat) { [int]$bat.EstimatedChargeRemaining } else { $null }
    charging = if ($bat) { $bat.BatteryStatus -eq 2 } else { $null }
    uptime   = [int]((Get-Date) - $os.LastBootUpTime).TotalSeconds
    procs    = [int]$os.NumberOfProcesses
  }
  # write-then-rename so the reader never sees a half-written file
  [IO.File]::WriteAllText($tmpFile, ($o | ConvertTo-Json -Compress -Depth 4))
  Move-Item -Force $tmpFile $outFile

  $wait = $interval - ((Get-Date) - $t0).TotalSeconds
  if ($wait -gt 0) { Start-Sleep -Milliseconds ([int]($wait * 1000)) }
}
"""


def find_powershell():
    return shutil.which("powershell.exe") or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def launch_sampler(interval):
    """Start the sampler as a hidden process that is not tied to any WSL console."""
    win_dir = subprocess.check_output(["wslpath", "-w", str(DATA_DIR)], text=True).strip()
    script = (PS_SAMPLER.replace("__DIR__", win_dir.replace("'", "''"))
              .replace("__INTERVAL__", str(interval))
              .replace("__HB_TIMEOUT__", str(HEARTBEAT_TIMEOUT)))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    launcher = ("Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "
                f"'-NoProfile','-NonInteractive','-EncodedCommand','{encoded}'")
    subprocess.run([find_powershell(), "-NoProfile", "-NonInteractive", "-Command", launcher],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=30)


def main():
    ap = argparse.ArgumentParser()
    add_args(ap)
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between samples")
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    heartbeat = DATA_DIR / "heartbeat"
    stats_file = DATA_DIR / "stats.json"
    stats_file.unlink(missing_ok=True)  # don't republish a sample left over from a previous run

    client = make_client("sysmon", "sysmon")
    topic_base = None
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    last_seq = None
    last_change = 0.0  # force a launch on start
    beat = 0
    while running:
        beat += 1
        heartbeat.write_text(f"{os.getpid()}:{beat}")

        try:
            data = json.loads(stats_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            data = None

        now = time.monotonic()
        if data and data.get("seq") != last_seq:
            last_seq, last_change = data["seq"], now
            data["ts"] = round(time.time(), 3)
            if topic_base is None:
                # connect once we know the Windows host name (topic + Last Will depend on it)
                topic_base = f"system/{data['host']}"
                client.will_set(f"{topic_base}/online", "offline", qos=1, retain=True)
                client.on_connect = lambda c, *_: c.publish(f"{topic_base}/online", "online",
                                                            qos=1, retain=True)
                connect(client, args.host, args.port, keepalive=10)
                print(f"publishing to {topic_base}/stats every {args.interval}s")
            client.publish(f"{topic_base}/stats", json.dumps(data), retain=True)
        elif now - last_change > STALE_RELAUNCH:
            print("no fresh samples, launching sampler", flush=True)
            try:
                launch_sampler(args.interval)
            except (OSError, subprocess.SubprocessError) as e:
                print(f"launch failed: {e}", file=sys.stderr)
            last_change = now  # give it time to start before retrying

        time.sleep(args.interval / 2)

    # tell the sampler to exit now (if we die uncleanly it notices the stale heartbeat instead)
    heartbeat.write_text("stop")
    if topic_base:
        try:
            client.publish(f"{topic_base}/online", "offline", qos=1,
                           retain=True).wait_for_publish(2)
        except (RuntimeError, ValueError):
            pass  # not connected: the broker will publish the Last Will instead
        client.loop_stop()
        client.disconnect()
    print("stopped")


if __name__ == "__main__":
    main()
