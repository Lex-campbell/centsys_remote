"""Per-operator MQTT trigger-packet construction for SMART Wi-Fi gates.

Each packet is a 4-byte header plus an obfuscated body, keyed per operator from
the first 4 bytes of its ``macAddress`` (from the device listing), so packets
are rebuilt for each gate. The three packets are cmd 01 (identity), cmd 05 (time
sync) and cmd 03 (activation); the handshake appends the live challenge from
cmd 02 to cmd 03.
"""

from __future__ import annotations

import datetime
import struct

BASE_KEY = bytes.fromhex("38983fba4dbfab9c")

# Activation id for the gate trigger. Sliders and swing operators trigger with
# TRG; garage-door operators use RUN from their own activation set.
ACTIVATION_TRG = 34
ACTIVATION_GDO_RUN = 1

# ``productType`` values that identify a garage-door operator (all other
# operators trigger with TRG). These mirror the app's operator classification.
GDO_PRODUCT_TYPES = frozenset({2, 50})

_ALGO_VERSION = 1
_KEY_VERSION = 1


def trigger_activation_id(product_type: int | None) -> int:
    """Return the activation id that opens an operator of this product type."""
    if product_type in GDO_PRODUCT_TYPES:
        return ACTIVATION_GDO_RUN
    return ACTIVATION_TRG


def parse_mac(mac: str | bytes) -> bytes:
    """Return the 4-byte key from a ``macAddress`` string or byte sequence.

    Accepts "AA:BB:CC:DD:EE:FF", "AABBCCDD..", or raw bytes. Raises ValueError
    if fewer than 4 bytes are present.
    """
    if isinstance(mac, str):
        raw = bytes.fromhex(mac.replace(":", "").replace("-", "").strip())
    else:
        raw = bytes(mac)
    if len(raw) < 4:
        raise ValueError(f"MAC too short for key derivation: {mac!r}")
    return raw[:4]


def _key(mac4: bytes) -> bytes:
    return bytes(BASE_KEY[i] ^ mac4[i] if i < 4 else BASE_KEY[i] for i in range(8))


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(data[i] ^ key[i % len(key)] for i in range(len(data)))


def _header(packet_type: int) -> bytes:
    return bytes([_ALGO_VERSION, _KEY_VERSION, packet_type, 0])


def _phone_parts(phone: str) -> tuple[int, int, int]:
    """Encode the phone number into the three uint32 identity parts."""
    s = phone.replace("+", "")
    n = len(s)
    lower = mid = upper = 0
    for i in range(16, 24):
        if i < n:
            lower |= (ord(s[n - i - 1]) - 48) << (((23 - i) * 4) & 31)
    for i in range(8, 16):
        if i < n:
            mid |= (ord(s[n - i - 1]) - 48) << (((15 - i) * 4) & 31)
    for i in range(0, 8):
        if i < n:
            upper |= (ord(s[n - i - 1]) - 48) << (((7 - i) * 4) & 31)
    return lower & 0xFFFFFFFF, mid & 0xFFFFFFFF, upper & 0xFFFFFFFF


def build_cmd01(phone: str, mac4: bytes) -> bytes:
    """Identity packet built from the account phone number."""
    lower, mid, upper = _phone_parts(phone)
    body = struct.pack("<III", lower, mid, upper)
    return _header(1) + _xor(body, _key(mac4))


def build_cmd05(mac4: bytes, when: datetime.datetime | None = None) -> bytes:
    """Time-sync packet: the current local date/time."""
    dt = when or datetime.datetime.now()
    body = bytes(
        [
            dt.second,
            dt.minute,
            dt.hour,
            dt.weekday(),
            dt.day,
            dt.month - 1,
            dt.year - 2000,
            0,
        ]
    )
    return _header(5) + _xor(body, _key(mac4))


# Activation response codes returned in cmd 04.
ACTIVATION_OK = 1
ACTIVATION_CONFIGURATION_MISMATCH = 7


def build_cmd03_prefix(
    mac4: bytes, config_version: int = 0, activation_id: int = ACTIVATION_TRG
) -> bytes:
    """Activation packet prefix; the live challenge is appended by the handshake.

    ``activation_id`` selects the action (TRG opens the gate). A stricter gate
    rejects a ``config_version`` it disagrees with (see :func:`decode_cmd04`).
    """
    body = struct.pack("<BBH", config_version & 0xFF, 0, activation_id & 0xFFFF)
    return _header(3) + _xor(body, _key(mac4))


def decode_cmd04(mac4: bytes, payload: bytes) -> tuple[int, int]:
    """Decode a cmd 04 response into (response_code, config_version).

    On a configuration mismatch the gate reports the ``config_version`` it
    expects, so the trigger can be retried with that value.
    """
    body = _xor(bytes(payload)[4:], _key(mac4))
    return body[1], body[0]


def build_open_packets(
    phone: str,
    mac: str | bytes,
    *,
    config_version: int = 0,
    when: datetime.datetime | None = None,
) -> tuple[bytes, bytes, bytes]:
    """Build (cmd01, cmd05, cmd03_prefix) for one operator."""
    mac4 = parse_mac(mac)
    return (
        build_cmd01(phone, mac4),
        build_cmd05(mac4, when),
        build_cmd03_prefix(mac4, config_version),
    )
