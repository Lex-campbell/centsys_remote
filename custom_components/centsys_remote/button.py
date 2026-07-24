"""Buttons for CenSys Gate Remote."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api.exceptions import CentsysCertExpiredError, CentsysError
from .api.packets import ACTIVATION_PED, GDO_PRODUCT_TYPES
from .const import DOMAIN
from .coordinator import CentsysCoordinator
from .entity import (
    CentsysEntity,
    CentsysGsmEntity,
    CentsysGsmIoEntity,
    async_setup_dynamic_entities,
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
            # Pedestrian opening is a SMART sliding/swing activation; garage-door
            # operators use a different activation set and have no PED.
            device = data.get("device")
            overview = data.get("overview")
            if (
                getattr(device, "product_type", None) in GDO_PRODUCT_TYPES
                or getattr(overview, "family", None) == "sdo5"
            ):
                return []
            return [CentsysWifiPedestrianButton(coordinator, key)]
        if data.get("kind") != "gsm":
            return []
        entities: list[ButtonEntity] = [CentsysGsmAirtimeButton(coordinator, key)]
        device = data.get("gsm_device")
        if device is not None:
            # The gate trigger is the cover; expose every other momentary output
            # (pedestrian, garage, ...) as its own button. Two-state outputs are
            # switches instead, so they are skipped here.
            gate_io = device.trigger_io
            gate_number = gate_io.io_number if gate_io else None
            for io in device.ios:
                if io.io_number == gate_number or io.entity_kind != "button":
                    continue
                entities.append(CentsysGsmIoButton(coordinator, key, io))
        return entities

    async_setup_dynamic_entities(entry, coordinator, async_add_entities, _factory)


class CentsysWifiPedestrianButton(CentsysEntity, ButtonEntity):
    """Pedestrian (partial) open for a SMART Wi-Fi gate.

    Same MQTT handshake as the cover's full open, but with the PED activation
    id -- matching the Pedestrian button in the official app.
    """

    _attr_translation_key = "pedestrian"
    _attr_icon = "mdi:walk"

    def __init__(self, coordinator: CentsysCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_pedestrian"

    async def async_press(self) -> None:
        data = self._device_data or {}
        device = data.get("device")
        mac = getattr(device, "mac_address", None)
        if not mac:
            raise HomeAssistantError(
                "Gate has no MAC address in the cloud device list; cannot build "
                "the trigger packet."
            )
        try:
            ok = await self.coordinator.client.open_gate(
                self._serial,
                mac=mac,
                product_type=getattr(device, "product_type", None),
                activation_id=ACTIVATION_PED,
            )
        except CentsysCertExpiredError as err:
            raise HomeAssistantError(str(err)) from err
        except CentsysError as err:
            raise HomeAssistantError(f"Failed to trigger pedestrian open: {err}") from err
        if not ok:
            raise HomeAssistantError(
                "Gate did not acknowledge the pedestrian trigger (offline or busy?)."
            )
        await self.coordinator.async_request_refresh()
        self.coordinator.start_live_follow(self._serial)


class CentsysGsmAirtimeButton(CentsysGsmEntity, ButtonEntity):
    """Request a network-balance (airtime) refresh for a GSM/ULTRA operator.

    Billable (queries the balance over the cellular network), so it is on-demand
    only; the call/SMS token sensors update once the result syncs back.
    """

    _attr_translation_key = "gsm_refresh_airtime"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: CentsysCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._attr_unique_id = f"{key}_refresh_airtime"

    async def async_press(self) -> None:
        device = self._gsm_device
        if device is None:
            raise HomeAssistantError("This gate has no GSM device to query.")
        try:
            await self.coordinator.client.request_gsm_airtime(device.device_id)
        except CentsysError as err:
            raise HomeAssistantError(f"Couldn't request airtime: {err}") from err
        self.coordinator.async_schedule_airtime_refresh(self._key, device.device_id)


class CentsysGsmIoButton(CentsysGsmIoEntity, ButtonEntity):
    """A momentary auxiliary output (IO) on a GSM/ULTRA operator.

    Pressing sends an activation pulse to the operator's IO -- the same action as
    tapping that button in the official app. The main gate trigger is the cover
    entity, so only the other momentary outputs surface here.
    """

    def __init__(self, coordinator: CentsysCoordinator, key: str, io) -> None:
        super().__init__(coordinator, key, io)
        self._attr_unique_id = f"{key}_io_{io.io_number}"

    async def async_press(self) -> None:
        device = self._gsm_device
        if device is None:
            raise HomeAssistantError("This gate is no longer available.")
        try:
            await self.coordinator.client.trigger_gsm_activation(
                device.device_id, self._io_number
            )
        except CentsysError as err:
            raise HomeAssistantError(
                f"Failed to activate {self._attr_name}: {err}"
            ) from err
