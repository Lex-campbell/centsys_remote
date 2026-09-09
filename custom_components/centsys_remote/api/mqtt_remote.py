"""Gate control and live telemetry over MQTT (mutual TLS).

A SMART Wi-Fi operator is controlled over a cloud MQTT broker (mTLS, MQTT v5),
not HTTP. Topics are prefixed with the long operator serial. The open command
is a short challenge-response: connect, request the connection, then exchange
the identity, time-sync and activation packets (see ``packets``); the gate
returns a fresh challenge that the activation echoes back.

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
from functools import lru_cache
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Private CA; leaf is CN=CentsysQA with no IP SAN (we connect by Azure IP).
MQTT_TLS_SERVER_NAME = "CentsysQA"


@lru_cache(maxsize=1)
def _ca_pem() -> str:
    """Read the pinned CA once.

    Deliberately not read at import time: this module is imported lazily from
    the event loop, and reading a file there is blocking I/O. Every caller
    reaches this from an executor thread.
    """
    return (Path(__file__).resolve().parent / "certs" / "centsys_ca.pem").read_text()


def mqtt_ssl_context(*, certfile: str, keyfile: str) -> ssl.SSLContext:
    """SSL context for Centsys MQTT: pinned CA + client cert (mTLS)."""
    ctx = ssl.create_default_context(cadata=_ca_pem())
    # Centurion's certs omit Authority Key Identifier; VERIFY_X509_STRICT
    # (default on HA's newer OpenSSL) rejects that. Chain + hostname still checked.
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return ctx


def configure_mqtt_tls(client, *, certfile: str, keyfile: str) -> None:
    """Apply pinned-CA mTLS. paho would use the IP as server_hostname; use leaf CN."""
    ctx = mqtt_ssl_context(certfile=certfile, keyfile=keyfile)
    client.tls_set_context(ctx)

    def _ssl_wrap_socket(tcp_sock):
        ssl_sock = ctx.wrap_socket(
            tcp_sock,
            server_hostname=MQTT_TLS_SERVER_NAME,
            do_handshake_on_connect=False,
        )
        ssl_sock.settimeout(client._keepalive)
        ssl_sock.do_handshake()
        return ssl_sock

    client._ssl_wrap_socket = _ssl_wrap_socket  # type: ignore[method-assign]


# --- deviceOverview telemetry -------------------------------------------------
#
# The gate publishes a binary blob on "<serial>/deviceOverview". The first 4
# bytes are a header; the rest is a packed, little-endian struct whose shape
# depends on the operator family:
#
#   * sliding-gate operators (e.g. D5 Evo)  -> 36-byte body
#   * other gate operators                  -> 38-byte body
#   * garage-door operators                 -> 24-byte body
#
# We auto-detect by the post-header length (see ``_BODY_LAYOUTS``) and decode the
# fields below. Two further lengths are seen in the field: 52 bytes (a separate
# product line, reported as "vx52") and 64 bytes (a padded 36-byte frame).

# Slider/swing operators.
_GATE_STATUS = {
    0: "open",
    1: "closed",
    2: "partly_open",
    3: "partly_closed",
    4: "opening",
    5: "closing",
}
# Garage-door operators use a different status enum.
_SDO_GATE_STATUS = {
    1: "open",
    2: "opening",
    3: "closed",
    4: "closing",
    5: "partly_open",
    6: "learn",
    7: "lost",
}
_POWER_STATUS = {0: "normal", 1: "low", 2: "unknown", 3: "psu_comms_off"}

# ``condition_flags`` bit reporting that Holiday Lock is active on a gate
# operator. Holiday Lock inhibits the operator's inputs, so the gate ignores
# triggers until it is switched off again.
CONDITION_HOLIDAY_LOCK = 0x1

# Telemetry families. Several actions differ between a garage door and a gate --
# in one case the same activation opens a garage but toggles Holiday Lock on a
# gate -- so callers ask via ``DeviceOverview.is_garage`` / ``.is_gate`` rather
# than comparing family strings themselves.
GARAGE_FAMILY = "sdo5"
GATE_FAMILIES = frozenset({"v2", "vx", "vx52"})


def _beam_label(value: int) -> str:
    """Collapse the operator's raw beam-status value to a simple condition."""
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


