"""Buttons for Centsys Gate Remote."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api.exceptions import CentsysCertExpiredError, CentsysError
from .api.packets import ACTIVATION_PED
from .const import DOMAIN
from .coordinator import CentsysCoordinator
from .entity import (
    CentsysEntity,
    CentsysGsmEntity,
    CentsysGsmIoEntity,
    CentsysSharedEntity,
    async_setup_dynamic_entities,
    shared_action_presentation,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CentsysCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _factory(key: str):
        data = coordinator.data.get(key) or {}
        kind = data.get("kind")
        if kind == "wifi":
            # Garage-door operators have no pedestrian mode. The telemetry
            # family is the signal; productType is unreliable, as the same type
            # ships as either a gate or a garage.
            overview = data.get("overview")
            if overview is not None and overview.is_garage:
                return []
            return [CentsysWifiPedestrianButton(coordinator, key)]
        if kind == "shared":
            # Only GSM/ULTRA shares are controllable; SMART shares are read-only
            # (BLE-at-the-gate). The gate action is the cover; every other action
            # (pedestrian, keep-open, ...) becomes its own button.
            access = data.get("shared")
            if access is None or not access.is_ultra:
                return []
            return [
                CentsysSharedActionButton(coordinator, key, action)
                for action in access.button_actions
            ]
        if kind != "gsm":
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


class CentsysSharedActionButton(CentsysSharedEntity, ButtonEntity):
    """A non-gate action on a shared-access gate (pedestrian, keep-open, ...).

    Pressing triggers the action server-side via the AccessSharing
    ``SendActivation`` call -- the same as tapping it in the official app.
    """

    def __init__(self, coordinator: CentsysCoordinator, key: str, action) -> None:
        super().__init__(coordinator, key)
        self._action_id = action.id
        label, icon = shared_action_presentation(action.name)
        self._attr_name = label
        self._attr_icon = icon
        self._attr_unique_id = self._uid(f"action_{action.id}")

    async def async_press(self) -> None:
        access = self._access
        # Re-resolve the action from the freshly-polled access (objects are
        # replaced each update); the id is stable.
        action = access.action_by_id(self._action_id) if access else None
        if access is None or action is None:
            raise HomeAssistantError("This shared action is no longer available.")
        try:
            await self.coordinator.client.trigger_shared_action(access, action)
        except CentsysError as err:
            raise HomeAssistantError(
                f"Failed to activate {self._attr_name}: {err}"
            ) from err
