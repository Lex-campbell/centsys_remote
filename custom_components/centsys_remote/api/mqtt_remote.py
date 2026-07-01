"""Gate control and live telemetry over MQTT (mutual TLS).

A SMART Wi-Fi operator is controlled over a cloud MQTT broker (mTLS, MQTT v5),
not HTTP. The open command is a short challenge-response; all topics are
prefixed with the long operator serial:

    ->  connectionRequest          (empty, QoS 2)
    <-  connectionRequestResponse  0xff               ("device present")
    ->  userRemoteTrigger  cmd 01  (identity packet)
    <-  ...Response        cmd 02  (... + 4-byte challenge)
    ->  userRemoteTrigger  cmd 05  (the command code)
    <-  ...Response        cmd 06
    ->  userRemoteTrigger  cmd 03  (... + the challenge echoed back)
    <-  ...Response        cmd 04                     -> gate moves
    ->  disconnect         (empty)                    (release the device)

Key details:
  * Connect as MQTT v5 with clientId "mcr:<number>" and attach that same value
    as a "ClientId" user property + a ResponseTopic on every publish (the gate
    uses these to know who is talking and where to reply).
  * The gate issues a fresh 4-byte challenge in cmd 02 each session and only
    validates that cmd 03 echoes it; the 4-byte nonces are not validated.

These functions are blocking; call them from an executor (see
CentsysRemoteClient.open_gate / get_overview / follow_overview).
"""

from __future__ import annotations

import base64
import logging
import os
import ssl
import struct
import tempfile
import threading
from dataclasses import asdict, dataclass

_LOGGER = logging.getLogger(__name__)


# --- deviceOverview telemetry -------------------------------------------------
#
# The gate publishes a binary blob on "<serial>/deviceOverview". The first 4
# bytes are a header; the rest is a packed, little-endian struct whose shape
# depends on the operator family:
#
#   * Slider-Plus operators (e.g. D5 Evo)  -> INFRATP_OVERVIEW_V2   (36 bytes)
#   * other "...Plus" gate operators        -> INFRATP_OVERVIEW_VX   (38 bytes)
#   * garage-door operators                 -> APPMOBILE_STATUS_..V3 (24 bytes)
#
# We auto-detect by the post-header length and decode the fields below.

_GATE_STATUS = {
    0: "open",
    1: "closed",
    2: "partly_open",
    3: "partly_closed",
    4: "opening",
    5: "closing",
}
_POWER_STATUS = {0: "normal", 1: "low", 2: "unknown", 3: "psu_comms_off"}


def _beam_label(value: int) -> str:
    """Collapse an APPBEAM_DISPLAY value to a simple beam condition."""
    if value == 0:
        return "disabled"
    if value == 1:
        return "never_activated"
    if value == 255:
        return "unknown"
    if 2 <= value <= 6:
        return "clear"
    if 7 <= value <= 11:
        return "obstructed"
    if 12 <= value <= 15:
        return "not_connected"
    if 16 <= value <= 20:
        return "wiring_error"
    return "unknown"


@dataclass
class DeviceOverview:
    """Decoded live telemetry from a "<serial>/deviceOverview" MQTT message."""

    family: str  # "v2" | "vx" | "sdo5"
    gate_status: str | None
    gate_status_raw: int
    battery_voltage: float | None  # volts
    battery_voltage_raw: int
    input_voltage: float | None  # volts (mains/solar feed), best-effort
    input_voltage_raw: int
    temperature_c: int | None
    power_status: str | None
    power_status_raw: int
    opening_beam: str | None
    opening_beam_raw: int
    closing_beam: str | None
    closing_beam_raw: int
    seconds_remaining: int
    gate_position: int | None  # percent, slider-only
    notification_flags: int  # (flags1 << 32) | flags2
    condition_flags: int

    def as_dict(self) -> dict:
        return asdict(self)


