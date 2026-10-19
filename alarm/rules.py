"""Alarm rules. Pure logic (no MQTT, no I/O) so it can be unit tested.

Feed observations in with on_* methods, then call evaluate(now): it returns every condition
that is currently true, keyed by "<source>:<type>". The caller diffs that against the alarms
it has open to raise / clear them. Thresholds have hysteresis: once a condition is active it
stays active until the value is clearly back to normal, so alarms don't flap.
"""
import math
from collections import deque
from dataclasses import dataclass, field

DEFAULTS = {
    "amr_offline_after": 3.0,     # s without a status message
    "battery_low": 20.0, "battery_low_clear": 25.0,
    "battery_critical": 10.0, "battery_critical_clear": 15.0,
    "stuck_after": 10.0,          # s "moving" ...
    "stuck_distance": 0.2,        # ... while travelling less than this (m)
    "monitor_offline_after": 10.0,
    "cpu_high": 90.0, "cpu_high_clear": 80.0, "cpu_high_for": 10.0,
    "mem_high": 90.0, "mem_high_clear": 85.0,
    "disk_low": 10.0, "disk_low_clear": 12.0,   # % free
}

STATE_LABEL = {"moving": "移動中", "idle": "閒置", "charging": "充電中",
               "paused": "暫停", "error": "錯誤"}


@dataclass
class Condition:
    source: str
    type: str
    severity: str          # critical | warning
    message: str
    details: dict = field(default_factory=dict)

    @property
    def key(self):
        return f"{self.source}:{self.type}"


