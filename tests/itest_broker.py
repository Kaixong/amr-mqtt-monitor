"""Integration tests against the running stack:  ./run.sh itest
(needs ./run.sh start first; uses the passwords from .env and certs/ca.crt)"""
import json
import os
import queue
import socket
import ssl
import sys
import time
import unittest
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

ROOT = Path(__file__).resolve().parent.parent
CA = str(ROOT / "certs" / "ca.crt")
HOST = "localhost"


def pw(user):
    return os.environ[f"MQTT_PASS_{user.upper()}"]


class Conn:
    """Small synchronous wrapper: connect, wait for CONNACK, collect messages in a queue."""

    def __init__(self, user=None, password=None, port=8883, transport="tcp", tls=True,
                 client_id=None, clean_start=True, session_expiry=None):
        self.msgs = queue.Queue()
        self.connack = queue.Queue()
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=client_id or f"itest-{uuid.uuid4().hex[:8]}",
                             protocol=mqtt.MQTTv5, transport=transport)
        if user:
            self.c.username_pw_set(user, password if password is not None else pw(user))
        if tls:
            self.c.tls_set(ca_certs=CA, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.c.on_connect = lambda c, u, f, rc, p: self.connack.put((rc, f))
        self.c.on_message = lambda c, u, m: self.msgs.put(m)
        props = Properties(PacketTypes.CONNECT)
        if session_expiry:
            props.SessionExpiryInterval = session_expiry
        self.c.connect(HOST, port, keepalive=10, clean_start=clean_start, properties=props)
        self.c.loop_start()
        self.rc, self.flags = self.connack.get(timeout=5)

    def sub(self, topic, qos=1):
        self.c.subscribe(topic, qos)
        time.sleep(0.3)
        return self

    def get(self, timeout=3):
        return self.msgs.get(timeout=timeout)

    def none_matching(self, pred, wait=2.0):
        end = time.time() + wait
        while time.time() < end:
            try:
                if pred(self.msgs.get(timeout=end - time.time())):
                    return False
            except queue.Empty:
                break
        return True

    def close(self):
        self.c.disconnect()
        self.c.loop_stop()


def cmd_props(response_topic=None, corr=None, expiry=5):
    p = Properties(PacketTypes.PUBLISH)
    p.MessageExpiryInterval = expiry
    if response_topic:
        p.ResponseTopic = response_topic
    if corr:
        p.CorrelationData = corr
    return p


class Transport(unittest.TestCase):
    def test_plaintext_port_1883_is_closed(self):
        with self.assertRaises(OSError):
            socket.create_connection((HOST, 1883), timeout=2).close()

    def test_tls_rejects_client_without_our_ca(self):
        ctx = ssl.create_default_context()      # system CAs only
        with socket.create_connection((HOST, 8883), timeout=3) as s:
            with self.assertRaises(ssl.SSLCertVerificationError):
                ctx.wrap_socket(s, server_hostname=HOST)

    def test_tls_certificate_is_for_localhost(self):
        ctx = ssl.create_default_context(cafile=CA)
        with socket.create_connection((HOST, 8883), timeout=3) as s:
            with ctx.wrap_socket(s, server_hostname=HOST) as t:
                self.assertIn(t.version(), ("TLSv1.2", "TLSv1.3"))

    def test_websocket_9001_and_secure_websocket_9443(self):
        ws = Conn("dashboard", port=9001, transport="websockets", tls=False)
        wss = Conn("dashboard", port=9443, transport="websockets", tls=True)
        self.assertFalse(ws.rc.is_failure)
        self.assertFalse(wss.rc.is_failure)
        ws.close(), wss.close()


class Auth(unittest.TestCase):
    def test_anonymous_is_refused(self):
        c = Conn()
        self.assertTrue(c.rc.is_failure, c.rc)
        c.close()

    def test_wrong_password_is_refused(self):
        c = Conn("dashboard", password="wrong-password")
        self.assertTrue(c.rc.is_failure, c.rc)
        c.close()

    def test_dashboard_cannot_spoof_amr_status(self):
        c = Conn("dashboard").sub("amr/AMR-01/status", 0)
        c.c.publish("amr/AMR-01/status", '{"FAKE": 1}')
        self.assertTrue(c.none_matching(lambda m: b"FAKE" in m.payload))
        c.close()

    def test_alarmd_cannot_send_amr_commands(self):
        watch = Conn("dashboard").sub("amr/AMR-01/cmd/result")
        bad = Conn("alarmd")
        bad.c.publish("amr/AMR-01/cmd", "pause", qos=1)
        self.assertTrue(watch.none_matching(lambda m: True))
        watch.close(), bad.close()


class Mqtt5(unittest.TestCase):
    def test_retained_status_carries_v5_properties(self):
        c = Conn("dashboard").sub("amr/AMR-01/status", 0)
        m = c.get()
        self.assertTrue(m.retain)
        self.assertEqual(m.properties.ContentType, "application/json")
        self.assertEqual(m.properties.PayloadFormatIndicator, 1)
        self.assertTrue(0 < m.properties.MessageExpiryInterval <= 24 * 3600)
        c.close()

    def test_command_reply_echoes_correlation_data(self):
        c = Conn("dashboard").sub("amr/AMR-02/cmd/result")
        corr = f"itest-{uuid.uuid4().hex[:6]}".encode()
        c.c.publish("amr/AMR-02/cmd", '{"cmd": "pause"}', qos=1,
                    properties=cmd_props("amr/AMR-02/cmd/result", corr))
        m = c.get(timeout=5)
        self.assertEqual(m.properties.CorrelationData, corr)
        self.assertIn(b'"cmd": "pause"', m.payload)
        c.c.publish("amr/AMR-02/cmd", "resume", qos=1)
        c.close()

    def test_foreign_response_topic_is_not_used(self):
        c = Conn("dashboard").sub("amr/AMR-02/cmd/result").sub("amr/AMR-01/cmd/result")
        c.c.publish("amr/AMR-02/cmd", '{"cmd": "noop"}', qos=1,
                    properties=cmd_props("amr/AMR-01/cmd/result", b"x"))
        m = c.get(timeout=5)
        self.assertEqual(m.topic, "amr/AMR-02/cmd/result")
        c.close()

    def test_message_expiry_drops_stale_commands_in_persistent_session(self):
        cid = f"itest-session-{uuid.uuid4().hex[:6]}"
        topic = "amr/ITEST/cmd"
        s = Conn("dashboard", client_id=cid, clean_start=False, session_expiry=60).sub(topic)
        s.close()                                   # offline, but the broker keeps the session
        pub = Conn("dashboard")
        pub.c.publish(topic, "stale", qos=1, properties=cmd_props(expiry=1)).wait_for_publish(3)
        pub.c.publish(topic, "fresh", qos=1, properties=cmd_props(expiry=60)).wait_for_publish(3)
        pub.close()
        time.sleep(2.5)                             # "stale" expires while we are away
        s = Conn("dashboard", client_id=cid, clean_start=False, session_expiry=60)
        self.assertTrue(s.flags.session_present)
        got = []
        try:
            while True:
                got.append(s.get(timeout=2).payload)
        except queue.Empty:
            pass
        self.assertEqual(got, [b"fresh"])
        s.close()
        Conn("dashboard", client_id=cid, clean_start=True).close()   # drop the session

    def test_alarm_state_is_retained_json(self):
        c = Conn("dashboard").sub("alarms/active")
        m = c.get()
        self.assertTrue(m.retain)
        self.assertEqual(m.properties.ContentType, "application/json")
        c.close()

    def test_recorder_publishes_utilization(self):
        c = Conn("dashboard").sub("stats/utilization")
        m = c.get(timeout=12)                      # retained, or the next one within 10 s
        self.assertEqual(m.properties.ContentType, "application/json")
        u = json.loads(m.payload)["windows"]["3600"]
        self.assertIn("AMR-01", u)
        self.assertTrue(0 <= u["AMR-01"]["utilization"] <= 100)
        self.assertAlmostEqual(sum(u["AMR-01"]["states"].values()), 100, delta=1)
        c.close()

    def test_dashboard_cannot_spoof_utilization(self):
        c = Conn("dashboard").sub("stats/utilization")
        c.c.publish("stats/utilization", '{"FAKE": 1}', qos=1)
        self.assertTrue(c.none_matching(lambda m: b"FAKE" in m.payload))
        c.close()

    def test_broker_statistics_are_readable(self):
        c = Conn("dashboard").sub("$SYS/broker/version", 0)
        self.assertIn(b"mosquitto", c.get(timeout=7).payload)
        c.close()


if __name__ == "__main__":
    sys.exit(unittest.main())