def parse_device_overview(payload: bytes) -> DeviceOverview:
    """Decode a raw deviceOverview MQTT payload into structured telemetry.

    ``payload`` is the full MQTT payload; the leading 4-byte header is stripped
    here (matching the app). Raises ValueError if the body length is unknown.
    """
    body = bytes(payload)[4:]
    n = len(body)

    if n >= 36 and n < 38:  # INFRATP_OVERVIEW_V2 (slider-plus, e.g. D5 Evo)
        (
            batt,
            gate_pos,
            temp,
            nf1,
            nf2,
            cond,
            secs,
            _timer,
            gate_st,
            irbo,
            irbc,
            _xmr,
            power,
        ) = struct.unpack_from("<HBBIIIIHBBBBB", body, 0)
        in_v = struct.unpack_from("<H", body, 34)[0]
        family, gate_position = "v2", gate_pos
    elif n >= 38:  # INFRATP_OVERVIEW_VX
        (
            _ver,
            temp,
            batt,
            nf1,
            nf2,
            cond,
            secs,
            _gm,
            _gs,
            gate_st,
            irbo,
            irbc,
        ) = struct.unpack_from("<BBHIIIIBBBBB", body, 0)
        power = body[28]
        in_v = struct.unpack_from("<H", body, 36)[0]
        family, gate_position = "vx", None
    elif n >= 24:  # APPMOBILE_STATUS_OVERVIEWV3 (garage door)
        nf1, batt, cond, _pad, secs, _timer, _pad2, gate_st, irbc, _xmr, power = (
            struct.unpack_from("<IHBBHBBBBBB", body, 0)
        )
        nf2, temp, irbo, in_v = 0, None, 0, 0
        family, gate_position = "sdo5", None
    else:
        raise ValueError(f"unrecognized deviceOverview length: {n} bytes")

    temp_c = temp if temp is None else (temp - 256 if temp > 127 else temp)
    return DeviceOverview(
        family=family,
        gate_status=_GATE_STATUS.get(gate_st),
        gate_status_raw=gate_st,
        battery_voltage=round(batt / 100.0, 2) if batt else None,
        battery_voltage_raw=batt,
        input_voltage=round(in_v / 100.0, 2) if in_v else None,
        input_voltage_raw=in_v,
        temperature_c=temp_c,
        power_status=_POWER_STATUS.get(power),
        power_status_raw=power,
        opening_beam=_beam_label(irbo),
        opening_beam_raw=irbo,
        closing_beam=_beam_label(irbc),
        closing_beam_raw=irbc,
        seconds_remaining=secs,
        gate_position=gate_position,
        notification_flags=(nf1 << 32) | nf2,
        condition_flags=cond,
    )


