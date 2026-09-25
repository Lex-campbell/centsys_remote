"""Tests for deviceOverview decoding (family detection and Holiday Lock).

``api/mqtt_remote.py`` is loaded from its file path so these tests run with plain
``pytest`` and no Home Assistant install. It imports its sibling ``enums`` via a
package-relative import, so both are registered under a synthetic package here;
neither needs ``cryptography``/``paho`` until a network function is called.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_API = Path(__file__).resolve().parents[1] / "custom_components" / "centsys_remote" / "api"

# A synthetic parent package so mqtt_remote's ``from . import enums`` resolves.
_pkg = types.ModuleType("centsys_api")
_pkg.__path__ = [str(_API)]
sys.modules["centsys_api"] = _pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"centsys_api.{name}", _API / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"centsys_api.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


enums = _load("enums")
mqtt_remote = _load("mqtt_remote")

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
        reported_product_code = None  # nothing reported -> falls back to frame
        reported_family = mqtt_remote.DeviceOverview.reported_family
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


# --- operator-reported product code overrides the frame shape -----------------


def test_reported_garage_code_overrides_gate_frame() -> None:
    # The reporter's case: a garage whose telemetry body decodes as a gate. The
    # product code it reports (39 -> internal 41 -> garage) must win.
    ov = mqtt_remote.parse_device_overview(_v2_frame())  # body looks like a gate
    assert ov.is_gate and not ov.is_garage  # without a reported code
    ov.reported_product_code = 39
    assert ov.reported_family == "garage"
    assert ov.is_garage is True
    assert ov.is_gate is False


def test_reported_gate_code_is_a_gate() -> None:
    ov = mqtt_remote.parse_device_overview(_garage())  # body is the garage shape
    ov.reported_product_code = 41  # D5 Evo reports 41 -> internal 43 -> slider
    assert ov.reported_family == "gate"
    assert ov.is_gate is True
    assert ov.is_garage is False


def test_absent_reported_code_falls_back_to_frame_shape() -> None:
    ov = mqtt_remote.parse_device_overview(_garage())
    assert ov.reported_product_code is None
    assert ov.reported_family is None
    assert ov.is_garage is True  # from the 24-byte garage frame


def test_reported_product_code_reads_the_pc_property() -> None:
    class _Props:
        UserProperty = [("ClientId", "x"), ("PC", "39"), ("FW", "1.2.3")]

    assert mqtt_remote.reported_product_code(_Props()) == 39
    assert mqtt_remote.reported_product_code(None) is None

    class _NoPc:
        UserProperty = [("ClientId", "x")]

    assert mqtt_remote.reported_product_code(_NoPc()) is None
