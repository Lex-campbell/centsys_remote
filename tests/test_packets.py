"""Tests for the trigger-activation selection in ``api/packets.py``.

The module is pure stdlib (no Home Assistant imports), so it is loaded directly
from its file path -- these tests run with plain ``pytest`` and no HA install.

The invariant under test is a safety one: the garage "RUN" activation must only
ever be selected for a telemetry-confirmed garage. Sending RUN to a gate
activates Holiday Lockout, so anything not positively a garage must fall back to
the universal TRG.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PACKETS = Path(__file__).resolve().parents[1] / "custom_components" / "centsys_remote" / "api" / "packets.py"
_spec = importlib.util.spec_from_file_location("centsys_packets", _PACKETS)
assert _spec and _spec.loader
packets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(packets)


def test_garage_gets_run() -> None:
    assert packets.trigger_activation_id(is_garage=True) == packets.ACTIVATION_GDO_RUN
    assert packets.ACTIVATION_GDO_RUN == 1


def test_gate_gets_trg() -> None:
    assert packets.trigger_activation_id(is_garage=False) == packets.ACTIVATION_TRG
    assert packets.ACTIVATION_TRG == 34


def test_default_is_safe_trg() -> None:
    # No positive garage signal -> never RUN (which would risk Holiday Lockout).
    assert packets.trigger_activation_id() == packets.ACTIVATION_TRG


def test_product_type_no_longer_selects_run() -> None:
    # productType is unreliable (type 50 ships as both gate and garage), so it
    # must not exist as a RUN fast path any more.
    assert not hasattr(packets, "GDO_PRODUCT_TYPES")


def test_holiday_lock_shares_the_garage_activation_id() -> None:
    # The operator reads this id by family: Holiday Lock on a gate, open on a
    # garage. If these ever diverge the callers' garage guards need revisiting.
    assert packets.ACTIVATION_HOLIDAY_LOCK == packets.ACTIVATION_GDO_RUN == 1


def test_holiday_lock_is_never_the_default_open() -> None:
    # A plain open must never resolve to the Holiday Lock / garage id on a gate.
    assert packets.trigger_activation_id() != packets.ACTIVATION_HOLIDAY_LOCK
    assert packets.trigger_activation_id(is_garage=False) == packets.ACTIVATION_TRG
