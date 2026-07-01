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

from .api.exceptions import CentsysError
from .const import DOMAIN
from .coordinator import CentsysCoordinator
from .entity import CentsysEntity, async_setup_dynamic_entities

# How long to follow the live MQTT status stream after a press (covers the
# open + auto-close cycle) and how long a live frame stays authoritative before
# we fall back to the HTTP poll.
LIVE_FOLLOW_SECONDS = 75.0
LIVE_TTL_SECONDS = 20.0


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
        lambda serial: [CentsysGateCover(coordinator, serial)],
    )


class CentsysGateCover(CentsysEntity, CoverEntity):
    """The gate operator as an HA cover."""

    _attr_device_class = CoverDeviceClass.GATE
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
    _attr_name = None  # primary entity -> use the device name

    def __init__(self, coordinator: CentsysCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_gate"
        # Live status from the MQTT deviceOverview stream (takes precedence over
        # the slower HTTP poll while fresh). One of the APPGATE_STATUS labels.
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
                await self.coordinator.client.follow_overview(
                    self._serial,
                    callback=_on_overview,
                    duration=LIVE_FOLLOW_SECONDS,
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
        try:
            ok = await self.coordinator.client.open_gate(self._serial)
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