class AlarmEngine:
    def __init__(self, config=None, active_keys=()):
        self.cfg = {**DEFAULTS, **(config or {})}
        self.amrs = {}     # id -> {"status", "last_seen", "online", "track"}
        self.hosts = {}    # host -> {"stats", "last_seen", "online", "cpu_high_since"}
        self.prev = set(active_keys)   # keys active after the last evaluate (hysteresis)

    # ---------- inputs ----------
    def _amr(self, amr_id):
        return self.amrs.setdefault(amr_id, {"status": None, "last_seen": None,
                                             "online": True, "track": deque()})

    def on_amr_status(self, amr_id, status, now):
        a = self._amr(amr_id)
        a["status"] = status
        # retained messages can be old: trust the publisher's timestamp
        a["last_seen"] = min(now, status.get("ts", now))
        a["track"].append((a["last_seen"], status.get("x", 0.0), status.get("y", 0.0)))
        while a["track"] and a["track"][0][0] < now - self.cfg["stuck_after"] - 5:
            a["track"].popleft()

    def on_amr_online(self, amr_id, online):
        self._amr(amr_id)["online"] = online

    def _host(self, host):
        return self.hosts.setdefault(host, {"stats": None, "last_seen": None,
                                            "online": True, "cpu_high_since": None})

    def on_sys_stats(self, host, stats, now):
        h = self._host(host)
        h["stats"] = stats
        h["last_seen"] = min(now, stats.get("ts", now))

    def on_sys_online(self, host, online):
        self._host(host)["online"] = online

    # ---------- evaluation ----------
    def _above(self, key, value, on, off):
        """Hysteresis: true from `on` upwards, stays true until below `off`."""
        return value >= on or (key in self.prev and value >= off)

    def _below(self, key, value, on, off):
        return value <= on or (key in self.prev and value <= off)

    def _carry_over(self, out, source, skip_type):
        """Source unreachable: keep its other alarms as they were (no fresh data)."""
        for key in self.prev:
            if key.startswith(source + ":") and not key.endswith(":" + skip_type):
                out[key] = None  # placeholder, caller keeps the open alarm as is

    def evaluate(self, now):
        c = self.cfg
        out = {}

        def add(cond):
            out[cond.key] = cond

        for amr_id, a in self.amrs.items():
            s = a["status"]
            age = None if a["last_seen"] is None else now - a["last_seen"]
            if not a["online"] or age is None or age > c["amr_offline_after"]:
                where = ""
                if s:
                    where = (f"，最後位置 ({s['x']:.1f}, {s['y']:.1f})，"
                             f"狀態 {STATE_LABEL.get(s['state'], s['state'])}"
                             + (f"，任務 {s['task']}" if s.get("task") else ""))
                reason = "broker 回報離線" if not a["online"] else (
                    f"{age:.0f} 秒沒有回報" if age is not None else "沒有任何回報")
                add(Condition(amr_id, "OFFLINE", "critical",
                              f"{amr_id} 失去連線（{reason}{where}）",
                              {k: s.get(k) for k in ("x", "y", "state", "task", "battery")}
                              if s else {}))
                self._carry_over(out, amr_id, "OFFLINE")
                continue

            if s["state"] == "error":
                add(Condition(amr_id, "ERROR", "critical",
                              f"{amr_id} 故障：{s.get('error') or '未知錯誤'}",
                              {"error": s.get("error"), "x": s["x"], "y": s["y"],
                               "task": s.get("task")}))

            bat = s["battery"]
            crit_key = f"{amr_id}:BATTERY_CRITICAL"
            if self._below(crit_key, bat, c["battery_critical"], c["battery_critical_clear"]):
                add(Condition(amr_id, "BATTERY_CRITICAL", "critical",
                              f"{amr_id} 電量極低：{bat:.0f}%", {"battery": bat}))
            elif self._below(f"{amr_id}:BATTERY_LOW", bat, c["battery_low"],
                             c["battery_low_clear"]):
                add(Condition(amr_id, "BATTERY_LOW", "warning",
                              f"{amr_id} 電量偏低：{bat:.0f}%", {"battery": bat}))

            if s["state"] == "moving":
                window = [p for p in a["track"] if p[0] >= now - c["stuck_after"]]
                covers = a["track"] and a["track"][0][0] <= now - c["stuck_after"]
                if covers and window:
                    moved = max(math.dist(window[0][1:], p[1:]) for p in window)
                    if moved < c["stuck_distance"]:
                        add(Condition(amr_id, "STUCK", "warning",
                                      f"{amr_id} 疑似卡住：移動中但 {c['stuck_after']:.0f} 秒內"
                                      f"位移只有 {moved:.2f} m",
                                      {"x": s["x"], "y": s["y"], "task": s.get("task")}))

        for host, h in self.hosts.items():
            src = f"PC:{host}"
            age = None if h["last_seen"] is None else now - h["last_seen"]
            if not h["online"] or age is None or age > c["monitor_offline_after"]:
                add(Condition(src, "MONITOR_OFFLINE", "warning",
                              f"電腦 {host} 的資源監控中斷（sysmon 沒有回報）"))
                self._carry_over(out, src, "MONITOR_OFFLINE")
                continue
            st = h["stats"]

            cpu_key = f"{src}:CPU_HIGH"
            if st["cpu"] >= c["cpu_high"] or (cpu_key in self.prev and st["cpu"] >= c["cpu_high_clear"]):
                h["cpu_high_since"] = h["cpu_high_since"] or now
            else:
                h["cpu_high_since"] = None
            if h["cpu_high_since"] and (cpu_key in self.prev
                                        or now - h["cpu_high_since"] >= c["cpu_high_for"]):
                add(Condition(src, "CPU_HIGH", "warning",
                              f"電腦 {host} CPU 使用率過高：{st['cpu']}%", {"cpu": st["cpu"]}))

            mem = st["mem_used"] / st["mem_total"] * 100
            if self._above(f"{src}:MEM_HIGH", mem, c["mem_high"], c["mem_high_clear"]):
                add(Condition(src, "MEM_HIGH", "warning",
                              f"電腦 {host} 記憶體使用率過高：{mem:.0f}%", {"mem_pct": round(mem, 1)}))

            for d in st.get("disks", []):
                free = d["free"] / d["total"] * 100 if d["total"] else 100
                t = f"DISK_LOW_{d['name'].rstrip(':')}"
                if self._below(f"{src}:{t}", free, c["disk_low"], c["disk_low_clear"]):
                    add(Condition(src, t, "warning",
                                  f"電腦 {host} 磁碟 {d['name']} 剩餘空間不足：{free:.1f}%",
                                  {"free_pct": round(free, 1)}))

        self.prev = set(out)
        return out
