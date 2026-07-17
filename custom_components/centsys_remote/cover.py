"""Cover entity representing the gate's open/closed state.

State comes from ``GetOperatorOverview`` (``operatorStatus``) for steady state,
and from the live MQTT ``deviceOverview`` stream while the gate is moving (see
``_start_live_follow``). The operator is a single-button trigger, so both open
and close pulse the same MQTT trigger (the gate decides direction from its own
state) -- exactly how the physical remote and the app's button behave.

We deliberately do NOT use ``assumed_state`` here: with the live follow keeping
the reported state accurate, HA's default greying (open disabled when open,
close disabled when closed) correctly reflects the real gate position.
"""

from __future__ import annotations

import asyncio
import time

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api.exceptions import CentsysCertExpiredError, CentsysError
from .const import DOMAIN
from .coordinator import CentsysCoordinator
from .entity import CentsysEntity, CentsysGsmEntity, async_setup_dynamic_entities

# How long to follow the live MQTT status stream after a press (covers the
# open + auto-close cycle) and how long a live frame stays authoritative before
# we fall back to the HTTP poll.
LIVE_FOLLOW_SECONDS = 75.0
LIVE_TTL_SECONDS = 20.0
# How often to re-poll a GSM operator's live IO states during the follow window.
GSM_LIVE_POLL_SECONDS = 3.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CentsysCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _factory(serial: str):
        data = coordinator.data.get(serial) or {}
        if data.get("kind") == "gsm":
            return [CentsysGsmGateCover(coordinator, serial)]
        return [CentsysGateCover(coordinator, serial)]

    async_setup_dynamic_entities(entry, coordinator, async_add_entities, _factory)


