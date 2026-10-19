"""Shared MQTT v5 client setup: TLS, credentials from the environment, reconnect backoff,
CONNACK error reporting and PUBLISH property helpers."""
import os
import ssl
import sys
from pathlib import Path

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

ROOT = Path(__file__).resolve().parent.parent
CA_FILE = os.environ.get("MQTT_CA", str(ROOT / "certs" / "ca.crt"))

# retained telemetry disappears from the broker if its publisher is gone for a day,
# so decommissioned AMRs / hosts don't linger forever
TELEMETRY_EXPIRY = 24 * 3600


def make_client(client_id, user):
    """MQTT v5 client over TLS, logged in as `user` (password from MQTT_PASS_<USER>, see .env)."""
    password = os.environ.get(f"MQTT_PASS_{user.upper()}")
    if not password:
        sys.exit(f"MQTT_PASS_{user.upper()} not set: run ./scripts/setup-auth.sh "
                 "and start the services with ./run.sh")
    if not Path(CA_FILE).is_file():
        sys.exit(f"{CA_FILE} not found: run ./scripts/setup-tls.sh")
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                    protocol=mqtt.MQTTv5)
    c.username_pw_set(user, password)
    # verifies the broker's certificate (and that it was issued for localhost)
    c.tls_set(ca_certs=CA_FILE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    c.reconnect_delay_set(min_delay=1, max_delay=30)
    return c


def connected_ok(client_id, reason_code):
    """Call first in on_connect. Logs why the broker refused us (MQTT v5 reason code)."""
    if not reason_code.is_failure:
        return True
    hint = {"Not authorized": "password wrong or rotated? restart via ./run.sh restart",
            "Bad user name or password": "password wrong or rotated? restart via ./run.sh restart",
            "Client identifier not valid": "duplicate or invalid client id"}.get(str(reason_code), "")
    print(f"[{client_id}] broker refused connection: {reason_code}"
          + (f" ({hint})" if hint else "") + " - retrying with backoff",
          file=sys.stderr, flush=True)
    return False


def connect(client, host, port, keepalive, session_expiry=None):
    """Connect in the background. With session_expiry (s) the broker keeps the session
    (subscriptions + queued QoS1 messages) across short disconnects."""
    props = Properties(PacketTypes.CONNECT)
    if session_expiry:
        props.SessionExpiryInterval = session_expiry
    client.connect_async(host, port, keepalive=keepalive,
                         clean_start=not session_expiry, properties=props)
    client.loop_start()


def publish_props(packet=PacketTypes.PUBLISH, **kw):
    """PUBLISH (or WILLMESSAGE) properties, e.g. publish_props(MessageExpiryInterval=5)."""
    props = Properties(packet)
    for k, v in kw.items():
        setattr(props, k, v)
    return props


def json_props(expiry=None, packet=PacketTypes.PUBLISH):
    """Properties for a JSON payload: UTF-8 indicator + content type (+ message expiry)."""
    kw = {"PayloadFormatIndicator": 1, "ContentType": "application/json"}
    if expiry:
        kw["MessageExpiryInterval"] = expiry
    return publish_props(packet, **kw)


def text_props(expiry=None, packet=PacketTypes.PUBLISH):
    kw = {"PayloadFormatIndicator": 1, "ContentType": "text/plain"}
    if expiry:
        kw["MessageExpiryInterval"] = expiry
    return publish_props(packet, **kw)


def add_args(ap):
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", 8883)))
