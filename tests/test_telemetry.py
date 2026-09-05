"""Tests for deviceOverview decoding (family detection and Holiday Lock).

``api/mqtt_remote.py`` is loaded straight from its file path so these tests run
with plain ``pytest`` and no Home Assistant install. The module only needs the
standard library plus ``cryptography``/``paho`` at call time, not at import.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "custom_components" / "centsys_remote" / "api" / "mqtt_remote.py"
_spec = importlib.util.spec_from_file_location("centsys_mqtt_remote", _SRC)
assert _spec and _spec.loader
mqtt_remote = importlib.util.module_from_spec(_spec)
sys.modules["centsys_mqtt_remote"] = mqtt_remote
_spec.loader.exec_module(mqtt_remote)

_HEADER = bytes.fromhex("00263924")


def _v2_frame(*, condition_byte0: int = 0x00, battery: int = 1340) -> bytes:
    """Build a minimal 36-byte slider (v2) body with a chosen condition byte."""
    body = bytearray(36)
    body[0:2] = battery.to_bytes(2, "little")
    body[12] = condition_byte0  # condition_flags low byte
    body[13] = 0x0A  # bits 9/11: set on every gate we've observed
    body[22] = 1  # gate status: closed
    return _HEADER + bytes(body)


def test_holiday_lock_bit_clear() -> None:
    ov = mqtt_remote.parse_device_overview(_v2_frame(condition_byte0=0x00))
    assert ov.holiday_lock is False


def test_holiday_lock_bit_set() -> None:
    ov = mqtt_remote.parse_device_overview(_v2_frame(condition_byte0=0x01))
    assert ov.holiday_lock is True


def test_holiday_lock_ignores_the_other_condition_bits() -> None:
    # Bit 12 (0x1000) is a different setting; it must not read as Holiday Lock.
    body = bytearray(_v2_frame()[4:])
    body[13] |= 0x10  # bit 12 sits in the second byte of the flags word
    ov = mqtt_remote.parse_device_overview(_HEADER + bytes(body))
    assert ov.condition_flags & 0x1000
    assert ov.holiday_lock is False


def test_family_detection_by_body_length() -> None:
    # Lengths seen in the field. 52 is a separate product line; 64 is a padded
    # v2 frame that must not be decoded with the vx layout (it would misread the
    # battery as ~51 V).
    for length, expected in ((24, "sdo5"), (36, "v2"), (38, "vx"), (52, "vx52"), (64, "v2")):
        body = bytearray(length)
        body[13] = 0x0A
        ov = mqtt_remote.parse_device_overview(_HEADER + bytes(body))
        assert ov.family == expected, f"{length}B -> {ov.family}, expected {expected}"


def test_padded_v2_frame_reads_a_sane_battery() -> None:
    body = bytearray(64)
    body[0:2] = (2760).to_bytes(2, "little")  # 27.60 V on the v2 scale
    ov = mqtt_remote.parse_device_overview(_HEADER + bytes(body))
    assert ov.family == "v2"
    assert ov.battery_voltage == 27.6


def test_family_helpers_classify_gate_and_garage() -> None:
    garage = mqtt_remote.parse_device_overview(_HEADER + bytes(24))
    assert (garage.is_garage, garage.is_gate) == (True, False)
    gate = mqtt_remote.parse_device_overview(_v2_frame())
    assert (gate.is_garage, gate.is_gate) == (False, True)


def test_unknown_family_is_not_treated_as_a_gate() -> None:
    # is_gate is an allow-list, so an operator we haven't catalogued withholds
    # gate-only actions rather than guessing (the Holiday Lock id opens a
    # garage, so guessing wrong would move a door).
    class _Unknown:
        family = "something-new"
        is_gate = mqtt_remote.DeviceOverview.is_gate
        is_garage = mqtt_remote.DeviceOverview.is_garage

    probe = _Unknown()
    assert probe.is_gate is False
    assert probe.is_garage is False


def test_short_body_is_rejected() -> None:
    try:
        mqtt_remote.parse_device_overview(_HEADER + bytes(8))
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for a too-short body")