def pfx_to_pem(pfx_b64: str, password: str) -> tuple[bytes, bytes]:
    """Convert a base64 PKCS#12 blob into (cert_pem, key_pem) byte strings."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    raw = base64.b64decode(pfx_b64)
    pwd = password.encode() if password else None
    key, cert, _extra = pkcs12.load_key_and_certificates(raw, pwd)
    if cert is None or key is None:
        raise ValueError("PKCS#12 blob missing certificate or private key")
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def open_gate_blocking(
    *,
    host: str,
    port: int,
    client_id: str,
    serial: str,
    cert_pem: bytes,
    key_pem: bytes,
    cmd01: bytes,
    cmd05: bytes,
    cmd03_prefix: bytes,
    timeout: float = 8.0,
) -> bool:
    """Run the full open handshake. Returns True if the gate acked cmd 03.

    Blocking (uses paho's loop in a background thread internally). Intended to be
    run via ``loop.run_in_executor`` from async code.
    """
    import paho.mqtt.client as mqtt
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties

    t_req = f"{serial}/connectionRequest"
    t_req_resp = f"{serial}/connectionRequestResponse"
    t_trig = f"{serial}/userRemoteTrigger"
    t_trig_resp = f"{serial}/userRemoteTriggerResponse"
    t_disc = f"{serial}/disconnect"

    subscribed = threading.Event()
    conn_resp = threading.Event()
    trig_q: list[bytes] = []
    trig_evt = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe([(t_req_resp, 0), (t_trig_resp, 0)])

    def on_subscribe(client, userdata, mid, reason_codes, properties=None):
        subscribed.set()

    def on_message(client, userdata, msg):
        _LOGGER.debug("MQTT <- %s (%dB) %s", msg.topic, len(msg.payload), msg.payload.hex(" "))
        if msg.topic == t_req_resp:
            conn_resp.set()
        elif msg.topic == t_trig_resp:
            trig_q.append(msg.payload)
            trig_evt.set()

    def wait_trig() -> bytes | None:
        trig_evt.wait(timeout)
        trig_evt.clear()
        return trig_q.pop(0) if trig_q else None

    def props(response_topic: str) -> "Properties":
        p = Properties(PacketTypes.PUBLISH)
        p.ResponseTopic = response_topic
        p.UserProperty = [("ClientId", client_id)]
        return p

    # paho's SSLContext.load_cert_chain needs files; write short-lived ones.
    cert_file = key_file = None
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv5,
    )
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    try:
        fd_c, cert_file = tempfile.mkstemp(suffix=".pem")
        os.write(fd_c, cert_pem)
        os.close(fd_c)
        fd_k, key_file = tempfile.mkstemp(suffix=".pem")
        os.write(fd_k, key_pem)
        os.close(fd_k)

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
        client.tls_set_context(ctx)

        client.connect(host, port, keepalive=30, clean_start=True)
        client.loop_start()

        if not subscribed.wait(timeout):
            _LOGGER.warning("MQTT open: subscriptions never confirmed")
            return False

        client.publish(t_req, b"", qos=2, properties=props(t_req_resp))
        if not conn_resp.wait(timeout):
            _LOGGER.warning("MQTT open: no connectionRequestResponse (gate offline?)")
            return False

        client.publish(t_trig, cmd01, qos=0, properties=props(t_trig_resp))
        cmd02 = wait_trig()
        if not cmd02 or len(cmd02) < 4:
            _LOGGER.warning("MQTT open: no/short cmd 02 response")
            return False
        challenge = cmd02[-4:]
        _LOGGER.debug("MQTT open: challenge %s", challenge.hex(" "))

        client.publish(t_trig, cmd05, qos=0, properties=props(t_trig_resp))
        wait_trig()

        client.publish(t_trig, cmd03_prefix + challenge, qos=0, properties=props(t_trig_resp))
        cmd04 = wait_trig()
        _LOGGER.debug("MQTT open: final response %s", cmd04.hex(" ") if cmd04 else None)
        return cmd04 is not None
    finally:
        try:
            client.publish(t_disc, b"", qos=0, properties=props(t_disc))
        except Exception:  # noqa: BLE001 - best-effort release
            pass
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        for f in (cert_file, key_file):
            if f and os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass


def follow_overview_blocking(
    *,
    host: str,
    port: int,
    client_id: str,
    serial: str,
    cert_pem: bytes,
    key_pem: bytes,
    on_overview,
    duration: float,
    wake_cmd01: bytes | None = None,
    connect_timeout: float = 15.0,
) -> None:
    """Stream live telemetry for ``duration`` seconds, one callback per frame.

    Connects, wakes the operator, then stays subscribed to
    ``<serial>/deviceOverview`` and invokes ``on_overview(DeviceOverview)`` for
    every broadcast until ``duration`` elapses. Used to follow an open/close
    cycle in real time (the gate streams ~1/sec while moving).

    ``on_overview`` is called from the MQTT network thread; keep it cheap and
    marshal back to your event loop (e.g. ``loop.call_soon_threadsafe``).

    Blocking; run via ``loop.run_in_executor``. Best-effort: connection issues
    are logged and end the follow rather than raising.
    """
    import paho.mqtt.client as mqtt
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties

    t_req = f"{serial}/connectionRequest"
    t_req_resp = f"{serial}/connectionRequestResponse"
    t_trig = f"{serial}/userRemoteTrigger"
    t_trig_resp = f"{serial}/userRemoteTriggerResponse"
    t_overview = f"{serial}/deviceOverview"
    t_disc = f"{serial}/disconnect"

    subscribed = threading.Event()
    conn_resp = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe([(t_req_resp, 0), (t_trig_resp, 0), (t_overview, 0)])

    def on_subscribe(client, userdata, mid, reason_codes, properties=None):
        subscribed.set()

    def on_message(client, userdata, msg):
        if msg.topic == t_req_resp:
            conn_resp.set()
        elif msg.topic == t_overview and msg.payload:
            try:
                ov = parse_device_overview(msg.payload)
            except ValueError:
                return
            try:
                on_overview(ov)
            except Exception:  # noqa: BLE001 - never let a callback kill the loop
                _LOGGER.debug("on_overview callback raised", exc_info=True)

    def props() -> "Properties":
        p = Properties(PacketTypes.PUBLISH)
        p.ResponseTopic = t_req_resp
        p.UserProperty = [("ClientId", client_id)]
        return p

    cert_file = key_file = None
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv5,
    )
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    try:
        fd_c, cert_file = tempfile.mkstemp(suffix=".pem")
        os.write(fd_c, cert_pem)
        os.close(fd_c)
        fd_k, key_file = tempfile.mkstemp(suffix=".pem")
        os.write(fd_k, key_pem)
        os.close(fd_k)

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
        client.tls_set_context(ctx)

        client.connect(host, port, keepalive=30, clean_start=True)
        client.loop_start()

        if not subscribed.wait(connect_timeout):
            _LOGGER.debug("MQTT follow: subscriptions never confirmed")
            return
        client.publish(t_req, b"", qos=2, properties=props())
        conn_resp.wait(connect_timeout)
        if wake_cmd01:
            client.publish(t_trig, wake_cmd01, qos=0, properties=props())

        # Frames arrive on the network thread via on_message; just hold open.
        threading.Event().wait(duration)
    except OSError as err:
        _LOGGER.debug("MQTT follow: %s", err)
    finally:
        try:
            client.publish(t_disc, b"", qos=0, properties=props())
        except Exception:  # noqa: BLE001
            pass
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        for f in (cert_file, key_file):
            if f and os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass


def fetch_overview_blocking(
    *,
    host: str,
    port: int,
    client_id: str,
    serial: str,
    cert_pem: bytes,
    key_pem: bytes,
    wake_cmd01: bytes | None = None,
    timeout: float = 15.0,
) -> DeviceOverview | None:
    """Connect, wake the gate, and return decoded telemetry.

    Subscribes to ``<serial>/deviceOverview`` (+ the response topics), sends a
    connectionRequest, then a cmd 01 identity packet to wake the operator's
    Wi-Fi telemetry (a battery-backed gate keeps it asleep otherwise and won't
    broadcast on a bare connectionRequest). Waits for the first overview blob
    and parses it. Returns None if nothing arrives within ``timeout``.

    The cmd 01 nudge only fetches the gate's challenge (cmd 02); the gate
    actuates only after the cmd 03 challenge echo, which is never sent here, so
    this does NOT open the gate. Pass ``wake_cmd01=b""`` to listen passively.

    Blocking; intended to be run via ``loop.run_in_executor``.
    """
    import paho.mqtt.client as mqtt
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties

    t_req = f"{serial}/connectionRequest"
    t_req_resp = f"{serial}/connectionRequestResponse"
    t_trig = f"{serial}/userRemoteTrigger"
    t_trig_resp = f"{serial}/userRemoteTriggerResponse"
    t_overview = f"{serial}/deviceOverview"
    t_disc = f"{serial}/disconnect"

    subscribed = threading.Event()
    conn_resp = threading.Event()
    got_overview = threading.Event()
    holder: dict[str, bytes] = {}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe([(t_req_resp, 0), (t_trig_resp, 0), (t_overview, 0)])

    def on_subscribe(client, userdata, mid, reason_codes, properties=None):
        subscribed.set()

    def on_message(client, userdata, msg):
        if msg.topic == t_req_resp:
            conn_resp.set()
        elif msg.topic == t_overview and msg.payload:
            _LOGGER.debug("MQTT <- %s (%dB)", msg.topic, len(msg.payload))
            holder["payload"] = msg.payload
            got_overview.set()

    def props() -> "Properties":
        p = Properties(PacketTypes.PUBLISH)
        p.ResponseTopic = t_req_resp
        p.UserProperty = [("ClientId", client_id)]
        return p

    cert_file = key_file = None
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv5,
    )
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    try:
        fd_c, cert_file = tempfile.mkstemp(suffix=".pem")
        os.write(fd_c, cert_pem)
        os.close(fd_c)
        fd_k, key_file = tempfile.mkstemp(suffix=".pem")
        os.write(fd_k, key_pem)
        os.close(fd_k)

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
        client.tls_set_context(ctx)

        client.connect(host, port, keepalive=30, clean_start=True)
        client.loop_start()

        if not subscribed.wait(timeout):
            _LOGGER.warning("MQTT overview: subscriptions never confirmed")
            return None

        client.publish(t_req, b"", qos=2, properties=props())
        if not conn_resp.wait(timeout):
            _LOGGER.warning("MQTT overview: no connectionRequestResponse (gate offline?)")
            return None

        # Wake the telemetry without actuating the gate (see docstring).
        if wake_cmd01:
            client.publish(t_trig, wake_cmd01, qos=0, properties=props())

        if not got_overview.wait(timeout):
            _LOGGER.warning("MQTT overview: no deviceOverview received (gate asleep?)")
            return None
        try:
            return parse_device_overview(holder["payload"])
        except ValueError as err:
            _LOGGER.warning("MQTT overview: %s", err)
            return None
    finally:
        try:
            client.publish(t_disc, b"", qos=0, properties=props())
        except Exception:  # noqa: BLE001 - best-effort release
            pass
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        for f in (cert_file, key_file):
            if f and os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass
