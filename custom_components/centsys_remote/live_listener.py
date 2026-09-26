"""Persistent live-telemetry listener for SMART Wi-Fi operators (opt-in).

Holds one MQTT connection on a distinct clientId (``mcr:<number>:ha``), so it
never collides with the phone app, and streams every owned operator's
``deviceOverview`` into the coordinator in real time -- catching gate movement
from a physical remote, the app or a schedule that the periodic cloud poll would
miss. It also wakes each operator periodically for idle battery/beam telemetry.

Separation of concerns: the transport lives in the client
(``run_live_listener`` -> ``mqtt_remote.listen_overview_blocking``); this class
owns *policy* -- reconnect/backoff, certificate refresh and lifecycle -- and
feeds the coordinator's single ingestion sink (``_thread_safe_ingest``). It
touches no entity state directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .api.exceptions import CentsysCertExpiredError
from .const import (
    LISTENER_RECONNECT_MAX,
    LISTENER_RECONNECT_MIN,
    TELEMETRY_SCAN_INTERVAL,
)

if TYPE_CHECKING:
    from .coordinator import CentsysCoordinator

_LOGGER = logging.getLogger(__name__)

# A connection that stayed up at least this long is treated as healthy, so the
# reconnect backoff resets to the minimum rather than growing after a normal drop.
_HEALTHY_UPTIME = 60.0


class LiveListener:
    """Supervises the persistent live connection for one config entry."""

    def __init__(self, coordinator: "CentsysCoordinator") -> None:
        self._coord = coordinator
        self._running = False
        self._stop = False
        # threading.Event handed to the active blocking connection so it can be
        # told to return promptly on shutdown.
        self._stop_event: Any = None
        self._connected = False

    @property
    def active(self) -> bool:
        """Whether a live connection is currently established and subscribed.

        False until the first connection subscribes and after any drop, so the
        legacy poll/follow keep working as a fallback until the listener is up.
        """
        return self._connected

    def start(self) -> None:
        """Begin supervising (idempotent). Runs as a tracked background task."""
        if self._running:
            return
        self._running = True
        self._stop = False
        self._coord.async_spawn(self._run(), name="centsys_live_listener")

    async def async_stop(self) -> None:
        """Signal the supervisor and the active connection to stop."""
        self._stop = True
        self._running = False
        self._set_connected(False)
        ev = self._stop_event
        if ev is not None:
            ev.set()

    def _targets(self) -> tuple[list[str], dict[str, Any]]:
        """(serials, macs) for the account's Wi-Fi operators, from coordinator data."""
        serials: list[str] = []
        macs: dict[str, Any] = {}
        for serial, entry in (self._coord.data or {}).items():
            if entry.get("kind") != "wifi":
                continue
            device = entry.get("device")
            if device is None:
                continue
            serials.append(serial)
            mac = getattr(device, "mac_address", None)
            if mac:
                macs[serial] = mac
        return serials, macs

    def _on_overview(self, serial: str, overview: Any) -> None:  # worker thread
        self._coord._thread_safe_ingest(serial, overview)

    def _on_connected(self) -> None:  # worker thread
        self._coord.hass.loop.call_soon_threadsafe(self._set_connected, True)

    def _set_connected(self, value: bool) -> None:
        if self._connected == value:
            return
        self._connected = value
        # Refresh entities so the cover leaves / falls back to the poll cleanly.
        self._coord.async_update_listeners()

    async def _run(self) -> None:
        import threading

        backoff = LISTENER_RECONNECT_MIN
        while not self._stop:
            serials, macs = self._targets()
            if not serials:
                # No Wi-Fi operators linked yet; check again shortly.
                await asyncio.sleep(LISTENER_RECONNECT_MIN)
                continue

            stop_event = threading.Event()
            self._stop_event = stop_event
            started = self._coord.hass.loop.time()
            try:
                await self._coord.client.run_live_listener(
                    serials,
                    macs=macs,
                    on_overview=self._on_overview,
                    on_connected=self._on_connected,
                    stop_event=stop_event,
                    wake_interval=float(TELEMETRY_SCAN_INTERVAL),
                )
            except CentsysCertExpiredError:
                # The client has already invalidated the cached cert; the next
                # attempt fetches a fresh one.
                _LOGGER.debug("Live listener: broker rejected the certificate; will refetch")
            except Exception as err:  # noqa: BLE001 - listener is best-effort
                _LOGGER.debug("Live listener error: %s", err)
            finally:
                self._set_connected(False)
                self._stop_event = None

            if self._stop:
                break

            # A connection that lasted a while is healthy: reset the backoff so a
            # normal drop reconnects quickly; only repeated fast failures grow it.
            if self._coord.hass.loop.time() - started >= _HEALTHY_UPTIME:
                backoff = LISTENER_RECONNECT_MIN
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, LISTENER_RECONNECT_MAX)
