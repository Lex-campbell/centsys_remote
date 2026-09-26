"""The Centsys Gate Remote integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .coordinator import CentsysCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Centsys Gate Remote from a config entry."""
    coordinator = CentsysCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Start the opt-in live listener (no-op unless enabled in options), and
    # reload the entry when options change so it starts/stops accordingly.
    coordinator.start_live_sources()
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change (e.g. the live-listener toggle)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: CentsysCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.dismiss_no_devices_issue()
        # Live follows and airtime polls run for over a minute; stop them so a
        # reload does not leave the old entry's jobs talking to the backend.
        await coordinator.async_shutdown()
    return unload_ok