# Per-family ``notification_flags`` bit -> condition name. The same bit means
# different things on different operator families, so each has its own table;
# condition names are functional (what the operator reports), not identifiers,
# and only the meaningful conditions are mapped. A name shared across families
# lets the groups below span all of them with a single definition.
_SLIDER_CONDITIONS: dict[int, str] = {
    0: "on_battery",
    5: "mains_low",
    7: "collision",
    19: "opening_beam_test_fail",
    20: "closing_beam_test_fail",
    21: "gate_stalled",
    22: "motor_disconnected",
    23: "max_collisions",
    24: "lost",
    25: "emergency_stop",
    26: "limits_not_set",
    28: "replace_battery",
    30: "origin_config_fault",
    35: "temperature_warning",
    38: "tamper_alarm",
    41: "origin_fault",
    42: "tamper_fault",
    43: "controller_unclipped",
    45: "no_batteries",
    49: "power_supply_fault",
    51: "photons_disconnected",
    54: "keep_open",
    56: "multiple_stall_events",
    58: "photon_battery_low",
    60: "tamper_alarm_armed",
}
_SWING_CONDITIONS: dict[int, str] = {
    1: "mains_low",
    2: "power_supply_fault",
    4: "replace_battery",
    5: "no_batteries",
    15: "collision",
    16: "collision",
    17: "max_collisions",
    18: "gate_stalled",
    19: "gate_stalled",
    20: "lost",
    21: "limits_not_set",
    27: "temperature_warning",
    28: "motor_disconnected",
    29: "motor_disconnected",
    30: "emergency_stop",
    32: "opening_beam_test_fail",
    33: "closing_beam_test_fail",
    37: "photons_disconnected",
    38: "photon_battery_low",
    39: "keep_open",
}
_GARAGE_CONDITIONS: dict[int, str] = {
    0: "collision",
    5: "max_collisions",
    6: "power_low",
    7: "drive_fault",
    9: "low_battery",
    10: "lost",
    11: "limits_not_set",
    13: "user_stop",
    14: "tamper_alarm",
    16: "on_battery",
    17: "beams_error",
    19: "vacation_mode",
    20: "keep_open",
    21: "low_battery_preventing_motion",
    22: "batteries_damaged",
    23: "no_batteries",
    24: "mains_low",
}
_CONDITIONS_BY_FAMILY: dict[str, dict[int, str]] = {
    "v2": _SLIDER_CONDITIONS,
    "vx": _SWING_CONDITIONS,
    "vx52": _SWING_CONDITIONS,
    "sdo5": _GARAGE_CONDITIONS,
}

# Conditions that report a mode the operator is in rather than a fault needing
# attention. Excluded from the problem roll-up. (Mains/battery power is already
# reported by the power and battery sensors, so it counts as state here too.)
_STATE_CONDITIONS = frozenset(
    {
        "holiday_lock",
        "keep_open",
        "vacation_mode",
        "tamper_alarm_armed",
        "mains_low",
        "on_battery",
        "power_low",
        "low_battery",
    }
)

# Named groups surfaced as their own diagnostics. Defined as sets of condition
# names (not bit numbers) so one definition covers every family even where the
# underlying bit differs.
_CONDITION_GROUPS: dict[str, frozenset[str]] = {
    "needs_relearn": frozenset(
        {"lost", "limits_not_set", "origin_fault", "origin_config_fault"}
    ),
    "motor_disconnected": frozenset({"motor_disconnected"}),
    "collision": frozenset(
        {"collision", "max_collisions", "gate_stalled", "multiple_stall_events"}
    ),
    "emergency_stop": frozenset({"emergency_stop", "user_stop"}),
    "battery_service_required": frozenset(
        {"replace_battery", "no_batteries", "batteries_damaged", "low_battery_preventing_motion"}
    ),
    "safety_beam_fault": frozenset(
        {"opening_beam_test_fail", "closing_beam_test_fail", "beams_error"}
    ),
    "tamper_alarm_armed": frozenset({"tamper_alarm_armed"}),
}


