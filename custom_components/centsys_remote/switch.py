"""Switches for Centsys Gate Remote."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api.exceptions import CentsysCertExpiredError, CentsysError
from .api.packets import ACTIVATION_HOLIDAY_LOCK, ACTIVATION_KEEP_OPEN
from .const import DOMAIN
from .coordinator import CentsysCoordinator
from .entity import CentsysEntity, CentsysGsmIoEntity, async_setup_dynamic_entities


@dataclass(frozen=True, kw_only=True)
class CentsysFlagSwitchDescription(SwitchEntityDescription):
    """A toggleable operator mode read from telemetry and set by an activation."""

    activation_id: int
    # Reads the current state off a DeviceOverview.
    state_fn: Callable[[Any], bool]


FLAG_SWITCHES: tuple[CentsysFlagSwitchDescription, ...] = (
    CentsysFlagSwitchDescription(
        key="holiday_lock",
        translation_key="holiday_lock",
        icon="mdi:lock-clock",
        activation_id=ACTIVATION_HOLIDAY_LOCK,
        state_fn=lambda ov: ov.holiday_lock,
    ),
    CentsysFlagSwitchDescription(
        key="keep_open",
        translation_key="keep_open",
        icon="mdi:gate-open",
        activation_id=ACTIVATION_KEEP_OPEN,
        state_fn=lambda ov: ov.keep_open,
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
            # These modes are a gate concept; on a garage the Holiday Lock id
            # would open the door, so skip a telemetry-confirmed garage.
            overview = data.get("overview")
            if overview is not None and overview.is_garage:
                return []
            return [CentsysFlagSwitch(coordinator, key, d) for d in FLAG_SWITCHES]
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


class CentsysFlagSwitch(CentsysEntity, SwitchEntity):
    """A toggleable mode (Holiday Lock, Keep Open) on a SMART Wi-Fi gate.

    State is read from the operator's live telemetry, so it follows the mode
    being changed anywhere -- a remote, a schedule, SmartGuard Air or the
    MyCentsys Pro app -- not just from here. The operator toggles the mode on a
    single activation, so on and off send the same command; the redundant
    direction is skipped when the reported state already matches.
    """

    entity_description: CentsysFlagSwitchDescription

    def __init__(
        self,
        coordinator: CentsysCoordinator,
        serial: str,
        description: CentsysFlagSwitchDescription,
    ) -> None:
        super().__init__(coordinator, serial)
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"

    @property
    def _overview(self):
        data = self._device_data
        return data.get("overview") if data else None

    @property
    def is_on(self) -> bool | None:
        """True/False from telemetry, or None until telemetry has been read."""
        overview = self._overview
        return self.entity_description.state_fn(overview) if overview is not None else None

    async def _gate_mac(self) -> str:
        """The operator MAC, having confirmed this is a gate and not a garage.

        The Holiday Lock id shares its number with the garage open command, so
        this refuses to act unless the operator is positively a gate.
        """
        mac = getattr((self._device_data or {}).get("device"), "mac_address", None)
        if not mac:
            raise HomeAssistantError(
                "Gate has no MAC address in the cloud device list; cannot build "
                "the command."
            )
        if await self._resolve_family(mac) != "gate":
            raise HomeAssistantError(
                "This control is only available on gate operators, once the gate "
                "has reported its status."
            )
        return mac

    async def _toggle(self) -> None:
        state_fn = self.entity_description.state_fn
        mac = await self._gate_mac()
        before = self.is_on
        try:
            ok = await self.coordinator.client.send_activation(
                self._serial, mac=mac, activation_id=self.entity_description.activation_id
            )
        except CentsysCertExpiredError as err:
            raise HomeAssistantError(str(err)) from err
        except CentsysError as err:
            raise HomeAssistantError(f"Failed to switch {self.name}: {err}") from err
        if not ok:
            raise HomeAssistantError(
                "Gate did not acknowledge the command (offline or busy?)."
            )

        # Read back rather than assume: the operator can accept the command and
        # still not apply it, e.g. when it has lost its origin.
        overview = await self._read_overview(mac, cached=False)
        if overview is None:
            await self.coordinator.async_request_refresh()
        elif before is not None and state_fn(overview) == before:
            raise HomeAssistantError(
                "The gate accepted the command but did not apply it. This can "
                "happen when the operator needs attention (for example if it has "
                "lost its origin and needs a re-learn)."
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
