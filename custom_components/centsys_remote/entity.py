"""Base entity for Centsys Gate Remote."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import CentsysCoordinator

_LOGGER = logging.getLogger(__name__)


@callback
def async_setup_dynamic_entities(
    entry: ConfigEntry,
    coordinator: CentsysCoordinator,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[str], Iterable[Entity]],
) -> None:
    """Add entities for each gate, now and as new gates appear on later polls.

    The device list is re-fetched on every coordinator update, so a gate that
    gets linked to the account after setup (e.g. once the user is added as a
    remote user) shows up automatically without reloading the integration.
    """
    known: set[str] = set()

    @callback
    def _sync() -> None:
        new = [serial for serial in coordinator.data if serial not in known]
        if not new:
            return
        known.update(new)
        entities: list[Entity] = []
        for serial in new:
            entities.extend(factory(serial))
        if entities:
            async_add_entities(entities)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class CentsysEntity(CoordinatorEntity[CentsysCoordinator]):
    """Common base tying an entity to one gate operator (by serial)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CentsysCoordinator, serial: str) -> None:
        super().__init__(coordinator)
        self._serial = serial

    @property
    def _device_data(self) -> dict[str, Any] | None:
        return self.coordinator.data.get(self._serial)

    @property
    def available(self) -> bool:
        return super().available and self._device_data is not None

    async def _read_overview(self, mac, *, cached: bool = True):
        """This operator's telemetry, fetching it if needed, or None.

        A fetch wakes the operator's radio, so the cached frame is reused unless
        ``cached=False`` asks for a fresh read (e.g. to confirm a command took
        effect). Errors return None; callers decide what "unknown" means, since
        for some actions it should block and for others it should fall back.
        """
        data = self._device_data or {}
        if cached and data.get("overview") is not None:
            return data["overview"]
        try:
            overview = await self.coordinator.client.get_overview(
                self._serial, mac=mac
            )
        except Exception as err:  # noqa: BLE001 - an unreadable gate isn't an error
            _LOGGER.debug("Telemetry read failed for %s: %s", self._serial, err)
            return None
        self.coordinator.set_overview(self._serial, overview)
        return overview

    async def _resolve_family(self, mac) -> str | None:
        """Return "garage", "gate" or None for this operator.

        Prefers the live telemetry frame, which is a direct observation of the
        running operator; falls back to the product code the cloud reports for
        it when telemetry can't be read. None means unknown, so a caller sends
        the safe default (never the garage command) and withholds gate-only
        controls.
        """
        overview = await self._read_overview(mac)
        if overview is not None:
            if overview.is_garage:
                return "garage"
            if overview.is_gate:
                return "gate"
        family = getattr((self._device_data or {}).get("device"), "product_family", None)
        if family == "garage":
            return "garage"
        if family in ("slider", "swing"):
            return "gate"
        return None

    async def async_update(self) -> None:
        """Refresh on explicit request, including the slow MQTT telemetry.

        Coordinator entities don't poll, so this runs only when a user or
        automation calls ``homeassistant.update_entity``. Internal refreshes are
        left unforced so they stay light on the gate. The enabled check mirrors
        the base class, which ignores updates for a disabled entity -- forcing
        there would only wake the operator on some later cycle.
        """
        if self.enabled:
            self.coordinator.async_force_telemetry()
        await super().async_update()

    @property
    def device_info(self) -> DeviceInfo:
        device = self._device_data["device"] if self._device_data else None
        hw = (device.raw.get("deviceHardware") if device else None) or {}
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=device.device_name if device else self._serial,
            manufacturer=MANUFACTURER,
            model=device.device_name if device else None,
            serial_number=self._serial,
            sw_version=hw.get("coreFirmwareVersion"),
        )


# Presentation for a configured GSM/ULTRA output, matched on its IO name. Each
# tuple is (keywords, friendly label, icon); the first keyword found in the
# (upper-cased) IO name wins, so order most-specific first.
_GSM_IO_PRESENTATION: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("PED", "PEDEST"), "Pedestrian", "mdi:walk"),
    (("GRGS", "GRG", "GDO", "GARAGE"), "Garage", "mdi:garage"),
    (("LGHT", "LIGHT", "LGT", "LAMP"), "Light", "mdi:lightbulb"),
    (("LCK", "LOCK"), "Lock", "mdi:lock"),
    (("STOP",), "Stop", "mdi:stop"),
    (("HLD", "HOLD"), "Hold Open", "mdi:gate-open"),
    (("TRG", "TRIGGER", "GATE"), "Gate", "mdi:boom-gate"),
)
_GSM_IO_DEFAULT_ICON = "mdi:gesture-tap-button"