@dataclass
class DeviceOverview:
    """Decoded live telemetry from a "<serial>/deviceOverview" MQTT message."""

    family: str  # "v2" | "vx" | "vx52" | "sdo5"
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
    # Two 32-bit words, low word first: bits 0-31 come from the first word and
    # bits 32-63 from the second, matching how the operator numbers them.
    notification_flags: int
    condition_flags: int

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def holiday_lock(self) -> bool:
        """Whether Holiday Lock is currently active on this operator."""
        return bool(self.condition_flags & CONDITION_HOLIDAY_LOCK)

    @property
    def is_garage(self) -> bool:
        """Whether this operator is a garage door."""
        return self.family == GARAGE_FAMILY

    @property
    def is_gate(self) -> bool:
        """Whether this is a known gate operator (a sliding or swing gate).

        Stricter than ``not is_garage``: an unrecognised family answers False,
        so an action that is only safe on a gate is withheld rather than guessed.
        """
        return self.family in GATE_FAMILIES

    @property
    def active_conditions(self) -> tuple[str, ...]:
        """Names of every condition the operator is currently reporting."""
        table = _CONDITIONS_BY_FAMILY.get(self.family, {})
        flags = self.notification_flags
        return tuple(
            sorted({name for bit, name in table.items() if flags >> bit & 1})
        )

    @property
    def problems(self) -> tuple[str, ...]:
        """Active conditions that indicate a fault needing attention."""
        return tuple(c for c in self.active_conditions if c not in _STATE_CONDITIONS)

    @property
    def keep_open(self) -> bool:
        """Whether the operator is currently holding the gate open."""
        return "keep_open" in self.active_conditions

    def has_condition(self, group: str) -> bool | None:
        """Whether any condition in a named group is active.

        Returns None when the group does not apply to this operator (none of its
        conditions exist for this family), so a caller can report "unknown"
        rather than a misleading "no".
        """
        names = _CONDITION_GROUPS.get(group, frozenset())
        applicable = names & set(_CONDITIONS_BY_FAMILY.get(self.family, {}).values())
        if not applicable:
            return None
        return bool(applicable & set(self.active_conditions))


# Known ``deviceOverview`` body lengths -> (struct layout, family label).
#
# Only the head of the body differs between layouts (where the battery and
# temperature sit); the flag/timer block that follows is shared. Two lengths
# share a layout with a different label: a 52-byte body is a separate product
# line (its battery reads ~13 V rather than ~27 V), and a 64-byte body is a
# zero-padded ``v2`` frame.
_BODY_LAYOUTS: dict[int, tuple[str, str]] = {
    24: ("sdo5", "sdo5"),
    36: ("v2", "v2"),
    38: ("vx", "vx"),
    52: ("vx", "vx52"),
    64: ("v2", "v2"),
}


def _layout_for(length: int, *, strict: bool = False) -> tuple[str, str]:
    """Return the (struct layout, family) to decode a body of this length.

    An unknown length is decoded with the nearest known layout rather than
    dropped: the flag/timer block is shared across the gate families, so the
    useful fields still land even on an operator we haven't catalogued. When
    ``strict`` is set an unknown length raises instead -- used on a multiplexed
    topic where non-telemetry messages must not be force-decoded as a frame.
    """
    known = _BODY_LAYOUTS.get(length)
    if known is not None:
        return known
    if strict:
        raise ValueError(f"unrecognized deviceOverview body length: {length} bytes")
    if length < 24:
        raise ValueError(f"deviceOverview too short: {length} bytes")
    layout = "sdo5" if length < 36 else ("v2" if length < 38 else "vx")
    _LOGGER.debug(
        "Unrecognized deviceOverview body length %s bytes; decoding it with the "
        "%s layout",
        length,
        layout,
    )
    return layout, layout


