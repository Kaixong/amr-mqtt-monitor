"""Run a fleet of simulated AMRs, one MQTT v5 client each (so each has its own Last Will).

Topics:
  amr/<id>/status      JSON status, retained, QoS 0, every tick
  amr/<id>/online      "online" / "offline" (retained, QoS 1, Last Will)
  amr/<id>/cmd         pause | resume | charge | clear | go  (QoS 1)
                       plain text, or JSON {"cmd": "...", "req_id": "..."}
                       simulate_netloss: test hook, the AMR goes silent without a clean
                       DISCONNECT (like a dropped network); the broker then fires its Last Will
                       after 1.5 x keepalive. Restart the simulator to bring it back.
  amr/<id>/cmd/result  {"req_id", "cmd", "ok", "state"} reply (QoS 1), echoing the request's
                       MQTT v5 Correlation Data. This is the only Response Topic accepted.
JSON messages carry Content Type + Payload Format Indicator; retained ones expire after 24 h.

Each AMR keeps a persistent session (Session Expiry 60 s), so a command sent during a short
network drop is delivered on reconnect - unless the sender's Message Expiry ran out first.
"""
import argparse
import json
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.mqttutil import (TELEMETRY_EXPIRY, add_args, connect, connected_ok,  # noqa: E402
                             json_props, make_client, text_props)
from paho.mqtt.packettypes import PacketTypes  # noqa: E402

from amr import AMR, CHARGERS  # noqa: E402

KEEPALIVE = 5          # broker declares us dead after 1.5 x keepalive
SESSION_EXPIRY = 60
STATUS_PROPS = json_props(expiry=TELEMETRY_EXPIRY)
ONLINE_PROPS = text_props(expiry=TELEMETRY_EXPIRY)


def parse_cmd(payload):
    text = payload.decode(errors="replace").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return str(data.get("cmd", "")).lower(), data.get("req_id")
    except json.JSONDecodeError:
        pass
    return text.lower(), None


def make_amr_client(amr, host, port):
    base = f"amr/{amr.id}"
    c = make_client(f"sim-{amr.id}", "simulator")
    c.will_set(f"{base}/online", "offline", qos=1, retain=True,
               properties=text_props(TELEMETRY_EXPIRY, PacketTypes.WILLMESSAGE))

    def on_connect(client, userdata, flags, reason_code, properties):
        if not connected_ok(f"sim-{amr.id}", reason_code):
            return
        client.publish(f"{base}/online", "online", qos=1, retain=True, properties=ONLINE_PROPS)
        client.subscribe(f"{base}/cmd", qos=1)
        print(f"[{amr.id}] connected ({reason_code}, session_present={flags.session_present})")

    def on_message(client, userdata, msg):
        cmd, req_id = parse_cmd(msg.payload)
        if cmd == "simulate_netloss":
            amr.netloss = True  # main loop stops this client's network loop
            print(f"[{amr.id}] simulating network loss")
            return
        ok = amr.command(cmd)
        print(f"[{amr.id}] cmd={cmd!r} -> {'ok' if ok else 'ignored'} (state={amr.state})")

        # reply on the requested Response Topic, echoing its Correlation Data. Only our own
        # result topic is accepted (the ACL would drop anything else anyway).
        req = msg.properties
        reply_topic = f"{base}/cmd/result"
        if getattr(req, "ResponseTopic", None) not in (None, reply_topic):
            print(f"[{amr.id}] ignoring response topic {req.ResponseTopic!r}")
        props = json_props()
        if getattr(req, "CorrelationData", None):
            props.CorrelationData = req.CorrelationData
        result = {"req_id": req_id, "cmd": cmd, "ok": ok, "state": amr.state,
                  "ts": round(time.time(), 3)}
        client.publish(reply_topic, json.dumps(result), qos=1, properties=props)

    c.on_connect = on_connect
    c.on_message = on_message
    connect(c, host, port, KEEPALIVE, session_expiry=SESSION_EXPIRY)
    return c


def main():
    ap = argparse.ArgumentParser()
    add_args(ap)
    ap.add_argument("-n", "--count", type=int, default=5)
    ap.add_argument("--hz", type=float, default=2.0, help="ticks per second")
    args = ap.parse_args()

    spots = list(CHARGERS.values())
    fleet = [AMR(f"AMR-{i + 1:02d}", spots[i % len(spots)][0] + i // len(spots) * 1.5,
                 spots[i % len(spots)][1] - 2) for i in range(args.count)]
    clients = {a.id: make_amr_client(a, args.host, args.port) for a in fleet}

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    dt = 1.0 / args.hz
    print(f"simulating {len(fleet)} AMRs at {args.hz} Hz -> {args.host}:{args.port}")
    while running:
        t0 = time.time()
        for a in fleet:
            a.step(dt)
            if getattr(a, "netloss", False):
                if not getattr(a, "silenced", False):
                    # no DISCONNECT and no more PINGREQs: the socket just goes quiet
                    clients[a.id].loop_stop()
                    a.silenced = True
                continue
            clients[a.id].publish(f"amr/{a.id}/status", json.dumps(a.status()), retain=True,
                                  properties=STATUS_PROPS)
        time.sleep(max(0.0, dt - (time.time() - t0)))

    for a in fleet:
        if getattr(a, "silenced", False):
            continue
        c = clients[a.id]
        try:
            c.publish(f"amr/{a.id}/online", "offline", qos=1, retain=True,
                      properties=ONLINE_PROPS).wait_for_publish(2)
        except (RuntimeError, ValueError):
            pass  # not connected: the broker will publish the Last Will instead
        c.loop_stop()
        c.disconnect()
    print("stopped")


if __name__ == "__main__":
    main()
