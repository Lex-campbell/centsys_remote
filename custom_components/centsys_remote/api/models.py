"""Typed models for the Centsys Remote client.

Field names mirror the JSON keys returned by the backend.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

    @property
    def product_family(self) -> str | None:
        """"slider", "swing" or "garage" from the product code, or None.

        Uses ``productCode`` (not the unreliable ``productType``) so a garage can
        be told from a gate even when live telemetry is unavailable.
        """
        return enums.product_family(self.product_code)


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
    """Live IO states for a legacy GSM/ULTRA operator.

    ``io_states`` is a positional array: entry ``n`` is the reported state id
    of IO number ``n + 1``. An operator only reports a gate position if it has
    a status-feedback input wired and configured; otherwise no IO carries a
    gate-state id and the gate position is unknown (``gate_state`` is ``None``).
    """

    device_id: int
    io_states: list[str] = field(default_factory=list)
    online: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    def _state_id(self, io_number: int) -> int | None:
        """The reported state id for a 1-based IO number, or None if absent."""
        index = io_number - 1
        if not 0 <= index < len(self.io_states):
            return None
        try:
            return int(self.io_states[index])
        except (TypeError, ValueError):
            return None

    def is_on(self, io_number: int) -> bool | None:
        """On/off for a two-state IO by its number, or None if unknown."""
        if not self.online:
            return None
        return gsm_io_is_on(self._state_id(io_number))

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
        if isinstance(io_list, list):
            # Keep the array positional: every entry maps to one IO in order, so
            # a missing/malformed entry becomes a placeholder rather than a skip.
            for entry in io_list:
                value = (
                    _pick(entry, "IOStateID", "IoStateId", "IOStateId")
                    if isinstance(entry, dict)
                    else None
                )
                states.append("" if value is None else str(value))
        return cls(
            device_id=int(device_id) if str(device_id).isdigit() else 0,
            io_states=states,
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


# ShareDeviceTypeEnum (from the app): 0 None, 1 Ultra, 2 Smart. Only Ultra needs
# naming (it selects the trigger id); Smart is simply "not Ultra".
SHARE_DEVICE_ULTRA = 1

# The main open/close action is surfaced as the gate cover rather than a button.
# Its ``Name`` is not stable (sometimes the i18n key "OpenCloseDescription",
# sometimes the resolved label "Open/Close"), so match on the stable TRG
# activation id and keep the names only as a fallback.
_GATE_TRIGGER_ID = 34  # packets.ACTIVATION_TRG
_GATE_ACTION_NAMES = ("OpenCloseDescription", "Open/Close")


def _parse_utc(value: Any) -> "datetime | None":
    """Parse a backend UTC timestamp string into an aware datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@dataclass
class SharedAction:
    """A single triggerable action ("activation") within a shared-access site.

    Parsed from an entry of ``serializedSharedAccessPayload.Activations``. The
    action is triggered server-side via ``SendActivation`` using ``trigger_id``
    (SMART operators) or ``io_number`` (GSM/ULTRA operators).
    """

    id: int = 0
    name: str = ""  # i18n key, e.g. "OpenCloseDescription", "PedestrianDescription"
    trigger_id: int = 0
    io_number: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SharedAction":
        return cls(
            id=int(_pick(data, "Id", default=0) or 0),
            name=str(_pick(data, "Name", default="") or ""),
            trigger_id=int(_pick(data, "TriggerId", default=0) or 0),
            io_number=int(_pick(data, "IONumber", "IoNumber", default=0) or 0),
            raw=data,
        )

    @property
    def is_gate(self) -> bool:
        """Whether this is the main open/close action (surfaced as the cover)."""
        return self.trigger_id == _GATE_TRIGGER_ID or self.name in _GATE_ACTION_NAMES


