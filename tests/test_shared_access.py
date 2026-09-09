"""Tests for community / shared-access (AccessSharing) parsing and gating.

``api/models.py`` uses a relative import (``from . import enums``), so it is
loaded under a small synthetic package here, letting these tests run with plain
``pytest`` and no Home Assistant install. The fixture is the real
``GetAccessesByUserNumber`` body captured from the app (trimmed to the fields
the parser reads); the operator + activations arrive as a nested JSON *string*,
exactly as on the wire.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_API = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "centsys_remote"
    / "api"
)

_pkg = types.ModuleType("centsys_api_shim")
_pkg.__path__ = [str(_API)]  # type: ignore[attr-defined]
sys.modules["centsys_api_shim"] = _pkg
for _name in ("enums", "models"):
    _spec = importlib.util.spec_from_file_location(
        f"centsys_api_shim.{_name}", _API / f"{_name}.py"
    )
    assert _spec and _spec.loader
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"centsys_api_shim.{_name}"] = _mod
    _spec.loader.exec_module(_mod)
models = sys.modules["centsys_api_shim.models"]

# Synthetic fixture data (no real accounts/devices).
NUMBER = "+27820000001"  # the recipient (this account)
OWNER_NUMBER = "+27820000002"  # the gate's owner
GUID = "11111111-1111-1111-1111-111111111111"

_PAYLOAD = (
    '{"Operator":{"Uuid":"22222222-2222-2222-2222-222222222222",'
    '"Name":"D5 Evo SMART","ProductCode":43,"DeviceMacAddress":"AAAAAA==",'
    '"IsWifiDevice":true},'
    '"Activations":['
    '{"Id":77,"Name":"OpenCloseDescription","TriggerId":34,"IoNumber":0},'
    '{"Id":78,"Name":"PedestrianDescription","TriggerId":35,"IoNumber":0},'
    '{"Id":71,"Name":"HolidayLockDescription","TriggerId":1,"IoNumber":0}]}'
)


def _share(**overrides):
    base = {
        "version": 1,
        "accessRevoked": False,
        "shareDeviceType": 2,  # Smart
        "guid": GUID,
        "type": 1,
        "deviceName": "D5 Evo SMART",
        "startTimeUtc": "2020-01-01T00:00:00",
        "endTimeUtc": "2099-01-01T00:00:00",
        "maximumTriggerCount": 2,
        "ultraDeviceId": None,
        "smartSerialNumber": "000000000000000000000001",
        "lastModifiedDateUtc": "2024-01-01T00:00:00",
        "serializedSharedAccessPayload": _PAYLOAD,
        "users": [
            {
                "accepted": False,
                "currentTriggerCount": 0,
                "user": {
                    "number": NUMBER,
                    "lastModifiedDateUtc": "2024-01-01T00:00:00",
                },
            }
        ],
        "owner": {"name": "Owner", "user": {"number": OWNER_NUMBER}},
        "accessRevokedByAdministrator": False,
    }
    base.update(overrides)
    return base


def _one(**overrides):
    accesses = models.parse_shared_accesses({"sharedAccesses": [_share(**overrides)]})
    assert len(accesses) == 1
    return accesses[0]


def test_basic_parse() -> None:
    a = _one()
    assert a.device_name == "D5 Evo SMART"
    assert a.guid == GUID
    assert a.key == f"shared-{GUID}"
    assert a.is_wifi and not a.is_ultra
    assert a.smart_serial_number == "000000000000000000000001"


def test_nested_payload_actions() -> None:
    a = _one()
    assert [act.name for act in a.actions] == [
        "OpenCloseDescription",
        "PedestrianDescription",
        "HolidayLockDescription",
    ]
    gate = a.gate_action
    assert gate is not None and gate.trigger_id == 34
    # Everything except the gate becomes a button.
    assert [b.name for b in a.button_actions] == [
        "PedestrianDescription",
        "HolidayLockDescription",
    ]
    assert a.action_by_id(78).trigger_id == 35


def test_ultra_uses_io_number() -> None:
    a = _one(shareDeviceType=1, ultraDeviceId=999)
    assert a.is_ultra and not a.is_wifi


def test_available_within_window_and_triggers() -> None:
    a = _one()
    assert a.is_available(NUMBER) is True
    assert a.remaining_triggers(NUMBER) == 2
    assert a.user_last_modified_utc(NUMBER) == "2024-01-01T00:00:00"


def test_revoked_unavailable() -> None:
    assert _one(accessRevoked=True).is_available(NUMBER) is False
    assert _one(accessRevokedByAdministrator=True).is_available(NUMBER) is False


def test_expired_unavailable() -> None:
    assert _one(endTimeUtc="2000-01-01T00:00:00").is_available(NUMBER) is False


def test_not_yet_active_unavailable() -> None:
    assert _one(startTimeUtc="2099-01-01T00:00:00").is_available(NUMBER) is False


def test_depleted_unavailable() -> None:
    a = _one(
        users=[
            {
                "accepted": True,
                "currentTriggerCount": 2,
                "user": {"number": NUMBER},
            }
        ]
    )
    assert a.remaining_triggers(NUMBER) == 0
    assert a.is_available(NUMBER) is False


def test_permanent_share_has_no_trigger_limit() -> None:
    a = _one(maximumTriggerCount=None)
    assert a.remaining_triggers(NUMBER) is None
    assert a.is_available(NUMBER) is True


def test_owner_detection() -> None:
    a = _one()
    assert a.owner_number == OWNER_NUMBER
    # The recipient is not the owner; the owner is.
    assert a.is_owned_by(NUMBER) is False
    assert a.is_owned_by(OWNER_NUMBER) is True
    # Matching is on digits, so the leading "+" is ignored...
    assert a.is_owned_by(OWNER_NUMBER.lstrip("+")) is True
    # ...but a genuinely different number is not the owner.
    assert a.is_owned_by("+27829999999") is False
    assert a.is_owned_by(None) is False


def test_expiry_parses_to_aware_utc() -> None:
    a = _one(endTimeUtc="2026-09-10T09:42:43.600438")
    exp = a.expiry
    assert exp is not None and exp.tzinfo is not None
    assert exp.year == 2026 and exp.month == 9 and exp.day == 10
    # Open-ended share has no expiry.
    assert _one(endTimeUtc=None).expiry is None


def test_empty_and_message_responses() -> None:
    assert models.parse_shared_accesses({"sharedAccesses": []}) == []
    assert models.parse_shared_accesses("No shared access") == []
    assert models.parse_shared_accesses(None) == []
