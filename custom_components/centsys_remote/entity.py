"""Base entity for CenSys Gate Remote."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import CentsysCoordinator


@callback
def async_setup_dynamic_entities(
    entry: ConfigEntry,
    coordinator: CentsysCoordinator,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[str], Iterable[Entity]],
) -> None:
    """Add entities for each gate, now and as new gates appear on later polls.

    The device list is re-fetched on every coordinator update, so a gate that
    gets linked to the account after setup (e.g. once the user is added as a
    remote user) shows up automatically without reloading the integration.
    """
    known: set[str] = set()

    @callback
    def _sync() -> None:
        new = [serial for serial in coordinator.data if serial not in known]
        if not new:
            return
        known.update(new)
        entities: list[Entity] = []
        for serial in new:
            entities.extend(factory(serial))
        if entities:
            async_add_entities(entities)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class CentsysEntity(CoordinatorEntity[CentsysCoordinator]):
    """Common base tying an entity to one gate operator (by serial)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CentsysCoordinator, serial: str) -> None:
        super().__init__(coordinator)
        self._serial = serial

    @property
    def _device_data(self) -> dict[str, Any] | None:
        return self.coordinator.data.get(self._serial)

    @property
    def available(self) -> bool:
        return super().available and self._device_data is not None

    @property
    def device_info(self) -> DeviceInfo:
        device = self._device_data["device"] if self._device_data else None
        hw = (device.raw.get("deviceHardware") if device else None) or {}
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=device.device_name if device else self._serial,
            manufacturer=MANUFACTURER,
            model=device.device_name if device else None,
            serial_number=self._serial,
            sw_version=hw.get("coreFirmwareVersion"),
        )
