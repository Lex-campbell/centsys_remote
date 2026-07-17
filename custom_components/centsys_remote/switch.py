"""Switches for CenSys Gate Remote."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api.exceptions import CentsysError
from .const import DOMAIN
from .coordinator import CentsysCoordinator
from .entity import CentsysGsmIoEntity, async_setup_dynamic_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CentsysCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _factory(key: str):
        data = coordinator.data.get(key) or {}
        if data.get("kind") != "gsm":
            return []
        device = data.get("gsm_device")
        if device is None:
            return []
        gate_io = device.trigger_io
        gate_number = gate_io.io_number if gate_io else None
        return [
            CentsysGsmIoSwitch(coordinator, key, io)
            for io in device.ios
            if io.io_number != gate_number and io.entity_kind == "switch"
        ]

    async_setup_dynamic_entities(entry, coordinator, async_add_entities, _factory)


class CentsysGsmIoSwitch(CentsysGsmIoEntity, SwitchEntity):
    """A two-state auxiliary output (e.g. courtesy light) on a GSM/ULTRA operator.

    The operator toggles the output on activation, so both turn-on and turn-off
    send the same pulse; the redundant direction is skipped when the reported
    state already matches. State is unknown until the operator reports it.
    """

    def __init__(self, coordinator: CentsysCoordinator, key: str, io) -> None:
        super().__init__(coordinator, key, io)
        self._attr_unique_id = f"{key}_switch_{io.io_number}"

    @property
    def is_on(self) -> bool | None:
        status = self._status
        return status.is_on(self._io_number) if status else None

    async def _toggle(self) -> None:
        device = self._gsm_device
        if device is None:
            raise HomeAssistantError("This gate is no longer available.")
        try:
            await self.coordinator.client.trigger_gsm_activation(
                device.device_id, self._io_number
            )
        except CentsysError as err:
            raise HomeAssistantError(
                f"Failed to switch {self._attr_name}: {err}"
            ) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.is_on is True:
            return
        await self._toggle()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self.is_on is False:
            return
        await self._toggle()
