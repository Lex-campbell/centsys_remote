"""Binary sensors for CenSys Gate Remote."""

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
from .entity import CentsysEntity, async_setup_dynamic_entities


@dataclass(frozen=True, kw_only=True)
class CentsysBinaryDescription(BinarySensorEntityDescription):
    """Describes a binary sensor and how to read its state from coordinator data."""

    value_fn: Callable[[dict[str, Any]], bool | None]


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
            CentsysBinarySensor(coordinator, serial, description)
            for description in BINARY_SENSORS
        ],
    )


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