@dataclass
class SharedAccess:
    """A community / shared-access "site" granted to this number (AccessSharing).

    A gate the user does not own but may trigger, exposing one or more
    :class:`SharedAction` (e.g. *Gate*, *Pedestrian*). Discovered via
    ``GetAccessesByUserNumber`` and triggered server-side via ``SendActivation``
    -- no MQTT or operator certificate is involved.
    """

    guid: str = ""
    device_name: str = ""
    share_device_type: int | None = None
    revoked: bool = False
    revoked_by_admin: bool = False
    start_time_utc: str | None = None
    end_time_utc: str | None = None
    maximum_trigger_count: int | None = None
    ultra_device_id: int | None = None
    smart_serial_number: str | None = None
    last_modified_utc: str | None = None
    owner_number: str | None = None
    actions: list[SharedAction] = field(default_factory=list)
    users: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable id for this shared access within coordinator data / entities."""
        return f"shared-{self.guid or self.device_name}"

    @property
    def is_ultra(self) -> bool:
        return self.share_device_type == SHARE_DEVICE_ULTRA or (
            self.ultra_device_id is not None
        )

    @property
    def is_wifi(self) -> bool:
        return not self.is_ultra

    @property
    def gate_action(self) -> SharedAction | None:
        """The main open/close action, surfaced as the cover (or None)."""
        return next((a for a in self.actions if a.is_gate), None)

    @property
    def button_actions(self) -> list[SharedAction]:
        """Every non-gate action, surfaced as buttons."""
        return [a for a in self.actions if not a.is_gate]

    def action_by_id(self, action_id: int) -> SharedAction | None:
        return next((a for a in self.actions if a.id == action_id), None)

    @staticmethod
    def _same_number(a: str | None, b: str | None) -> bool:
        da = re.sub(r"\D", "", a or "")
        db = re.sub(r"\D", "", b or "")
        return bool(da and db and da == db)

    def is_owned_by(self, number: str | None) -> bool:
        """Whether ``number`` is the share's owner.

        The owner controls this gate directly (it is in ``GetDevices``), so we
        don't surface a shared entity for it -- that would double it up and
        collide with the recipient's copy in a multi-account setup.
        """
        return self._same_number(number, self.owner_number)

    def _user(self, number: str | None) -> dict[str, Any] | None:
        """The user entry for ``number`` (digit-compared), if present."""
        target = re.sub(r"\D", "", number or "")
        if not target:
            return None
        for entry in self.users:
            usr = entry.get("user") or {}
            if self._same_number(usr.get("number") or entry.get("number"), target):
                return entry
        return None

    def user_last_modified_utc(self, number: str | None) -> str | None:
        entry = self._user(number)
        return (entry.get("user") or {}).get("lastModifiedDateUtc") if entry else None

    def remaining_triggers(self, number: str | None) -> int | None:
        """Triggers left for ``number`` (None when the share has no limit)."""
        if not self.maximum_trigger_count:
            return None
        entry = self._user(number)
        used = int((entry or {}).get("currentTriggerCount") or 0)
        return max(0, self.maximum_trigger_count - used)

    @property
    def expiry(self) -> "datetime | None":
        """The share's end time as an aware datetime, or None if open-ended."""
        return _parse_utc(self.end_time_utc)

    def is_available(self, number: str | None, *, now: "datetime | None" = None) -> bool:
        """Whether this number may currently trigger the share.

        Gated on concrete fields only (revoked, time window, remaining triggers)
        rather than the share's ``Type`` enum (whose values we don't map), so a
        working share is never hidden by a misread type.
        """
        if self.revoked or self.revoked_by_admin:
            return False
        now = now or datetime.now(timezone.utc)
        start = _parse_utc(self.start_time_utc)
        end = _parse_utc(self.end_time_utc)
        if start and now < start:
            return False
        if end and now > end:
            return False
        remaining = self.remaining_triggers(number)
        if remaining is not None and remaining <= 0:
            return False
        return True

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SharedAccess":
        # The operator + activations live inside a nested JSON *string*.
        payload = _pick(data, "SerializedSharedAccessPayload", default=None)
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        activations = _pick(payload, "Activations", default=[]) or []
        users = _pick(data, "Users", default=[]) or []
        owner = _pick(data, "Owner", default={}) or {}
        owner_user = _pick(owner, "User", default={}) if isinstance(owner, dict) else {}
        return cls(
            guid=str(_pick(data, "Guid", "AccessGuid", default="") or ""),
            device_name=str(_pick(data, "DeviceName", "SiteName", "Name", default="") or ""),
            share_device_type=_pick(data, "ShareDeviceType"),
            revoked=bool(_pick(data, "AccessRevoked", "Revoked", default=False)),
            revoked_by_admin=bool(_pick(data, "AccessRevokedByAdministrator", default=False)),
            start_time_utc=_pick(data, "StartTimeUtc"),
            end_time_utc=_pick(data, "EndTimeUtc"),
            maximum_trigger_count=_pick(data, "MaximumTriggerCount"),
            ultra_device_id=_pick(data, "UltraDeviceId"),
            smart_serial_number=_pick(data, "SmartSerialNumber"),
            last_modified_utc=_pick(data, "LastModifiedDateUtc"),
            owner_number=_pick(owner_user, "Number") if isinstance(owner_user, dict) else None,
            actions=[
                SharedAction.from_json(a) for a in activations if isinstance(a, dict)
            ],
            users=[u for u in users if isinstance(u, dict)],
            raw=data,
        )


def parse_shared_accesses(raw: Any) -> list[SharedAccess]:
    """Parse a ``GetAccessesByUserNumber`` response into shared-access models."""
    if isinstance(raw, dict):
        items = _pick(raw, "SharedAccesses", default=None)
        if items is None:
            items = next((v for v in raw.values() if isinstance(v, list)), [])
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    return [SharedAccess.from_json(x) for x in items if isinstance(x, dict)]


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
