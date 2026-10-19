"""Shared MQTT v5 client setup: credentials from the environment, reconnect backoff."""
import os
import sys

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties


def make_client(client_id, user):
    """MQTT v5 client logged in as `user` (password from MQTT_PASS_<USER>, see .env)."""
    password = os.environ.get(f"MQTT_PASS_{user.upper()}")
    if not password:
        sys.exit(f"MQTT_PASS_{user.upper()} not set: run ./scripts/setup-auth.sh "
                 "and start the services with ./run.sh")
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                    protocol=mqtt.MQTTv5)
    c.username_pw_set(user, password)
    c.reconnect_delay_set(min_delay=1, max_delay=30)
    return c


def connect(client, host, port, keepalive, session_expiry=None):
    """Connect in the background. With session_expiry (s) the broker keeps the session
    (subscriptions + queued QoS1 messages) across short disconnects."""
    props = Properties(PacketTypes.CONNECT)
    if session_expiry:
        props.SessionExpiryInterval = session_expiry
    client.connect_async(host, port, keepalive=keepalive,
                         clean_start=not session_expiry, properties=props)
    client.loop_start()


def publish_props(**kw):
    """PUBLISH properties, e.g. publish_props(MessageExpiryInterval=5)."""
    props = Properties(PacketTypes.PUBLISH)
    for k, v in kw.items():
        setattr(props, k, v)
    return props


def add_args(ap):
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", 1883)))
