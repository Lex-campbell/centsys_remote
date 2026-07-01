"""Typed models for the Centsys Remote client.

Field names mirror the JSON keys returned by the backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import enums


@dataclass
class DeviceInfo:
    """Client identity sent to the backend (largely informational)."""

    manufacturer: str = "Apple"
    device_model: str = "iPhone17,2"
    device_platform: str = "iOS"
    operating_version: str = "26.5"
    onesignal_player_id: str = ""  # set to a random UUID per install if empty

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
