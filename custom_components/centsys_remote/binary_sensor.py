"""Binary sensors for Centsys Gate Remote."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import CentsysCoordinator
from .entity import CentsysEntity, CentsysGsmIoEntity, async_setup_dynamic_entities


@dataclass(frozen=True, kw_only=True)
class CentsysBinaryDescription(BinarySensorEntityDescription):
    """Describes a binary sensor and how to read its state from coordinator data."""

    value_fn: Callable[[dict[str, Any]], bool | None]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None


def _overview(data: dict[str, Any]):
    return data.get("overview")


def _condition_group(group: str) -> Callable[[dict[str, Any]], bool | None]:
    """Read a named condition group off the cached telemetry (None if unknown)."""

    def _inner(data: dict[str, Any]) -> bool | None:
        overview = _overview(data)
        return None if overview is None else overview.has_condition(group)

    return _inner


def _any_problem(data: dict[str, Any]) -> bool | None:
    overview = _overview(data)
    return None if overview is None else bool(overview.problems)


def _problem_conditions(data: dict[str, Any]) -> dict[str, Any] | None:
    overview = _overview(data)
    if overview is None or not overview.problems:
        return None
    return {"conditions": list(overview.problems)}


BINARY_SENSORS: tuple[CentsysBinaryDescription, ...] = (
    CentsysBinaryDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda data: (
            None if data["device"].is_online is None else bool(data["device"].is_online)
        ),
    ),
    CentsysBinaryDescription(
        key="fault",
        translation_key="fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (
            None
            if data["device"].faulty_device is None
            else bool(data["device"].faulty_device)
        ),
    ),
    CentsysBinaryDescription(
        key="warranty_void",
        translation_key="warranty_void",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: (
            None
            if data["device"].warranty_void is None
            else bool(data["device"].warranty_void)
        ),
    ),
    # Operator conditions decoded from the live telemetry (unknown until it is
    # read). The catch-all reports whether anything is wrong and lists it; the
    # rest pick out the conditions worth a dedicated, actionable entity.
    CentsysBinaryDescription(
        key="operator_problem",
        translation_key="operator_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_any_problem,
        attrs_fn=_problem_conditions,
    ),
    CentsysBinaryDescription(
        key="needs_relearn",
        translation_key="needs_relearn",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_condition_group("needs_relearn"),
    ),
    CentsysBinaryDescription(
        key="motor_disconnected",
        translation_key="motor_disconnected",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_condition_group("motor_disconnected"),
    ),
    CentsysBinaryDescription(
        key="collision",
        translation_key="collision",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_condition_group("collision"),
    ),
    CentsysBinaryDescription(
        key="emergency_stop",
        translation_key="emergency_stop",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_condition_group("emergency_stop"),
    ),
    CentsysBinaryDescription(
        key="battery_service_required",
        translation_key="battery_service_required",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_condition_group("battery_service_required"),
    ),
    CentsysBinaryDescription(
        key="safety_beam_fault",
        translation_key="safety_beam_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_condition_group("safety_beam_fault"),
    ),
    # Read-only state, not a fault: whether the tamper alarm is armed.
    CentsysBinaryDescription(
        key="tamper_alarm_armed",
        translation_key="tamper_alarm_armed",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_condition_group("tamper_alarm_armed"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CentsysCoordinator = hass.data[DOMAIN][entry.entry_id]
    def _factory(key: str):
        data = coordinator.data.get(key) or {}
        if data.get("kind") == "wifi":
            return [
                CentsysBinarySensor(coordinator, key, description)
                for description in BINARY_SENSORS
            ]
        if data.get("kind") == "gsm":
            device = data.get("gsm_device")
            if device is None:
                return []
            gate_io = device.trigger_io
            gate_number = gate_io.io_number if gate_io else None
            return [
                CentsysGsmIoBinarySensor(coordinator, key, io)
                for io in device.ios
                if io.io_number != gate_number and io.entity_kind == "binary_sensor"
            ]
        return []

    async_setup_dynamic_entities(entry, coordinator, async_add_entities, _factory)


class CentsysBinarySensor(CentsysEntity, BinarySensorEntity):
    """A single boolean condition from the device overview."""

    entity_description: CentsysBinaryDescription

    def __init__(
        self,
        coordinator: CentsysCoordinator,
        serial: str,
        description: CentsysBinaryDescription,
    ) -> None:
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        data = self._device_data
        if not data:
            return None
        return self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        data = self._device_data
        if fn is None or not data:
            return None
        return fn(data)


class CentsysGsmIoBinarySensor(CentsysGsmIoEntity, BinarySensorEntity):
    """A read-only status input on a GSM/ULTRA operator (on/off feedback)."""

    def __init__(
        self,
        coordinator: CentsysCoordinator,
        key: str,
        io,
    ) -> None:
        super().__init__(coordinator, key, io)
        self._attr_unique_id = f"{key}_input_{io.io_number}"

    @property
    def is_on(self) -> bool | None:
        status = self._status
        return status.is_on(self._io_number) if status else None