class CentsysGateCover(CentsysEntity, CoverEntity):
    """The gate operator as an HA cover."""

    _attr_device_class = CoverDeviceClass.GATE
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
    _attr_name = None  # primary entity -> use the device name

    def __init__(self, coordinator: CentsysCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_gate"
        # Live gate status from the MQTT deviceOverview stream (takes precedence
        # over the slower HTTP poll while fresh).
        self._live_status: str | None = None
        self._live_expiry = 0.0
        self._following = False

    @property
    def _status(self):
        data = self._device_data
        return data.get("status") if data else None

    def _live(self) -> str | None:
        """The most recent live gate status, if still within its TTL."""
        if self._live_status and time.monotonic() < self._live_expiry:
            return self._live_status
        return None

    @property
    def is_closed(self) -> bool | None:
        live = self._live()
        if live is not None:
            return live == "closed"
        status = self._status
        return status.is_closed if status else None

    @property
    def is_opening(self) -> bool:
        live = self._live()
        if live is not None:
            return live == "opening"
        status = self._status
        return bool(status and status.is_opening)

    @property
    def is_closing(self) -> bool:
        live = self._live()
        if live is not None:
            return live == "closing"
        status = self._status
        return bool(status and status.is_closing)

    def _apply_live_overview(self, overview) -> None:
        """Push a live deviceOverview frame onto the entity (event-loop side)."""
        if overview is None or overview.gate_status is None:
            return
        self._live_status = overview.gate_status
        self._live_expiry = time.monotonic() + LIVE_TTL_SECONDS
        self.async_write_ha_state()

    def _start_live_follow(self) -> None:
        """Follow the MQTT status stream for one open/close cycle."""
        if self._following:
            return
        self._following = True
        loop = self.hass.loop

        def _on_overview(overview) -> None:  # called from a worker thread
            loop.call_soon_threadsafe(self._apply_live_overview, overview)

        async def _runner() -> None:
            try:
                device = self._device_data["device"] if self._device_data else None
                await self.coordinator.client.follow_overview(
                    self._serial,
                    callback=_on_overview,
                    duration=LIVE_FOLLOW_SECONDS,
                    mac=getattr(device, "mac_address", None),
                )
            except Exception:  # noqa: BLE001 - live follow is best-effort
                pass
            finally:
                self._following = False
                # Final reconcile against the authoritative cloud status.
                await self.coordinator.async_request_refresh()

        self.hass.async_create_background_task(
            _runner(), name=f"centsys_follow_{self._serial}"
        )

    async def _trigger(self) -> None:
        data = self._device_data or {}
        device = data.get("device")
        mac = getattr(device, "mac_address", None)
        if not mac:
            raise HomeAssistantError(
                "Gate has no MAC address in the cloud device list; cannot build "
                "the trigger packet."
            )
        # Only garage-door operators emit the "sdo5" telemetry frame.
        is_garage = getattr(data.get("overview"), "family", None) == "sdo5"
        try:
            ok = await self.coordinator.client.open_gate(
                self._serial,
                mac=mac,
                product_type=getattr(device, "product_type", None),
                is_garage=is_garage,
            )
        except CentsysCertExpiredError as err:
            raise HomeAssistantError(str(err)) from err
        except CentsysError as err:
            raise HomeAssistantError(f"Failed to trigger gate: {err}") from err
        if not ok:
            raise HomeAssistantError(
                "Gate did not acknowledge the trigger (offline or busy?)."
            )
        await self.coordinator.async_request_refresh()
        self._start_live_follow()

    async def async_open_cover(self, **kwargs) -> None:
        await self._trigger()

    async def async_close_cover(self, **kwargs) -> None:
        await self._trigger()


class CentsysGsmGateCover(CentsysGsmEntity, CoverEntity):
    """A legacy GSM/ULTRA gate as an HA cover.

    Operators with a status-feedback input report their live position (via the
    ``AppIOStatesEN`` poll), so the cover greys the open/close buttons to match
    the real state and follows the poll more closely just after a trigger. When
    an operator provides no feedback, the cover falls back to ``assumed_state``
    (both buttons always pressable). Either way, both open and close activate the
    device's gate-trigger IO (a momentary pulse), like the remote/app button.
    """

    _attr_device_class = CoverDeviceClass.GATE
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
    _attr_name = None  # primary entity -> use the device name

    def __init__(self, coordinator: CentsysCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._attr_unique_id = f"{key}_gate"
        # Live gate position from the AppIOStatesEN poll (takes precedence over
        # the slower coordinator poll while fresh). One of GSM_GATE_STATES.
        self._live_status: str | None = None
        self._live_expiry = 0.0
        self._polling = False

    def _live(self) -> str | None:
        if self._live_status and time.monotonic() < self._live_expiry:
            return self._live_status
        return None

    @property
    def _gate_state(self) -> str | None:
        """Best current gate position ('open'/'closed'/...), or None if unknown."""
        live = self._live()
        if live is not None:
            return live
        status = self._status
        return status.gate_state if status else None

    @property
    def assumed_state(self) -> bool:
        # Only assume state when the operator reports no position feedback.
        return self._gate_state is None

    @property
    def is_closed(self) -> bool | None:
        state = self._gate_state
        return None if state is None else state == "closed"

    @property
    def is_opening(self) -> bool:
        return self._gate_state == "opening"

    @property
    def is_closing(self) -> bool:
        return self._gate_state == "closing"

    def _start_live_poll(self) -> None:
        """Poll the live IO states for one open/close cycle after a trigger."""
        if self._polling:
            return
        device = self._gsm_device
        if device is None:
            return
        self._polling = True

        async def _runner() -> None:
            try:
                deadline = time.monotonic() + LIVE_FOLLOW_SECONDS
                while time.monotonic() < deadline:
                    status = await self.coordinator.client.get_gsm_io_states(
                        device.device_id
                    )
                    if status is not None and status.gate_state is not None:
                        self._live_status = status.gate_state
                        self._live_expiry = time.monotonic() + LIVE_TTL_SECONDS
                        self.async_write_ha_state()
                    await asyncio.sleep(GSM_LIVE_POLL_SECONDS)
            except Exception:  # noqa: BLE001 - live poll is best-effort
                pass
            finally:
                self._polling = False
                await self.coordinator.async_request_refresh()

        self.hass.async_create_background_task(
            _runner(), name=f"centsys_gsm_follow_{self._key}"
        )

    async def _trigger(self) -> None:
        device = self._gsm_device
        io = device.trigger_io if device else None
        if device is None or io is None:
            raise HomeAssistantError("No trigger button configured for this gate.")
        try:
            await self.coordinator.client.trigger_gsm_activation(
                device.device_id, io.io_number
            )
        except CentsysError as err:
            raise HomeAssistantError(f"Failed to trigger gate: {err}") from err
        # Follow the live status only if this operator actually reports one.
        status = self._status
        if status is not None and status.has_feedback:
            self._start_live_poll()

    async def async_open_cover(self, **kwargs) -> None:
        await self._trigger()

    async def async_close_cover(self, **kwargs) -> None:
        await self._trigger()
