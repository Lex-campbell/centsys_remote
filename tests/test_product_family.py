"""Tests for the product-code family classifier in ``api/enums.py``.

Loaded straight from its file path so the suite runs with plain ``pytest`` and
no Home Assistant install; the module is pure stdlib.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "custom_components" / "centsys_remote" / "api" / "enums.py"
_spec = importlib.util.spec_from_file_location("centsys_enums", _SRC)
assert _spec and _spec.loader
enums = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(enums)


def test_our_gate_classifies_as_slider() -> None:
    # The D5 Evo used to develop this reports product code 41 and is a slider.
    assert enums.product_family(41) == "slider"


def test_garage_product_code() -> None:
    assert enums.product_family(39) == "garage"


def test_swing_product_code() -> None:
    assert enums.product_family(25) == "swing"


def test_unrecognised_code_returns_none() -> None:
    # Unknown or out-of-range codes fall back rather than guess a family.
    assert enums.product_family(99) is None
    assert enums.product_family(0) is None
    assert enums.product_family(None) is None


def test_product_type_is_not_the_signal() -> None:
    # productType 50 has shipped as both a gate and a garage, so there must be
    # no family lookup keyed on it.
    assert not hasattr(enums, "product_type_family")
