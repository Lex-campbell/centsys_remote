"""Base entity for CenSys Gate Remote."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import CentsysCoordinator


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
