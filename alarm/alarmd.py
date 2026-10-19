"""Alarm service: watches AMR + PC telemetry, records anomalies, publishes alarm state.

Subscribes:
  amr/+/status, amr/+/online, system/+/stats, system/+/online
  alarms/ack                {"id": 12, "by": "..."} or {"all": true, "by": "..."}
Publishes (QoS 1):
  alarms/event              {"action": "raised"|"cleared"|"acked", "alarm": {...}}
  alarms/active   retained  {"ts", "alarms": [...]}  alarms whose condition is still true
  alarms/history  retained  {"ts", "alarms": [...]}  latest HISTORY_SIZE alarms, newest first
  alarms/service/online     "online" / "offline" (Last Will)
Every alarm is stored in data/alarms.db (SQLite).
"""
import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.mqttutil import add_args, connect, make_client  # noqa: E402

from rules import AlarmEngine  # noqa: E402
from store import AlarmStore  # noqa: E402

HISTORY_SIZE = 300
# After (re)connecting, wait before judging anything: retained messages need to arrive, and
# after a broker outage every device needs time to reconnect. Without this a broker restart
# would raise an OFFLINE alarm for every AMR although the gap was the broker's, not theirs.
GRACE = 10.0


class AlarmService:
    def __init__(self, args):
        (ROOT / "data").mkdir(exist_ok=True)
        self.store = AlarmStore(args.db)
        self.open = self.store.open_alarms()          # key -> alarm row
        self.engine = AlarmEngine(active_keys=self.open.keys())
        self.lock = threading.Lock()
        self.connected_at = None   # None while disconnected from the broker

        self.client = make_client("alarmd", "alarmd")
        self.client.will_set("alarms/service/online", "offline", qos=1, retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message
        connect(self.client, args.host, args.port, keepalive=10)

    # ---------- MQTT ----------
    def on_connect(self, client, userdata, flags, reason_code, properties):
        print(f"connected ({reason_code})", flush=True)
        client.publish("alarms/service/online", "online", qos=1, retain=True)
        client.subscribe([("amr/+/status", 0), ("amr/+/online", 1),
                          ("system/+/stats", 0), ("system/+/online", 1), ("alarms/ack", 1)])
        with self.lock:
            self.connected_at = time.time()
            self.publish_state()

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        print(f"disconnected ({reason_code}), pausing evaluation", flush=True)
        self.connected_at = None

    def on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        now = time.time()
        try:
            text = msg.payload.decode()
            with self.lock:
                if parts[0] == "amr" and parts[2] == "status":
                    self.engine.on_amr_status(parts[1], json.loads(text), now)
                elif parts[0] == "amr" and parts[2] == "online":
                    self.engine.on_amr_online(parts[1], text == "online")
                elif parts[0] == "system" and parts[2] == "stats":
                    self.engine.on_sys_stats(parts[1], json.loads(text), now)
                elif parts[0] == "system" and parts[2] == "online":
                    self.engine.on_sys_online(parts[1], text == "online")
                elif msg.topic == "alarms/ack":
                    self.handle_ack(json.loads(text), now)
        except (ValueError, KeyError, IndexError) as e:
            print(f"bad message on {msg.topic}: {e}", file=sys.stderr, flush=True)

    def emit(self, action, alarm):
        self.client.publish("alarms/event", json.dumps({"action": action, "alarm": alarm},
                                                       ensure_ascii=False), qos=1)
        print(f"{action:8} #{alarm['id']} [{alarm['severity']}] {alarm['message']}", flush=True)

    def publish_state(self):
        now = round(time.time(), 3)
        active = sorted(self.open.values(), key=lambda a: a["raised_at"], reverse=True)
        self.client.publish("alarms/active", json.dumps({"ts": now, "alarms": active},
                                                        ensure_ascii=False), qos=1, retain=True)
        self.client.publish("alarms/history",
                            json.dumps({"ts": now, "alarms": self.store.recent(HISTORY_SIZE)},
                                       ensure_ascii=False), qos=1, retain=True)

    # ---------- logic ----------
    def handle_ack(self, req, now):
        by = str(req.get("by", "unknown"))[:40]
        ids = None if req.get("all") else [int(req["id"])]
        acked = self.store.ack(ids, by, now)
        for a in acked:
            if a["key"] in self.open and self.open[a["key"]]["id"] == a["id"]:
                self.open[a["key"]] = a
            self.emit("acked", a)
        if acked:
            self.publish_state()

    def tick(self):
        now = time.time()
        if self.connected_at is None or now - self.connected_at < GRACE:
            return
        with self.lock:
            current = self.engine.evaluate(now)
            changed = False
            for key, cond in current.items():
                if cond is not None and key not in self.open:
                    self.open[key] = self.store.raise_(cond, now)
                    self.emit("raised", self.open[key])
                    changed = True
            for key in [k for k in self.open if k not in current]:
                alarm = self.store.clear(self.open.pop(key)["id"], now)
                self.emit("cleared", alarm)
                changed = True
            if changed:
                self.publish_state()


def main():
    ap = argparse.ArgumentParser()
    add_args(ap)
    ap.add_argument("--db", default=str(ROOT / "data" / "alarms.db"))
    args = ap.parse_args()

    svc = AlarmService(args)
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(f"alarm service started, {len(svc.open)} alarm(s) still open from before", flush=True)
    while running:
        svc.tick()
        time.sleep(0.5)

    try:
        svc.client.publish("alarms/service/online", "offline", qos=1,
                           retain=True).wait_for_publish(2)
    except (RuntimeError, ValueError):
        pass
    svc.client.loop_stop()
    svc.client.disconnect()
    print("stopped")


if __name__ == "__main__":
    main()
