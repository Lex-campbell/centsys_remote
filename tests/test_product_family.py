"""Tests for the product-code family classifier in ``api/enums.py``.

The input is the product code the operator reports about itself (the telemetry
"PC" field), not the cloud device listing's productType/productCode. Loaded
straight from its file path so the suite runs with plain ``pytest`` and no Home
Assistant install; the module is pure stdlib.
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
    # The D5 Evo reports product code 41 in its telemetry and is a slider.
    assert enums.product_family(41) == "slider"


def test_garage_product_code() -> None:
    # A garage reports code 39 (-> internal 41).
    assert enums.product_family(39) == "garage"


def test_cloud_sdo5_code_is_not_classified() -> None:
    # The SDO5's *cloud* productCode is 51, a different numbering that is out of
    # range here. It must NOT classify (the operator-reported code is used
    # instead); returning None keeps us on the safe fallback.
    assert enums.product_family(51) is None


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
