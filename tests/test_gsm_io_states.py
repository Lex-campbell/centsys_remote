"""Tests for GSM/ULTRA live IO-state decoding (``api/models.py``).

``models.py`` only imports its sibling ``enums`` module, so it is loaded under a
lightweight synthetic package -- these tests run with plain ``pytest`` and no
Home Assistant install.

The invariant under test: the status feed is a *positional* array. Entry ``n``
is the state of IO number ``n + 1``; there is no per-entry IO number to match
on. Reading it by position is what lets a configured input (e.g. an early
warning or tamper input) report on/off instead of staying unknown.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_API = Path(__file__).resolve().parents[1] / "custom_components" / "centsys_remote" / "api"

# Give the module a real package parent so ``from . import enums`` resolves.
_pkg = types.ModuleType("centsys_api")
_pkg.__path__ = [str(_API)]
sys.modules.setdefault("centsys_api", _pkg)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"centsys_api.{name}", _API / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"centsys_api.{name}"] = module
    spec.loader.exec_module(module)
    return module


_load("enums")
models = _load("models")


def _root(*state_ids: int) -> dict:
    """An IO-state response carrying the given state ids, in IO order."""
    return {
        "DeviceId": 42,
        "RefreshPermissionVersion": 1,
        "IOList": [{"IOStateID": sid} for sid in state_ids],
    }


# --- the on/off decode itself -------------------------------------------------


def test_even_state_ids_are_on() -> None:
    for sid in (88, 90, 92, 94, 96, 98):
        assert models.gsm_io_is_on(sid) is True


def test_odd_state_ids_are_off() -> None:
    for sid in (89, 91, 93, 95, 97, 99):
        assert models.gsm_io_is_on(sid) is False


def test_state_ids_outside_the_range_are_unknown() -> None:
    for sid in (0, 47, 51, 52, 87, 100, None):
        assert models.gsm_io_is_on(sid) is None


# --- positional lookup (the fix) ---------------------------------------------


def test_is_on_reads_by_position() -> None:
    # IO 1 = gate feedback (closed), IO 2 = an "on" input, IO 3 = an "off" input.
    status = models.GsmStatus.from_root(42, _root(51, 88, 89))
    assert status.is_on(2) is True
    assert status.is_on(3) is False
    # IO 1 carries a gate-state id, which is not an on/off value.
    assert status.is_on(1) is None


def test_is_on_out_of_range_is_unknown() -> None:
    status = models.GsmStatus.from_root(42, _root(88))
    assert status.is_on(0) is None
    assert status.is_on(2) is None


def test_is_on_offline_is_unknown() -> None:
    status = models.GsmStatus(device_id=42, io_states=["88"], online=False)
    assert status.is_on(1) is None


def test_label_state_inverted_labels_flip_result() -> None:
    assert models.gsm_io_label_state(True, "off", "on") == ("off", False)
    assert models.gsm_io_label_state(False, "off", "on") == ("on", True)


def test_label_state_normal_labels_preserve_raw() -> None:
    assert models.gsm_io_label_state(True, "on", "off") == ("on", True)
    assert models.gsm_io_label_state(False, "on", "off") == ("off", False)


def test_label_state_unrecognised_labels_fall_back_to_raw() -> None:
    assert models.gsm_io_label_state(True, "PULSED", "Gate Open") == ("PULSED", True)
    assert models.gsm_io_label_state(False, "PULSED", "Gate Open") == ("Gate Open", False)


def test_label_state_empty_labels_fall_back_to_raw() -> None:
    assert models.gsm_io_label_state(True, "", "") == ("", True)
    assert models.gsm_io_label_state(False, "", "") == ("", False)


def test_label_state_unknown_raw_is_unknown() -> None:
    assert models.gsm_io_label_state(None, "on", "off") == (None, None)


def test_missing_entries_keep_alignment() -> None:
    # A malformed/empty entry must not shift the IOs that follow it.
    root = {"IOList": [{}, {"IOStateID": 89}]}
    status = models.GsmStatus.from_root(42, root)
    assert status.is_on(1) is None
    assert status.is_on(2) is False


# --- gate feedback still works ------------------------------------------------


def test_gate_state_scans_all_positions() -> None:
    status = models.GsmStatus.from_root(42, _root(88, 51))
    assert status.gate_state == "closed"
    assert status.is_closed is True