def gsm_io_presentation(io_name: str, io_number: int) -> tuple[str, str]:
    """Return a (label, icon) for a GSM output from its configured IO name."""
    name = (io_name or "").upper()
    for keywords, label, icon in _GSM_IO_PRESENTATION:
        if any(k in name for k in keywords):
            return label, icon
    return (io_name.strip() or f"Output {io_number}"), _GSM_IO_DEFAULT_ICON


class CentsysGsmEntity(CoordinatorEntity[CentsysCoordinator]):
    """Common base for a legacy GSM/ULTRA operator (keyed by ``gsm-<id>``)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CentsysCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key

    @property
    def _device_data(self) -> dict[str, Any] | None:
        return self.coordinator.data.get(self._key)

    @property
    def _gsm_device(self):
        data = self._device_data
        return data.get("gsm_device") if data else None

    @property
    def _status(self):
        """The cached live IO status (``GsmStatus``) for this operator, if any."""
        data = self._device_data
        return data.get("status") if data else None

    @property
    def available(self) -> bool:
        return super().available and self._device_data is not None

    @property
    def device_info(self) -> DeviceInfo:
        device = self._gsm_device
        name = device.name if device and device.name else self._key
        return DeviceInfo(
            identifiers={(DOMAIN, self._key)},
            name=name,
            manufacturer=MANUFACTURER,
            model="GSM/ULTRA operator",
        )


class CentsysGsmIoEntity(CentsysGsmEntity):
    """Base for an entity bound to a single configurable IO of an operator."""

    def __init__(self, coordinator: CentsysCoordinator, key: str, io) -> None:
        super().__init__(coordinator, key)
        self._io_number = io.io_number
        label, icon = gsm_io_presentation(io.io_name, io.io_number)
        self._attr_name = label
        self._attr_icon = icon


# Presentation for a shared-access action, keyed on the app's i18n name. Anything
# not listed falls back to a de-camel-cased label (see ``shared_action_presentation``).
_SHARED_ACTION_PRESENTATION: dict[str, tuple[str, str]] = {
    "OpenCloseDescription": ("Gate", "mdi:boom-gate"),
    "PedestrianDescription": ("Pedestrian", "mdi:walk"),
    "HolidayLockDescription": ("Holiday lock", "mdi:lock"),
    "KeepOpenDescription": ("Keep open", "mdi:gate-open"),
    "ArmTamperAlarmDescription": ("Arm tamper alarm", "mdi:shield"),
    "DisarmTamperAlarmDescription": ("Disarm tamper alarm", "mdi:shield-off"),
    "ClearAlarmsDescription": ("Clear alarms", "mdi:alarm-light-off"),
    "CancelAutoCloseDescription": ("Cancel auto-close", "mdi:timer-off-outline"),
}
_SHARED_ACTION_DEFAULT_ICON = "mdi:gesture-tap-button"


def shared_action_presentation(name: str) -> tuple[str, str]:
    """Return a (label, icon) for a shared action from its app i18n name."""
    if name in _SHARED_ACTION_PRESENTATION:
        return _SHARED_ACTION_PRESENTATION[name]
    base = re.sub(r"Description$", "", name or "")
    label = re.sub(r"(?<!^)(?=[A-Z])", " ", base).strip()
    return (label or name or "Action"), _SHARED_ACTION_DEFAULT_ICON


class CentsysSharedEntity(CoordinatorEntity[CentsysCoordinator]):
    """Common base for a community / shared-access gate (keyed ``shared-<guid>``).

    Shared gates are triggered server-side (AccessSharing ``SendActivation``),
    so there is no telemetry: entities are assumed-state and carry no live
    position, battery or beam data.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: CentsysCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key

    @property
    def _device_data(self) -> dict[str, Any] | None:
        return self.coordinator.data.get(self._key)

    @property
    def _access(self):
        """The current :class:`SharedAccess` for this site, if still present."""
        data = self._device_data
        return data.get("shared") if data else None

    @property
    def _number(self) -> str:
        return self.coordinator.client.mobile_number

    @property
    def _scope(self) -> str:
        """Account digits used to keep entity/device ids unique across accounts.

        A shared grant is per-account: the same share can be granted to several
        numbers, and one HA may hold more than one of them (e.g. the owner and a
        recipient), so ids are scoped by the account they belong to.
        """
        return re.sub(r"\D", "", self._number or "")

    def _uid(self, suffix: str) -> str:
        """Build an account-scoped unique id for a child entity."""
        return f"{self._key}-{self._scope}_{suffix}"

    @property
    def available(self) -> bool:
        access = self._access
        return bool(
            super().available
            and access is not None
            and access.is_available(self._number)
        )

    @property
    def device_info(self) -> DeviceInfo:
        access = self._access
        name = access.device_name if access and access.device_name else self._key
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._key}-{self._scope}")},
            name=name,
            manufacturer=MANUFACTURER,
            model="Shared gate",
        )
