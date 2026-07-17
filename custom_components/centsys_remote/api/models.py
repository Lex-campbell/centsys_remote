"""Typed models for the Centsys Remote client.

Field names mirror the JSON keys returned by the backend.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import enums


def _random_player_id() -> str:
    return str(uuid.uuid4())


@dataclass
class DeviceInfo:
    """Client identity sent to the backend (largely informational)."""

    manufacturer: str = "Apple"
    device_model: str = "iPhone17,2"
    device_platform: str = "iOS"
    operating_version: str = "26.5"
    # A stable per-install OneSignal id; the GWeb config call rejects an empty one.
    onesignal_player_id: str = field(default_factory=_random_player_id)

    @property
    def device_string(self) -> str:
        """Concatenated identity used in the GWeb MCROTPNumb auth header.

        Format: "<manufacturer><device_model><operating_version>".
        """
        return f"{self.manufacturer}{self.device_model}{self.operating_version}"


@dataclass
class Device:
    """A gate operator returned by GetDevicesByRemoteUserNumber."""

    serial_number: str
    device_name: str
    product_type: int | None = None
    product_code: int | None = None
    is_wifi_device: bool = False
    is_online: bool | None = None
    latitude: str | None = None
    longitude: str | None = None
    faulty_device: bool | None = None
    warranty_void: bool | None = None
    last_seen: str | None = None
    # Operator MAC from the device listing; used to build the trigger packets.
    mac_address: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Device":
        wifi_status = data.get("deviceWiFiStatus") or {}
        return cls(
            serial_number=data.get("serialNumber", ""),
            device_name=data.get("deviceName", ""),
            product_type=data.get("productType"),
            product_code=data.get("productCode"),
            is_wifi_device=bool(data.get("isWifiDevice", False)),
            is_online=wifi_status.get("isOnline"),
            latitude=data.get("lattitude"),  # note: backend misspells "latitude"
            longitude=data.get("longitude"),
            faulty_device=data.get("faultyDevice"),
            warranty_void=data.get("warrantyVoid"),
            last_seen=wifi_status.get("lastBackendConnectionDate"),
            mac_address=data.get("macAddress"),
            raw=data,
        )


def _pick(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present key (case-tolerant) from a dict."""
    for key in keys:
        if key in data:
            return data[key]
    lowered = {k.lower(): v for k, v in data.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return default


@dataclass
class GsmIo:
    """A single configurable button/output on a legacy GSM/ULTRA device."""

    io_number: int
    io_name: str = ""
    io_direction: int | None = None
    on_state_name: str = ""
    off_state_name: str = ""
    state_name_visible: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "GsmIo":
        return cls(
            io_number=int(_pick(data, "IONumber", "IoNumber", default=0)),
            io_name=str(_pick(data, "IOName", "IoName", default="") or ""),
            io_direction=_pick(data, "IODirection", "IoDirection"),
            on_state_name=str(_pick(data, "OnStateName", default="") or ""),
            off_state_name=str(_pick(data, "OffStateName", default="") or ""),
            state_name_visible=bool(_pick(data, "StateNameVisible", default=False)),
            raw=data,
        )

    @property
    def is_gate_trigger(self) -> bool:
        """Whether this IO looks like the main gate trigger (TRG/gate)."""
        name = self.io_name.upper()
        return any(tag in name for tag in ("TRG", "TRIGGER", "GATE"))

    @property
    def has_named_states(self) -> bool:
        """Whether this IO reports a two-state (on/off) position with labels."""
        return bool(self.state_name_visible and self.on_state_name and self.off_state_name)

    @property
    def entity_kind(self) -> str:
        """How this IO should surface in HA: 'switch', 'binary_sensor', or 'button'.

        A two-state output is a switch; a two-state input is a read-only sensor;
        anything else is a momentary button. The main gate trigger is handled by
        the cover and excluded by the callers, not here.
        """
        if self.has_named_states:
            return "binary_sensor" if self.io_direction == 1 else "switch"
        return "button"


@dataclass
class GsmDevice:
    """A legacy GSM/ULTRA operator from the GWeb config (MCRConfEnV3).

    These reach the cloud through a GSM/ULTRA module rather than SMART Wi-Fi,
    and are controlled by "activating" one of their IOs (see
    ``CentsysRemoteClient.trigger_gsm_activation``).
    """

    device_id: int
    name: str = ""
    imei: str | None = None
    device_type: int | None = None
    online: bool | None = None
    is_admin: bool | None = None
    ios: list[GsmIo] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable id for this device within coordinator data / entities."""
        return f"gsm-{self.device_id}"

    @property
    def trigger_io(self) -> GsmIo | None:
        """The IO to use for a gate open/close (a TRG-like IO, else the first)."""
        if not self.ios:
            return None
        for io in self.ios:
            if io.is_gate_trigger:
                return io
        return self.ios[0]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "GsmDevice":
        ios_raw = _pick(data, "IOConfigs", "IoConfigs", "Ios", default=[]) or []
        return cls(
            device_id=int(_pick(data, "DeviceId", default=0)),
            name=str(_pick(data, "DeviceName", default="") or ""),
            imei=_pick(data, "DeviceImei", "Imei"),
            device_type=_pick(data, "DeviceType"),
            online=_pick(data, "DeviceOnline", "Online"),
            is_admin=_pick(data, "DeviceAdmin", "IsAdmin"),
            ios=[GsmIo.from_json(io) for io in ios_raw if isinstance(io, dict)],
            raw=data,
        )


# Feedback-IO state codes -> gate position. 48 ("opening") is omitted on
# purpose: it collides with the idle value of unconfigured IOs.
GSM_GATE_STATES: dict[int, str] = {
    49: "open",
    50: "closing",
    51: "closed",
    52: "running",
}

# Reported state ids for a two-state (on/off) IO. Values outside this range
# carry no on/off meaning and are treated as unknown.
_GSM_IO_ONOFF_RANGE = range(88, 100)


def gsm_io_is_on(state_id: int | None) -> bool | None:
    """Interpret a two-state IO's reported state id as on/off, else None."""
    if state_id is None or state_id not in _GSM_IO_ONOFF_RANGE:
        return None
    return state_id % 2 == 0


@dataclass
class GsmStatus:
    """Live IO states for a legacy GSM/ULTRA operator (AppIOStatesEN).

    ``io_states`` is the list of per-IO state ids as returned in the ``IOList``.
    An operator only reports a gate position if it has a status-feedback input
    wired and configured; otherwise no IO carries a gate-state id and the gate
    position is unknown (``gate_state`` is ``None``).
    """

    device_id: int
    io_states: list[str] = field(default_factory=list)
    io_state_by_number: dict[int, int] = field(default_factory=dict)
    online: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    def is_on(self, io_number: int) -> bool | None:
        """On/off for a two-state IO by its number, or None if unknown."""
        if not self.online:
            return None
        return gsm_io_is_on(self.io_state_by_number.get(io_number))

    @property
    def gate_state(self) -> str | None:
        """Gate position ('open'/'closed'/'closing'/...) if a feedback IO reports it."""
        if not self.online:
            return None
        for state in self.io_states:
            try:
                code = int(state)
            except (TypeError, ValueError):
                continue
            label = GSM_GATE_STATES.get(code)
            if label is not None:
                return label
        return None

    @property
    def has_feedback(self) -> bool:
        return self.gate_state is not None

    @property
    def is_closed(self) -> bool | None:
        state = self.gate_state
        return None if state is None else state == "closed"

    @property
    def is_opening(self) -> bool:
        return self.gate_state == "opening"

    @property
    def is_closing(self) -> bool:
        return self.gate_state == "closing"

    @classmethod
    def from_root(cls, device_id: int | str, root: dict[str, Any]) -> "GsmStatus":
        io_list = root.get("IOList") or root.get("ioList") or []
        states: list[str] = []
        by_number: dict[int, int] = {}
        if isinstance(io_list, list):
            for entry in io_list:
                if not isinstance(entry, dict):
                    continue
                value = _pick(entry, "IOStateID", "IoStateId", "IOStateId")
                if value is None:
                    continue
                states.append(str(value))
                number = _pick(entry, "IONumber", "IoNumber")
                try:
                    by_number[int(number)] = int(value)
                except (TypeError, ValueError):
                    pass
        return cls(
            device_id=int(device_id) if str(device_id).isdigit() else 0,
            io_states=states,
            io_state_by_number=by_number,
            online=True,
            raw=root,
        )


# Airtime balance is reported by the device as "Call: <n> SMS: <n>".
_AIRTIME_RE = re.compile(r"Call:\s*(\d+)\s*SMS:\s*(\d+)", re.IGNORECASE)
_ANTENNA_LABELS = {0: "internal", 1: "external"}


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    return float(match.group().replace(",", ".")) if match else None


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if value is None:
        return None
    match = re.search(r"-?\d+", str(value))
    return int(match.group()) if match else None


@dataclass
class GsmDeviceStatus:
    """Diagnostic status for a legacy GSM/ULTRA operator (MCRStatus).

    Field names/formats are parsed defensively as the gateway is loosely typed.
    """

    device_id: int
    online: bool = True
    voltage: float | None = None
    signal: int | None = None
    antenna: str | None = None
    firmware: str | None = None
    connection: str | None = None
    network_type: str | None = None
    number: str | None = None
    last_synced: str | None = None
    call_tokens: int | None = None
    sms_tokens: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, device_id: int | str, data: dict[str, Any]) -> "GsmDeviceStatus":
        antenna = _pick(data, "Antenna", "AntennaSelection")
        if isinstance(antenna, int):
            antenna = _ANTENNA_LABELS.get(antenna)
        elif antenna:
            antenna = str(antenna).lower()
        conn = _pick(data, "OnlineStatus", "ConnectionStatus")
        if isinstance(conn, bool):
            conn = "Active" if conn else "Inactive"
        elif conn not in (None, ""):
            conn = str(conn)

        call = sms = None
        match = _AIRTIME_RE.search(str(_pick(data, "Airtime", "AirtimeMessage", default="")))
        if match:
            call, sms = int(match.group(1)), int(match.group(2))

        return cls(
            device_id=int(device_id) if str(device_id).isdigit() else 0,
            voltage=_to_float(_pick(data, "Voltage")),
            signal=_to_int(_pick(data, "Signal")),
            antenna=antenna or None,
            firmware=(_pick(data, "Firmware") or None),
            connection=conn or None,
            network_type=(_pick(data, "ConnectionType", "NetworkType") or None),
            number=(_pick(data, "Number") or None),
            last_synced=(_pick(data, "LastSynced", "AirtimeLastUpdated") or None),
            call_tokens=call,
            sms_tokens=sms,
            raw=data,
        )


