"""Guards for the MQTT clientId identities.

Three connection identities must stay mutually distinct, or the broker refuses
one as a duplicate of another (the bug that left live/Keep/Lock state stale when
the phone app was connected):
  * the plain ``mcr:<number>`` (activation; also the app's id),
  * ``LISTENER_CLIENT_SUFFIX`` (the persistent live listener),
  * ``READ_CLIENT_SUFFIX`` (one-off telemetry reads/wakes).

``api/const.py`` is plain stdlib, so it is loaded straight from its path (same
approach as ``test_region.py``) and runs under bare ``pytest``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "centsys_remote"


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


const = _load("centsys_api_const", "api/const.py")


def test_read_and_listener_suffixes_are_non_empty() -> None:
    # An empty suffix would collapse a read/listener onto the app's plain id.
    assert const.LISTENER_CLIENT_SUFFIX
    assert const.READ_CLIENT_SUFFIX


def test_the_three_identities_are_mutually_distinct() -> None:
    plain = ""  # the activation / phone-app identity
    identities = {plain, const.LISTENER_CLIENT_SUFFIX, const.READ_CLIENT_SUFFIX}
    assert len(identities) == 3
