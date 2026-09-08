"""Data update coordinator for Centsys Gate Remote."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CentsysRemoteClient, SharedAccess
from .api.exceptions import CentsysAuthError, CentsysError
from .const import (
    AIRTIME_POLL_ATTEMPTS,
    AIRTIME_POLL_INTERVAL,
    CONF_MOBILE_NUMBER,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    GSM_SCAN_INTERVAL,
    LIVE_FOLLOW_SECONDS,
    LIVE_STATUS_TTL,
    NO_GATES_HELP_URL,
    TELEMETRY_FORCE_MIN_INTERVAL,
    TELEMETRY_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _shape(value: Any) -> str:
    """Describe a response by structure, so diagnostics can be shared safely.

    A short string is one of the gateway's own status messages, so it is kept.
    """
    if isinstance(value, dict):
        return f"dict with keys {sorted(value)}"
    if isinstance(value, list):
        keys = sorted({k for v in value if isinstance(v, dict) for k in v})
        return f"list of {len(value)} entries, keys {keys}"
    if isinstance(value, str):
        return repr(value) if len(value) <= 80 else f"string, {len(value)} chars"
    return type(value).__name__


class CentsysCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls the Centsys backend for devices and live operator status.

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
        # Live gate-status pushed by a cover's follow after a press, keyed by the
        # entity key (Wi-Fi serial or GSM key): (label, monotonic expiry). It
        # takes precedence over the cloud poll while fresh so the cover *and*
        # the operator-status sensor reflect movement in real time.
        self._live_status: dict[str, tuple[str, float]] = {}
        # Serials currently streaming deviceOverview after a TRG/PED press.
        self._live_following: set[str] = set()
        # -inf, not 0: monotonic() is time since boot, so a zero start would
        # skip the first fetch when HA starts within a poll interval of boot.
        self._last_telemetry = float("-inf")
        # Retry cadence for telemetry, widened on each empty cycle (see
        # _maybe_refresh_telemetry).
        self._telemetry_interval = float(DEFAULT_SCAN_INTERVAL)
        # Set when a user explicitly asks an entity to update, so the next
        # cycle reads MQTT telemetry instead of waiting for its slow cadence.
        self._force_telemetry = False
        self._no_devices_issue = f"no_devices_{entry.entry_id}"
        self._backup_diagnostic_done = False
        self._gsm_devices: list[Any] = []
        self._gsm_loaded = False
        self._last_gsm = 0.0
        self._gsm_status: dict[str, Any] = {}
        self._gsm_diag: dict[str, Any] = {}
        self._last_gsm_diag = 0.0
        self._tasks: set[asyncio.Task] = set()

    def async_spawn(self, coro, *, name: str) -> None:
        """Start a best-effort background job owned by this config entry.

        Live follows and airtime polls run for over a minute, so they are
        tracked and cancelled by :meth:`async_shutdown` rather than left talking
        to the backend after the entry has been unloaded or reloaded.
        """
        task = self.hass.async_create_background_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def async_shutdown(self) -> None:
        """Cancel in-flight background jobs, then stop polling."""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        await super().async_shutdown()

    def set_live_gate_status(self, key: str, label: str | None) -> None:
        """Push (or clear) a live gate-status label and refresh entities.

        Pass ``None`` to drop the live label so entities fall back to the poll.
        Entities must read via :meth:`live_gate_status` (TTL-aware).
        """
        if label is None:
            self._live_status.pop(key, None)
        else:
            self._live_status[key] = (label, time.monotonic() + LIVE_STATUS_TTL)
        self.async_update_listeners()

    def live_gate_status(self, key: str) -> str | None:
        """Live gate-status label for ``key`` if still within its TTL, else None."""
        return self._live_status_label(key)

    def set_overview(self, serial: str, overview: Any) -> None:
        """Cache an on-demand MQTT overview and surface it to entities.

        Lets a cover that had no cached telemetry (cold start) store the
        overview it just fetched, so the garage/gate family is known for later
        presses and the pedestrian-button exposure without waiting for the slow
        telemetry poll.
        """
        if overview is None:
            return
        self._overview[serial] = overview
        # This is a real telemetry read, so it counts as one: no need to wake
        # the operator again on the usual cadence right after.
        self._last_telemetry = time.monotonic()
        if self.data and serial in self.data:
            self.data[serial]["overview"] = overview
        self.async_update_listeners()

    def async_force_telemetry(self) -> None:
        """Let the next update read MQTT telemetry, ignoring its slow cadence.

        Used when a user asks an entity to update, so a change made elsewhere --
        Holiday Lock set from a remote or the app, say -- is picked up on demand.
        ``TELEMETRY_FORCE_MIN_INTERVAL`` still applies as a floor.
        """
        self._force_telemetry = True

    def start_live_follow(self, serial: str) -> None:
        """Follow the MQTT status stream for one open/close cycle after a press.

        Shared by the Wi-Fi cover and the pedestrian button so both update live
        status through the same path. Concurrent follows for the same serial are
        coalesced.
        """
        if serial in self._live_following:
            return
        data = (self.data or {}).get(serial) or {}
        device = data.get("device")
        mac = getattr(device, "mac_address", None)
        self._live_following.add(serial)
        loop = self.hass.loop

        def _apply(overview) -> None:
            # The stream is already open, so keep the whole frame rather than
            # just the position: it also carries battery, beams and the Holiday
            # Lock bit, which would otherwise wait for the slow telemetry cycle.
            self.set_overview(serial, overview)
            self.set_live_gate_status(serial, overview.gate_status)

        def _on_overview(overview) -> None:  # called from a worker thread
            if overview is None:
                return
            loop.call_soon_threadsafe(_apply, overview)

        async def _runner() -> None:
            try:
                await self.client.follow_overview(
                    serial,
                    callback=_on_overview,
                    duration=LIVE_FOLLOW_SECONDS,
                    mac=mac,
                )
            except Exception:  # noqa: BLE001 - live follow is best-effort
                pass
            finally:
                self._live_following.discard(serial)
                # Drop live so the cloud poll is authoritative after the follow.
                self.set_live_gate_status(serial, None)
                await self.async_request_refresh()

        self.async_spawn(_runner(), name=f"centsys_follow_{serial}")

    def _live_status_label(self, key: str) -> str | None:
        """The live gate-status label for ``key`` if still within its TTL."""
        entry = self._live_status.get(key)
        if entry is None:
            return None
        label, expiry = entry
        if time.monotonic() >= expiry:
            del self._live_status[key]
            return None
        return label

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            devices = await self.client.get_devices()
            serials = [d.serial_number for d in devices if d.serial_number]
            statuses = {}
            if serials:
                for status in await self.client.get_operator_overview(serials):
                    statuses[status.operator_serial_number] = status
                    # Diagnostic aid for "theft alarm: Unknown": log the raw
                    # value so an absent field (None) can be told apart from an
                    # unmapped enum code for this operator model.
                    _LOGGER.debug(
                        "Operator %s: theftAlarmState=%r -> %s",
                        status.operator_serial_number,
                        status.theft_alarm_state,
                        status.theft_alarm_state_label,
                    )
        except CentsysAuthError as err:
            # Token rejected -> prompt the user to sign in again.
            raise ConfigEntryAuthFailed(str(err)) from err
        except CentsysError as err:
            raise UpdateFailed(str(err)) from err

        await self._maybe_refresh_telemetry(devices)
        await self._maybe_refresh_gsm()
        await self._refresh_gsm_status()
        await self._maybe_refresh_gsm_diag()

        data: dict[str, dict[str, Any]] = {
            d.serial_number: {
                "kind": "wifi",
                "device": d,
                "status": statuses.get(d.serial_number),
                "overview": self._overview.get(d.serial_number),
                "live_status": self._live_status_label(d.serial_number),
            }
            for d in devices
            if d.serial_number
        }
        for gsm in self._gsm_devices:
            data[gsm.key] = {
                "kind": "gsm",
                "gsm_device": gsm,
                "status": self._gsm_status.get(gsm.key),
                "diag": self._gsm_diag.get(gsm.key),
                "live_status": self._live_status_label(gsm.key),
            }

        has_devices = bool(data)
        if not has_devices:
            await self._log_backup_diagnostic()
        self._update_no_devices_issue(has_devices)

        return data

    def dismiss_no_devices_issue(self) -> None:
        """Clear the 'no gates linked' repair issue (e.g. on unload)."""
        ir.async_delete_issue(self.hass, DOMAIN, self._no_devices_issue)

    def _update_no_devices_issue(self, has_devices: bool) -> None:
        """Raise or clear the repair explaining an account with no linked gates.

        This is a standing configuration problem the user has to fix in the
        official app, not a one-off alert, so it belongs in Repairs. New gates
        are picked up on the next poll and the issue clears itself.
        """
        if has_devices:
            ir.async_delete_issue(self.hass, DOMAIN, self._no_devices_issue)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._no_devices_issue,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="no_gates_linked",
            learn_more_url=NO_GATES_HELP_URL,
        )

    async def _log_backup_diagnostic(self) -> None:
        """Diagnose an account with no Wi-Fi gates (once per session).

        ``GetDevicesByRemoteUserNumber`` only returns SMART Wi-Fi operators
        where this number is a linked *remote user*. GSM/ULTRA units (and older
        non-Wi-Fi motors reached via an add-on module) live on the legacy GWeb
        gateway instead, and community / shared-access "sites" live on the
        AccessSharing backend. This probes all fallback sources and logs what
        the backend holds, so a user can enable debug logging and share it.
        """
        if self._backup_diagnostic_done:
            return
        self._backup_diagnostic_done = True
        await self._log_legacy_config()
        await self._log_shared_access()
        await self._log_gweb_backup()

    async def _log_shared_access(self) -> None:
        """Log any community / shared-access sites granted to this number.

        These are gates the user does not own but may trigger (e.g. a shared
        estate "site" exposing Main Gate / Pedestrian Gate actions). They are
        returned by the AccessSharing backend, which the integration does not
        yet control, so this surfaces what the backend holds to guide support.
        """
        try:
            shared = await self.client.get_shared_accesses()
        except Exception as err:  # noqa: BLE001 - purely diagnostic
            _LOGGER.debug("Shared-access diagnostic fetch failed: %s", err)
            return

        if not shared:
            _LOGGER.info("No community / shared-access sites for this number.")
            return

        # The response may be a bare list of sites or a dict wrapping one; pull
        # out the first list of dicts so a site/action summary can be logged.
        entries = shared
        if isinstance(shared, dict):
            entries = next(
                (v for v in shared.values() if isinstance(v, list)),
                [],
            )
        sites = (
            [SharedAccess.from_json(s) for s in entries if isinstance(s, dict)]
            if isinstance(entries, list)
            else []
        )

        if sites:
            summary = ", ".join(
                f"{s.name or s.access_guid or '?'} "
                f"[{', '.join(a.name for a in s.actions) or 'no actions'}]"
                for s in sites
            )
            _LOGGER.info(
                "This number has %d community / shared-access site(s): %s. These "
                "are shared gates the integration does not control yet; the "
                "response shape is logged at debug level to help add support.",
                len(sites),
                summary,
            )
        else:
            _LOGGER.info(
                "This number has community / shared-access data the integration "
                "does not control yet; the response shape is logged at debug "
                "level to help add support."
            )
        _LOGGER.debug("AccessSharing response: %s", _shape(shared))

    async def _log_legacy_config(self) -> None:
        """Log the legacy GWeb device config (GSM/ULTRA devices show up here)."""
        try:
            buttons = await self.client.get_buttons()
        except Exception as err:  # noqa: BLE001 - purely diagnostic
            _LOGGER.debug("Legacy config diagnostic fetch failed: %s", err)
            return

        if isinstance(buttons, list) and buttons:
            _LOGGER.info(
                "No Wi-Fi gates for this number, but the legacy GWeb gateway "
                "returned %s configured button(s) - this looks like a GSM/ULTRA "
                "or non-Wi-Fi device, which this integration does not control "
                "yet. Field names logged at debug level.",
                len(buttons),
            )
            _LOGGER.debug("Legacy GWeb device config: %s", _shape(buttons))
        else:
            _LOGGER.info(
                "Legacy GWeb gateway returned no configured devices for this "
                "number either (response: %s).",
                _shape(buttons),
            )

    async def _log_gweb_backup(self) -> None:
        """Log the account's GWeb app backup (the app's restore source)."""
        try:
            backup = await self.client.get_backup()
        except Exception as err:  # noqa: BLE001 - purely diagnostic
            _LOGGER.debug("Backup diagnostic fetch failed: %s", err)
            return

        if not backup:
            _LOGGER.info("No cloud backup is stored for this number.")
            return

        operators = None
        if isinstance(backup, dict):
            blob = backup.get("SerializedVersionedBackup") or backup.get("Backup")
            decoded = backup
            if isinstance(blob, str):
                try:
                    decoded = json.loads(blob)
                except (json.JSONDecodeError, ValueError):
                    decoded = backup
            if isinstance(decoded, dict):
                operators = decoded.get("Operators") or decoded.get("Devices")

        count = len(operators) if isinstance(operators, list) else "unknown"
        _LOGGER.info(
            "A cloud backup exists for this number (operators in backup: %s).",
            count,
        )
        _LOGGER.debug("GWeb app backup: %s", _shape(backup))

    async def _maybe_refresh_gsm(self) -> None:
        """Refresh the legacy GSM/ULTRA device list, best-effort.

        Rate-limited to ``GSM_SCAN_INTERVAL``; the cached list is reused between
        refreshes and a failure keeps the previous value. Wi-Fi-only accounts
        simply get an empty list here.
        """
        now = time.monotonic()
        if self._gsm_loaded and (now - self._last_gsm) < GSM_SCAN_INTERVAL:
            return
        self._last_gsm = now
        try:
            self._gsm_devices = await self.client.get_gsm_config()
            self._gsm_loaded = True
        except Exception as err:  # noqa: BLE001 - legacy config is best-effort
            _LOGGER.debug("GSM config fetch failed: %s", err)

    async def _refresh_gsm_status(self) -> None:
        """Refresh live IO states (gate position) for each GSM/ULTRA device.

        The ``AppIOStatesEN`` poll is lightweight (no auth, short timeout) and is
        the same status feed the app uses. Failures are swallowed and keep the
        previous value; a device with no status-feedback input simply never
        reports a gate position.
        """
        for gsm in self._gsm_devices:
            status = await self._fetch_gsm_status(gsm.device_id)
            if status is not None:
                self._gsm_status[gsm.key] = status
                _LOGGER.debug(
                    "GSM %s (id=%s) IO states=%s -> gate=%s (online=%s)",
                    gsm.name,
                    gsm.device_id,
                    status.io_states,
                    status.gate_state,
                    status.online,
                )

    async def _fetch_gsm_status(self, device_id: int | str) -> Any:
        """Fetch one GSM device's live IO states, best-effort (returns None on error)."""
        try:
            return await self.client.get_gsm_io_states(device_id)
        except Exception as err:  # noqa: BLE001 - status poll is best-effort
            _LOGGER.debug("GSM IO-state fetch failed for %s: %s", device_id, err)
            return None

    async def _maybe_refresh_gsm_diag(self) -> None:
        """Refresh GSM diagnostics (voltage/signal/airtime) on the slow cadence."""
        now = time.monotonic()
        if self._gsm_diag and (now - self._last_gsm_diag) < TELEMETRY_SCAN_INTERVAL:
            return
        self._last_gsm_diag = now
        for gsm in self._gsm_devices:
            try:
                diag = await self.client.get_gsm_status(gsm.device_id)
            except Exception as err:  # noqa: BLE001 - diagnostics are best-effort
                _LOGGER.debug("GSM diagnostics fetch failed for %s: %s", gsm.device_id, err)
                continue
            if diag is not None:
                self._gsm_diag[gsm.key] = diag

    def async_schedule_airtime_refresh(self, key: str, device_id: int | str) -> None:
        """Poll cached diagnostics after an on-demand airtime request.

        The balance answer lands a little after it is queued, so refresh in the
        background until the tokens appear (or attempts run out).
        """
        self.async_spawn(
            self._poll_airtime(key, device_id), name=f"{DOMAIN}_airtime_{key}"
        )

    async def _poll_airtime(self, key: str, device_id: int | str) -> None:
        for _ in range(AIRTIME_POLL_ATTEMPTS):
            await asyncio.sleep(AIRTIME_POLL_INTERVAL)
            try:
                diag = await self.client.get_gsm_status(device_id)
            except Exception as err:  # noqa: BLE001 - best-effort follow-up poll
                _LOGGER.debug("Airtime follow-up fetch failed for %s: %s", device_id, err)
                continue
            if diag is None:
                continue
            self._gsm_diag[key] = diag
            self._last_gsm_diag = time.monotonic()
            if self.data and key in self.data:
                self.data[key]["diag"] = diag
            self.async_update_listeners()
            if diag.call_tokens is not None or diag.sms_tokens is not None:
                return

    async def _maybe_refresh_telemetry(self, devices: list[Any]) -> None:
        """Refresh cached MQTT telemetry for Wi-Fi operators, best-effort.

        Each fetch opens a TLS session and wakes the operator's radio, so it is
        rate-limited to ``TELEMETRY_SCAN_INTERVAL`` once values are flowing.
        Before that it retries at the poll interval so a fresh install fills in
        quickly, doubling the wait after every cycle that yields nothing -- an
        operator that is asleep, offline or has no MAC would otherwise be woken
        every poll, forever. Failures are expected and keep the cached values.
        """
        now = time.monotonic()
        forced, self._force_telemetry = self._force_telemetry, False
        interval = (
            float(TELEMETRY_FORCE_MIN_INTERVAL) if forced else self._telemetry_interval
        )
        if (now - self._last_telemetry) < interval:
            return
        self._last_telemetry = now

        got_any = False
        for device in devices:
            serial = device.serial_number
            if not serial or not getattr(device, "is_wifi_device", False):
                continue
            try:
                overview = await self.client.get_overview(
                    serial, mac=getattr(device, "mac_address", None)
                )
            except CentsysError as err:
                _LOGGER.debug("Telemetry fetch failed for %s: %s", serial, err)
                continue
            except Exception as err:  # noqa: BLE001 - telemetry is best-effort
                _LOGGER.debug("Telemetry error for %s: %s", serial, err)
                continue
            if overview is not None:
                got_any = True
                self._overview[serial] = overview
                # Diagnostic aid for "battery voltage: Unknown" reports: log the
                # decoded family and raw battery value so a genuine 0 (no
                # battery) can be told apart from a value that failed to decode.
                _LOGGER.debug(
                    "Telemetry %s: family=%s battery_raw=%s (%.2fV) input_raw=%s "
                    "power_raw=%s",
                    serial,
                    overview.family,
                    overview.battery_voltage_raw,
                    overview.battery_voltage or 0.0,
                    overview.input_voltage_raw,
                    overview.power_status_raw,
                )

        self._telemetry_interval = (
            float(TELEMETRY_SCAN_INTERVAL)
            if got_any
            else min(self._telemetry_interval * 2, float(TELEMETRY_SCAN_INTERVAL))
        )
