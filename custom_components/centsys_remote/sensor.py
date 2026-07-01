"""Sensors for CenSys Gate Remote."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricPotential,
    UnitOfTemperature,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .api.enums import BEAM_STATE_OPTIONS
from .const import DOMAIN
from .coordinator import CentsysCoordinator
from .entity import CentsysEntity, async_setup_dynamic_entities


@dataclass(frozen=True, kw_only=True)
class CentsysSensorDescription(SensorEntityDescription):
    """Describes a CenSys sensor and how to read its value from coordinator data."""

    value_fn: Callable[[dict[str, Any]], Any]


def _wifi_rssi(data: dict[str, Any]) -> Any:
    wifi = (data["device"].raw.get("deviceWiFiStatus") or {})
    return wifi.get("wifiRssi")


def _last_seen(data: dict[str, Any]) -> datetime | None:
    raw = data["device"].last_seen
    return dt_util.parse_datetime(raw) if raw else None


def _status_label(attr: str) -> Callable[[dict[str, Any]], Any]:
    """Return a decoded enum label property (e.g. 'closed', 'normal')."""

    def _inner(data: dict[str, Any]) -> Any:
        status = data.get("status")
        return getattr(status, attr) if status else None

    return _inner


def _overview_attr(attr: str) -> Callable[[dict[str, Any]], Any]:
    """Return a field off the cached MQTT DeviceOverview (battery, temp, ...)."""

    def _inner(data: dict[str, Any]) -> Any:
        overview = data.get("overview")
        return getattr(overview, attr, None) if overview else None

    return _inner


SENSORS: tuple[CentsysSensorDescription, ...] = (
    CentsysSensorDescription(
        key="operator_status",
        translation_key="operator_status",
        device_class=SensorDeviceClass.ENUM,
        options=["unknown", "open", "closed", "partly_open", "partly_closed", "opening", "closing"],
        value_fn=_status_label("operator_status_label"),
    ),
    CentsysSensorDescription(
        key="theft_alarm_state",
        translation_key="theft_alarm_state",
        device_class=SensorDeviceClass.ENUM,
        options=["activated", "cleared", "disabled"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_status_label("theft_alarm_state_label"),
    ),
    CentsysSensorDescription(
        key="power_supply_status",
        translation_key="power_supply_status",
        device_class=SensorDeviceClass.ENUM,
        options=["normal", "low", "off", "unknown"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_status_label("power_supply_status_label"),
    ),
    CentsysSensorDescription(
        key="closing_beam",
        translation_key="closing_beam",
        device_class=SensorDeviceClass.ENUM,
        options=list(BEAM_STATE_OPTIONS),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_status_label("closing_beam_label"),
    ),
    CentsysSensorDescription(
        key="opening_beam",
        translation_key="opening_beam",
        device_class=SensorDeviceClass.ENUM,
        options=list(BEAM_STATE_OPTIONS),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_status_label("opening_beam_label"),
    ),
    CentsysSensorDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_last_seen,
    ),
    CentsysSensorDescription(
        key="wifi_rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_wifi_rssi,
    ),
    # MQTT telemetry (refreshed on the slow TELEMETRY_SCAN_INTERVAL).
    CentsysSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=_overview_attr("battery_voltage"),
    ),
    CentsysSensorDescription(
        key="operator_temperature",
        translation_key="operator_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_overview_attr("temperature_c"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CentsysCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda serial: [
            CentsysSensor(coordinator, serial, description)
            for description in SENSORS
        ],
    )


class CentsysSensor(CentsysEntity, SensorEntity):
    """A single read-only value from the device or operator overview."""

    entity_description: CentsysSensorDescription

    def __init__(
        self,
        coordinator: CentsysCoordinator,
        serial: str,
        description: CentsysSensorDescription,
    ) -> None:
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"

    @property
    def native_value(self) -> Any:
        data = self._device_data
        if not data:
            return None
        return self.entity_description.value_fn(data)