def parse_device_overview(payload: bytes, *, strict: bool = False) -> DeviceOverview:
    """Decode a raw deviceOverview MQTT payload into structured telemetry.

    ``payload`` is the full MQTT payload; the leading 4-byte header is stripped
    here (matching the app). Raises ValueError if the body length is unusable --
    with ``strict`` any length that isn't an exact known layout (see
    ``_layout_for``), used when reading a topic that also carries non-telemetry
    messages.
    """
    body = bytes(payload)[4:]
    n = len(body)
    layout, family = _layout_for(n, strict=strict)

    if layout == "v2":
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
        gate_position = gate_pos
    elif layout == "vx":
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
        gate_position = None
    else:  # garage-door operator
        nf1, batt, cond, _pad, secs, _timer, _pad2, gate_st, irbc, _xmr, power = (
            struct.unpack_from("<IHBBHBBBBBB", body, 0)
        )
        nf2, temp, irbo, in_v = 0, None, 0, 0
        gate_position = None

    temp_c = temp if temp is None else (temp - 256 if temp > 127 else temp)
    # Garage-door operators use a distinct status enum and battery scale.
    is_sdo = family == GARAGE_FAMILY
    status_map = _SDO_GATE_STATUS if is_sdo else _GATE_STATUS
    batt_divisor = 10.0 if is_sdo else 100.0
    return DeviceOverview(
        family=family,
        gate_status=status_map.get(gate_st),
        gate_status_raw=gate_st,
        battery_voltage=round(batt / batt_divisor, 2) if batt else None,
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
        notification_flags=(nf2 << 32) | nf1,
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
    build_cmd03,
    decode_cmd04,
    timeout: float = 8.0,
) -> bool:
    """Run the full open handshake. Returns True if the gate accepted the trigger.

    ``build_cmd03(config_version)`` builds the activation prefix (the live 4-byte
    challenge is appended here). The activation carries a configuration version;
    a stricter gate rejects a stale one and reports the value it expects, so on
    a mismatch the trigger is retried once with that value. ``decode_cmd04``
    turns a cmd 04 response into ``(response_code, config_version)``.

    Blocking (uses paho's loop in a background thread internally). Intended to be
    run via ``loop.run_in_executor`` from async code.
    """
    import paho.mqtt.client as mqtt
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties

    from . import packets

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

        configure_mqtt_tls(client, certfile=cert_file, keyfile=key_file)

        try:
            client.connect(host, port, keepalive=30, clean_start=True)
        except ssl.SSLError as err:
            if "CERTIFICATE_EXPIRED" in str(err).upper():
                from .exceptions import CentsysCertExpiredError

                raise CentsysCertExpiredError(
                    "The Centsys broker rejected the connection citing an expired "
                    "certificate. This is a provider-side outage that affects all "
                    "clients (the official app included); gate control resumes once "
                    "Centsys resolves it."
                ) from err
            raise
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

        # Most operators accept config version 0; a stricter one rejects it and
        # reports the version it wants, so retry once with that value.
        cv = 0
        for _ in range(2):
            client.publish(
                t_trig, build_cmd03(cv) + challenge, qos=0, properties=props(t_trig_resp)
            )
            cmd04 = wait_trig()
            _LOGGER.debug("MQTT open: response %s", cmd04.hex(" ") if cmd04 else None)
            if not cmd04:
                return False
            code, gate_cv = decode_cmd04(cmd04)
            if code != packets.ACTIVATION_CONFIGURATION_MISMATCH or gate_cv == cv:
                return code == packets.ACTIVATION_OK
            _LOGGER.debug("MQTT open: config mismatch, retrying with version %s", gate_cv)
            cv = gate_cv
        return False
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
    # Secondary telemetry topic, multiplexed -> parsed strictly (see fetch).
    t_sysurc = f"{serial}/sysTpUrc"
    t_disc = f"{serial}/disconnect"

    subscribed = threading.Event()
    conn_resp = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe([(t_req_resp, 0), (t_trig_resp, 0), (t_overview, 0), (t_sysurc, 0)])

    def on_subscribe(client, userdata, mid, reason_codes, properties=None):
        subscribed.set()

    def on_message(client, userdata, msg):
        if msg.topic == t_req_resp:
            conn_resp.set()
        elif msg.topic in (t_overview, t_sysurc) and msg.payload:
            try:
                ov = parse_device_overview(msg.payload, strict=msg.topic == t_sysurc)
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

        configure_mqtt_tls(client, certfile=cert_file, keyfile=key_file)

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
    # Some operators publish their telemetry only on this secondary topic. It is
    # multiplexed (it also carries non-telemetry messages), so it is parsed
    # strictly and anything that isn't a known frame is ignored.
    t_sysurc = f"{serial}/sysTpUrc"
    t_disc = f"{serial}/disconnect"

    subscribed = threading.Event()
    conn_resp = threading.Event()
    got_overview = threading.Event()
    holder: dict[str, DeviceOverview] = {}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe([(t_req_resp, 0), (t_trig_resp, 0), (t_overview, 0), (t_sysurc, 0)])

    def on_subscribe(client, userdata, mid, reason_codes, properties=None):
        subscribed.set()

    def on_message(client, userdata, msg):
        if msg.topic == t_req_resp:
            conn_resp.set()
        elif msg.topic in (t_overview, t_sysurc) and msg.payload:
            try:
                holder["overview"] = parse_device_overview(
                    msg.payload, strict=msg.topic == t_sysurc
                )
            except ValueError:
                return
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

        configure_mqtt_tls(client, certfile=cert_file, keyfile=key_file)

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
        return holder.get("overview")
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
