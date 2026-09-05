"""Switches for Centsys Gate Remote."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api.exceptions import CentsysCertExpiredError, CentsysError
from .const import DOMAIN
from .coordinator import CentsysCoordinator
from .entity import CentsysEntity, CentsysGsmIoEntity, async_setup_dynamic_entities

# Telemetry families that are gate operators (i.e. not a garage door). Holiday
# Lock only applies to these, and its activation id means "open" on a garage.
_GATE_FAMILIES = frozenset({"v2", "vx", "vx52"})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CentsysCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _factory(key: str):
        data = coordinator.data.get(key) or {}
        if data.get("kind") == "wifi":
            # Garage-door operators have no Holiday Lock, and there the same
            # activation would open the door -- so skip them.
            if getattr(data.get("overview"), "family", None) == "sdo5":
                return []
            return [CentsysHolidayLockSwitch(coordinator, key)]
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


class CentsysHolidayLockSwitch(CentsysEntity, SwitchEntity):
    """Holiday Lock on a SMART Wi-Fi gate operator.

    Holiday Lock inhibits the operator's inputs, so the gate ignores triggers
    until it is switched off again. State is read from the operator's live
    telemetry, so it follows the lock being changed anywhere -- a remote, a
    schedule, SmartGuard Air or the MyCentsys Pro app -- not just from here.

    The operator toggles the lock on a single activation, so on and off send the
    same command; the redundant direction is skipped when the reported state
    already matches.
    """

    _attr_translation_key = "holiday_lock"
    _attr_icon = "mdi:lock-clock"

    def __init__(self, coordinator: CentsysCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_holiday_lock"

    @property
    def _overview(self):
        data = self._device_data
        return data.get("overview") if data else None

    @property
    def is_on(self) -> bool | None:
        """True/False from telemetry, or None until telemetry has been read."""
        overview = self._overview
        return overview.holiday_lock if overview is not None else None

    async def _gate_mac(self) -> str:
        """The operator MAC, confirming first that this is not a garage door.

        Holiday Lock shares its activation id with the garage open command, so
        this refuses to act unless telemetry positively reports a gate family.
        """
        data = self._device_data or {}
        mac = getattr(data.get("device"), "mac_address", None)
        if not mac:
            raise HomeAssistantError(
                "Gate has no MAC address in the cloud device list; cannot build "
                "the Holiday Lock command."
            )
        overview = self._overview
        if overview is None:
            # Cold start: read telemetry once so the family is known.
            try:
                overview = await self.coordinator.client.get_overview(
                    self._serial, mac=mac
                )
            except CentsysError as err:
                raise HomeAssistantError(
                    f"Couldn't read the gate's status to confirm Holiday Lock "
                    f"applies to it: {err}"
                ) from err
            if overview is not None:
                self.coordinator.set_overview(self._serial, overview)
        family = getattr(overview, "family", None)
        if family not in _GATE_FAMILIES:
            raise HomeAssistantError(
                "Holiday Lock is only available once the gate has reported its "
                "status, and it does not apply to garage-door operators."
            )
        return mac

    async def _toggle(self) -> None:
        mac = await self._gate_mac()
        before = self.is_on
        try:
            ok = await self.coordinator.client.toggle_holiday_lock(
                self._serial, mac=mac
            )
        except CentsysCertExpiredError as err:
            raise HomeAssistantError(str(err)) from err
        except CentsysError as err:
            raise HomeAssistantError(f"Failed to switch Holiday Lock: {err}") from err
        if not ok:
            raise HomeAssistantError(
                "Gate did not acknowledge the Holiday Lock command (offline or "
                "busy?)."
            )

        # Read the state back: the operator can accept the command and still not
        # apply it (e.g. when it has lost its origin), so don't assume success.
        try:
            overview = await self.coordinator.client.get_overview(
                self._serial, mac=mac
            )
        except CentsysError:
            overview = None
        if overview is None:
            await self.coordinator.async_request_refresh()
            return
        self.coordinator.set_overview(self._serial, overview)
        if before is not None and overview.holiday_lock == before:
            raise HomeAssistantError(
                "The gate accepted the Holiday Lock command but did not apply "
                "it. This can happen when the operator needs attention (for "
                "example if it has lost its origin and needs a re-learn)."
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.is_on is True:
            return
        await self._toggle()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self.is_on is False:
            return
        await self._toggle()


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
