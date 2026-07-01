"""Config flow for CenSys Gate Remote (OTP onboarding)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
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
    """Two-step flow: collect number -> send OTP, then verify OTP -> store token."""

    VERSION = 1

    def __init__(self) -> None:
        self._client: CentsysRemoteClient | None = None
        self._number: str | None = None
        self._name: str | None = None
        self._email: str | None = None
        self._otp_platform: int = DEFAULT_OTP_PLATFORM

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

                session = async_get_clientsession(self.hass)
                self._client = CentsysRemoteClient(self._number, session=session)
                try:
                    sent = await self._client.send_otp(otp_platform=self._otp_platform)
                except CentsysError:
                    errors["base"] = "cannot_connect"
                else:
                    if sent:
                        return await self.async_step_otp()
                    errors["base"] = "otp_not_sent"

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and self._client is not None:
            try:
                token = await self._client.validate_otp(user_input["otp"].strip())
            except OtpInvalidError:
                errors["base"] = "invalid_otp"
            except CentsysError:
                errors["base"] = "cannot_connect"
            else:
                # We create the entry even if no gates are linked yet: the
                # coordinator re-checks on every poll and gates appear
                # automatically once the number is added as a remote user. A
                # persistent notification (see coordinator) explains the empty
                # state in the meantime.
                return self.async_create_entry(
                    title=self._name or self._number or "CenSys Gate",
                    data={
                        CONF_MOBILE_NUMBER: self._number,
                        CONF_TOKEN: token,
                        CONF_NAME: self._name,
                        CONF_EMAIL: self._email,
                    },
                )

        return self.async_show_form(
            step_id="otp", data_schema=OTP_SCHEMA, errors=errors
        )
