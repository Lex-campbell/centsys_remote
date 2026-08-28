"""Config flow for Centsys Gate Remote (OTP onboarding)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CentsysRemoteClient, to_international_number
from .api.exceptions import CentsysError, OtpInvalidError
from .const import (
    CONF_COUNTRY,
    CONF_EMAIL,
    CONF_MOBILE_NUMBER,
    CONF_NAME,
    CONF_OTP_PLATFORM,
    CONF_TOKEN,
    DEFAULT_OTP_PLATFORM,
    DOMAIN,
    OTP_PLATFORM_SMS,
    OTP_PLATFORM_WHATSAPP,
)
from .countries import COUNTRIES, DEFAULT_COUNTRY

_DIAL_CODES = {iso: dial for iso, _name, dial in COUNTRIES}

_COUNTRY_OPTIONS = [
    selector.SelectOptionDict(value=iso, label=f"{name} (+{dial})")
    for iso, name, dial in COUNTRIES
]

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_COUNTRY, default=DEFAULT_COUNTRY): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=_COUNTRY_OPTIONS,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(CONF_MOBILE_NUMBER): str,
        vol.Optional(CONF_NAME): str,
        vol.Optional(CONF_EMAIL): str,
        vol.Required(
            CONF_OTP_PLATFORM, default=str(DEFAULT_OTP_PLATFORM)
        ): vol.In(
            {
                str(OTP_PLATFORM_WHATSAPP): "WhatsApp",
                str(OTP_PLATFORM_SMS): "SMS",
            }
        ),
    }
)

OTP_SCHEMA = vol.Schema({vol.Required("otp"): str})


class CentsysConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two-step flow: collect number -> send OTP, then verify OTP -> store token.

    Reauthentication reuses the same OTP exchange, skipping the number step
    because the entry already knows the number and delivery channel.
    """

    VERSION = 1

    def __init__(self) -> None:
        self._client: CentsysRemoteClient | None = None
        self._number: str | None = None
        self._name: str | None = None
        self._email: str | None = None
        self._otp_platform: int = DEFAULT_OTP_PLATFORM
        self._reauth_entry: ConfigEntry | None = None
        self._otp_sent = False

    async def _async_send_otp(self) -> str | None:
        """Start an OTP exchange for ``self._number``.

        Returns an error key for the form, or None once the PIN is on its way.
        """
        if not self._number:
            return "cannot_connect"
        self._client = CentsysRemoteClient(
            self._number, session=async_get_clientsession(self.hass)
        )
        try:
            sent = await self._client.send_otp(otp_platform=self._otp_platform)
        except CentsysError:
            return "cannot_connect"
        return None if sent else "otp_not_sent"

    async def _async_validate_otp(self, otp: str) -> tuple[str | None, str | None]:
        """Exchange an OTP for a session token. Returns (token, error key)."""
        if self._client is None:
            return None, "cannot_connect"
        try:
            return await self._client.validate_otp(otp.strip()), None
        except OtpInvalidError:
            return None, "invalid_otp"
        except CentsysError:
            return None, "cannot_connect"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            country = user_input[CONF_COUNTRY]
            self._number = to_international_number(
                user_input[CONF_MOBILE_NUMBER], _DIAL_CODES[country]
            )
            self._name = user_input.get(CONF_NAME)
            self._email = user_input.get(CONF_EMAIL)
            self._otp_platform = int(
                user_input.get(CONF_OTP_PLATFORM, DEFAULT_OTP_PLATFORM)
            )

            if not self._number.lstrip("+").isdigit() or len(self._number) < 8:
                errors["base"] = "invalid_number"
            else:
                await self.async_set_unique_id(self._number)
                self._abort_if_unique_id_configured()

                error = await self._async_send_otp()
                if error is None:
                    return await self.async_step_otp()
                errors["base"] = error

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token, error = await self._async_validate_otp(user_input["otp"])
            if error:
                errors["base"] = error
            else:
                # We create the entry even if no gates are linked yet: the
                # coordinator re-checks on every poll and gates appear
                # automatically once the number is added as a remote user. A
                # repair issue (see coordinator) explains the empty state in
                # the meantime.
                return self.async_create_entry(
                    title=self._name or self._number or "Centsys Gate",
                    data={
                        CONF_MOBILE_NUMBER: self._number,
                        CONF_TOKEN: token,
                        CONF_NAME: self._name,
                        CONF_EMAIL: self._email,
                        CONF_OTP_PLATFORM: self._otp_platform,
                    },
                )

        return self.async_show_form(
            step_id="otp", data_schema=OTP_SCHEMA, errors=errors
        )

    # -- reauthentication --------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a rejected session token by signing in again."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._number = entry_data[CONF_MOBILE_NUMBER]
        # Entries created before the channel was stored fall back to the default.
        self._otp_platform = int(
            entry_data.get(CONF_OTP_PLATFORM, DEFAULT_OTP_PLATFORM)
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send a fresh OTP, then swap the new token into the existing entry."""
        errors: dict[str, str] = {}
        if user_input is not None and self._otp_sent:
            token, error = await self._async_validate_otp(user_input["otp"])
            if error:
                errors["base"] = error
            elif self._reauth_entry is not None:
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={**self._reauth_entry.data, CONF_TOKEN: token},
                )
                await self.hass.config_entries.async_reload(
                    self._reauth_entry.entry_id
                )
                return self.async_abort(reason="reauth_successful")
        else:
            # First display of the form, or a retry after the PIN could not be
            # sent -- resubmitting asks for a new one rather than dead-ending.
            error = await self._async_send_otp()
            if error:
                errors["base"] = error
            else:
                self._otp_sent = True

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=OTP_SCHEMA,
            errors=errors,
            description_placeholders={"number": self._number or ""},
        )
