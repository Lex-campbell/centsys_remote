"""Status enums that decode the integers returned by ``GetOperatorOverview``."""

from __future__ import annotations

from enum import IntEnum


class OperatorStatus(IntEnum):
    UNKNOWN = 0
    OPEN = 1
    CLOSED = 2
    PARTLY_OPEN = 3
    PARTLY_CLOSED = 4
    OPENING = 5
    CLOSING = 6


class PowerStatus(IntEnum):
    NORMAL = 0
    LOW = 1
    OFF = 2
    UNKNOWN = 3


class TheftAlarmState(IntEnum):
    ACTIVATED = 0
    CLEARED = 1
    DISABLED = 2


class BeamStatus(IntEnum):
    DISABLED = 0
    NEVER_ACTIVATED = 1
    IO_MADE_BLE_NC = 2
    IO_MADE_BLE_MADE = 3
    IO_MADE_BLE_BROKEN = 4
    IO_MADE_BLE_WAITING = 5
    IO_MADE_BLE_DISCONNECTED = 6
    IO_BROKEN_BLE_NC = 7
    IO_BROKEN_BLE_BROKEN = 8
    IO_BROKEN_BLE_MADE = 9
    IO_BROKEN_BLE_WAITING = 10
    IO_BROKEN_BLE_DISCONNECTED = 11
    IO_NC_BLE_MADE = 12
    IO_NC_BLE_BROKEN = 13
    IO_NC_BLE_WAITING = 14
    IO_NC_BLE_DISCONNECTED = 15
    IO_8K2ERR_BLE_NC = 16
    IO_8K2ERR_BLE_MADE = 17
    IO_8K2ERR_BLE_BROKEN = 18
    IO_8K2ERR_BLE_WAITING = 19
    IO_8K2ERR_BLE_DISCONNECTED = 20
    IO_MADE_BLE_SLEEP = 21
    IO_BROKEN_BLE_SLEEP = 22
    IO_NC_BLE_SLEEP = 23
    IO_8K2ERR_BLE_SLEEP = 24


def _label(enum_cls: type[IntEnum], value: int | None) -> str | None:
    """Human-friendly lower_snake label for a raw int, or None if unmappable."""
    if value is None:
        return None
    try:
        return enum_cls(value).name.lower()
    except ValueError:
        return None


# The raw BeamStatus encodes both the wired IO line and a BLE mirror in 25
# states; we collapse them to the meaningful physical condition of the beam.
BEAM_STATE_OPTIONS = (
    "disabled",
    "idle",
    "clear",
    "obstructed",
    "not_connected",
    "wiring_error",
    "sleep",
)


def beam_state(value: int | None) -> str | None:
    """Collapse a raw BeamStatus int to a simple beam condition label."""
    if value is None:
        return None
    try:
        name = BeamStatus(value).name
    except ValueError:
        return None
    if name == "DISABLED":
        return "disabled"
    if name == "NEVER_ACTIVATED":
        return "idle"
    if name.endswith("BLE_SLEEP"):
        return "sleep"
    if name.startswith("IO_MADE"):
        return "clear"
    if name.startswith("IO_BROKEN"):
        return "obstructed"
    if name.startswith("IO_NC"):
        return "not_connected"
    if name.startswith("IO_8K2ERR"):
        return "wiring_error"
    return None
