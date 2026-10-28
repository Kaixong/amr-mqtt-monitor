"""Recorder: logs every AMR state change to SQLite and publishes fleet utilization.

Subscribes:
  amr/+/status, amr/+/online
Publishes (QoS 1):
  stats/utilization  retained  {"ts", "since", "windows": {"3600": {...}, "86400": {...}}}
                     per window: {amr: {"total": s, "utilization": %, "states": {state: %}}}
                     utilization = share of observed time spent "moving" (driving a task)
  stats/recorder/online        "online" / "offline" (Last Will)
State intervals are stored in data/telemetry.db (SQLite), kept for KEEP_DAYS.
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
from common.mqttutil import (add_args, connect, connected_ok, json_props,  # noqa: E402
                             make_client, text_props)
from paho.mqtt.packettypes import PacketTypes  # noqa: E402

from statedb import StateDB  # noqa: E402
from tracker import StateTracker, utilization  # noqa: E402

WINDOWS = (3600, 86400)      # s: last hour, last 24 h
PUBLISH_EVERY = 10.0         # s
KEEP_DAYS = 30
JSON = json_props()


class Recorder:
    def __init__(self, args):
        (ROOT / "data").mkdir(exist_ok=True)
        self.db = StateDB(args.db)
        self.db.prune(time.time() - KEEP_DAYS * 86400)
        self.tracker = StateTracker()
        self.lock = threading.Lock()

        self.client = make_client("recorder", "recorder")
        self.client.will_set("stats/recorder/online", "offline", qos=1, retain=True,
                             properties=text_props(packet=PacketTypes.WILLMESSAGE))
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        connect(self.client, args.host, args.port, keepalive=10)

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if not connected_ok("recorder", reason_code):
            return
        print(f"connected ({reason_code})", flush=True)
        client.publish("stats/recorder/online", "online", qos=1, retain=True,
                       properties=text_props())
        client.subscribe([("amr/+/status", 0), ("amr/+/online", 1)])

    def on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        now = time.time()
        try:
            text = msg.payload.decode()
            with self.lock:
                if parts[2] == "status":
                    s = json.loads(text)
                    closed = self.tracker.observe(parts[1], s["state"], s.get("ts", now), now)
                else:
                    closed = self.tracker.set_online(parts[1], text == "online", now, msg.retain)
                self.db.add(closed)
        except (ValueError, KeyError, IndexError) as e:
            print(f"bad message on {msg.topic}: {e}", file=sys.stderr, flush=True)

    def tick(self):
        with self.lock:
            self.db.add(self.tracker.tick(time.time()))

    def publish(self):
        now = time.time()
        with self.lock:
            windows = {}
            for w in WINDOWS:
                d = self.tracker.add_open(self.db.durations(now - w, now), now - w, now)
                windows[str(w)] = utilization(d)
            # oldest data point, so the dashboard can say "only N minutes of data so far"
            starts = [s for _, s in self.tracker.cur.values()] + [self.db.first_record()]
            since = min((s for s in starts if s is not None), default=None)
        self.client.publish("stats/utilization",
                            json.dumps({"ts": round(now, 3), "since": since, "windows": windows}),
                            qos=1, retain=True, properties=JSON)

    def close(self):
        with self.lock:
            self.db.add(self.tracker.close_all(time.time()))


def main():
    ap = argparse.ArgumentParser()
    add_args(ap)
    ap.add_argument("--db", default=str(ROOT / "data" / "telemetry.db"))
    args = ap.parse_args()

    rec = Recorder(args)
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print("recorder started", flush=True)
    next_publish = time.time() + 3   # let retained + first live statuses arrive
    while running:
        rec.tick()
        if time.time() >= next_publish:
            rec.publish()
            next_publish = time.time() + PUBLISH_EVERY
        time.sleep(0.5)

    rec.close()
    try:
        rec.client.publish("stats/recorder/online", "offline", qos=1, retain=True,
                           properties=text_props()).wait_for_publish(2)
    except (RuntimeError, ValueError):
        pass
    rec.client.loop_stop()
    rec.client.disconnect()
    print("stopped")


if __name__ == "__main__":
    main()