@dataclass
class OperatorStatus:
    """Live status from GetOperatorOverview."""

    operator_serial_number: str
    operator_status: int | None = None
    power_supply_status: int | None = None
    closing_beam_status: int | None = None
    opening_beam_status: int | None = None
    theft_alarm_state: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "OperatorStatus":
        return cls(
            operator_serial_number=data.get("operatorSerialNumber", ""),
            operator_status=data.get("operatorStatus"),
            power_supply_status=data.get("powerSupplyStatus"),
            closing_beam_status=data.get("closingBeamStatus"),
            opening_beam_status=data.get("openingBeamStatus"),
            theft_alarm_state=data.get("theftAlarmState"),
            raw=data,
        )

    @property
    def operator_status_label(self) -> str | None:
        """e.g. 'closed', 'open', 'opening' (None if unmappable)."""
        return enums._label(enums.OperatorStatus, self.operator_status)

    @property
    def power_supply_status_label(self) -> str | None:
        return enums._label(enums.PowerStatus, self.power_supply_status)

    @property
    def theft_alarm_state_label(self) -> str | None:
        return enums._label(enums.TheftAlarmState, self.theft_alarm_state)

    @property
    def closing_beam_label(self) -> str | None:
        """Simplified safety-beam condition (clear/obstructed/disabled/...)."""
        return enums.beam_state(self.closing_beam_status)

    @property
    def opening_beam_label(self) -> str | None:
        return enums.beam_state(self.opening_beam_status)

    @property
    def is_closed(self) -> bool | None:
        if self.operator_status is None:
            return None
        return self.operator_status == enums.OperatorStatus.CLOSED

    @property
    def is_opening(self) -> bool:
        return self.operator_status == enums.OperatorStatus.OPENING

    @property
    def is_closing(self) -> bool:
        return self.operator_status == enums.OperatorStatus.CLOSING
