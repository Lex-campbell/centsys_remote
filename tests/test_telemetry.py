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


def test_garage_learn_and_lost_status() -> None:
    def _sdo(gate_st: int) -> bytes:
        body = bytearray(24)
        body[12] = gate_st
        return _HEADER + bytes(body)

    assert mqtt_remote.parse_device_overview(_sdo(6)).gate_status == "learn"
    assert mqtt_remote.parse_device_overview(_sdo(7)).gate_status == "lost"


# --- notification flags -------------------------------------------------------
#
# The two 32-bit words sit at body offsets 4-7 (low, bits 0-31) and 8-11 (high,
# bits 32-63) for both the slider and swing layouts.


def _slider(*, nf1: int = 0, nf2: int = 0, cond: int = 0x0A00) -> bytes:
    body = bytearray(36)
    body[0:2] = (1340).to_bytes(2, "little")
    body[4:8] = (nf1 & 0xFFFFFFFF).to_bytes(4, "little")
    body[8:12] = (nf2 & 0xFFFFFFFF).to_bytes(4, "little")
    body[12:16] = (cond & 0xFFFFFFFF).to_bytes(4, "little")
    body[22] = 1
    return _HEADER + bytes(body)


def _swing(*, nf1: int = 0, nf2: int = 0) -> bytes:
    body = bytearray(38)
    body[4:8] = (nf1 & 0xFFFFFFFF).to_bytes(4, "little")
    body[8:12] = (nf2 & 0xFFFFFFFF).to_bytes(4, "little")
    return _HEADER + bytes(body)


def _garage(*, nf1: int = 0) -> bytes:
    body = bytearray(24)
    body[0:4] = (nf1 & 0xFFFFFFFF).to_bytes(4, "little")
    body[12] = 3
    return _HEADER + bytes(body)


def test_notification_flag_packing_low_and_high_words() -> None:
    # A bit in the low word keeps its number; a bit in the high word is +32.
    assert mqtt_remote.parse_device_overview(_slider(nf1=1 << 7)).notification_flags >> 7 & 1
    assert mqtt_remote.parse_device_overview(_slider(nf2=1 << 7)).notification_flags >> 39 & 1


def test_slider_conditions_and_problem_rollup() -> None:
    # "lost" is bit 24 (low word); it is a fault, so it is a problem.
    ov = mqtt_remote.parse_device_overview(_slider(nf1=1 << 24))
    assert "lost" in ov.active_conditions
    assert "lost" in ov.problems
    assert ov.has_condition("needs_relearn") is True


def test_state_conditions_are_excluded_from_problems() -> None:
    # Keep Open is bit 54 (high word, 54-32=22) and is a mode, not a fault.
    ov = mqtt_remote.parse_device_overview(_slider(nf2=1 << 22))
    assert ov.keep_open is True
    assert "keep_open" in ov.active_conditions
    assert "keep_open" not in ov.problems
    # Tamper armed is bit 60 (high word, 28) -- also a state, not a problem.
    ov = mqtt_remote.parse_device_overview(_slider(nf2=1 << 28))
    assert ov.has_condition("tamper_alarm_armed") is True
    assert not ov.problems


def test_motor_disconnected_resolves_per_family() -> None:
    # Slider: single bit 22. Swing: the master/slave bits (28/29) both count.
    assert mqtt_remote.parse_device_overview(_slider(nf1=1 << 22)).has_condition(
        "motor_disconnected"
    )
    assert mqtt_remote.parse_device_overview(_swing(nf1=1 << 28)).has_condition(
        "motor_disconnected"
    )
    assert mqtt_remote.parse_device_overview(_swing(nf1=1 << 29)).has_condition(
        "motor_disconnected"
    )


def test_inapplicable_group_returns_none() -> None:
    # Swing operators have no tamper-armed bit, and a garage has no motor bit,
    # so those groups are "unknown" (None), not a misleading False.
    assert mqtt_remote.parse_device_overview(_swing()).has_condition("tamper_alarm_armed") is None
    assert mqtt_remote.parse_device_overview(_garage()).has_condition("motor_disconnected") is None


def test_sys_tp_urc_style_strict_parsing_rejects_unknown_length() -> None:
    # An unrecognised length on the multiplexed topic must be dropped by strict
    # parsing rather than force-decoded with the nearest layout (which is what
    # the lenient default does, and would corrupt the family signal).
    other = _HEADER + bytes(40)  # not a known layout, but long enough to decode
    assert mqtt_remote.parse_device_overview(other).family == "vx"  # lenient fallback
    try:
        mqtt_remote.parse_device_overview(other, strict=True)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("strict parsing should reject an unknown length")
