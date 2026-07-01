"""Data update coordinator for CenSys Gate Remote."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CentsysRemoteClient
from .api.exceptions import CentsysAuthError, CentsysError
from .const import (
    CONF_MOBILE_NUMBER,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    TELEMETRY_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class CentsysCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls the CenSys backend for devices and live operator status.

    The cloud HTTP poll (device list + operator status) runs every
    ``DEFAULT_SCAN_INTERVAL``. Live MQTT telemetry (battery voltage etc.) is
    much heavier, so it is refreshed only every ``TELEMETRY_SCAN_INTERVAL`` and
    cached between cycles; a telemetry failure never fails the HTTP update.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.entry = entry
        session = async_get_clientsession(hass)
        self.client = CentsysRemoteClient(
            entry.data[CONF_MOBILE_NUMBER],
            session=session,
            session_token=entry.data[CONF_TOKEN],
        )
        self._overview: dict[str, Any] = {}
        self._last_telemetry = 0.0
        self._no_devices_notice = f"{DOMAIN}_no_devices_{entry.entry_id}"

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            devices = await self.client.get_devices()
            serials = [d.serial_number for d in devices if d.serial_number]
            statuses = {}
            if serials:
                for status in await self.client.get_operator_overview(serials):
                    statuses[status.operator_serial_number] = status
        except CentsysAuthError as err:
            # Token rejected -> trigger reauth in the UI.
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except CentsysError as err:
            raise UpdateFailed(str(err)) from err

        await self._maybe_refresh_telemetry(devices)

        self._update_no_devices_notice(bool(serials))

        return {
            d.serial_number: {
                "device": d,
                "status": statuses.get(d.serial_number),
                "overview": self._overview.get(d.serial_number),
            }
            for d in devices
            if d.serial_number
        }

    def dismiss_no_devices_notice(self) -> None:
        """Clear the 'no gates linked' notification (e.g. on unload)."""
        persistent_notification.async_dismiss(self.hass, self._no_devices_notice)

    def _update_no_devices_notice(self, has_devices: bool) -> None:
        """Show/clear a notification explaining an account with no linked gates.

        New gates are picked up automatically on the next poll, so this simply
        tells the user what to do and then clears itself once a gate appears.
        """
        if has_devices:
            persistent_notification.async_dismiss(self.hass, self._no_devices_notice)
            return
        persistent_notification.async_create(
            self.hass,
            (
                "You're signed in, but no gates are linked to this number yet.\n\n"
                "Open the **MyCentsys Remote** app and make sure your gate appears "
                "there for this phone number - the gate's admin needs to add your "
                "number as a **remote user** (a direct/Bluetooth-only connection "
                "isn't enough). Once it's linked, it will appear here automatically "
                "within a minute; no restart needed."
            ),
            title="CenSys Gate Remote: no gates linked",
            notification_id=self._no_devices_notice,
        )

    async def _maybe_refresh_telemetry(self, devices: list[Any]) -> None:
        """Refresh cached MQTT telemetry for Wi-Fi operators, best-effort.

        Rate-limited to ``TELEMETRY_SCAN_INTERVAL``. Each fetch wakes the gate
        and waits for a status broadcast, so failures are expected (asleep /
        offline) and are swallowed, keeping the last known values.
        """
        now = time.monotonic()
        if self._overview and (now - self._last_telemetry) < TELEMETRY_SCAN_INTERVAL:
            return
        self._last_telemetry = now

        for device in devices:
            serial = device.serial_number
            if not serial or not getattr(device, "is_wifi_device", False):
                continue
            try:
                overview = await self.client.get_overview(serial)
            except CentsysError as err:
                _LOGGER.debug("Telemetry fetch failed for %s: %s", serial, err)
                continue
            except Exception as err:  # noqa: BLE001 - telemetry is best-effort
                _LOGGER.debug("Telemetry error for %s: %s", serial, err)
                continue
            if overview is not None:
                self._overview[serial] = overview
