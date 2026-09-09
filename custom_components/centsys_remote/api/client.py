"""Async client for the Centsys gate backend.

Capabilities:
  * OTP login (SendOtp / ValidateOtp) -> long-lived session JWT
  * GWeb token derivation (GetGwebToken -> MCROTPNumb)
  * Discovery: list devices, list configured buttons
  * Live operator status
  * Gate open and live telemetry over MQTT (mutual TLS) -- see ``mqtt_remote``

Notes:
  * The session JWT (HS256) is long-lived; ValidateOtp returns a freshened copy.
  * SendOtp/ValidateOtp run before a user token exists, so they use a
    service-level bearer (see ``_otp_bearer``).
  * SMART Wi-Fi operators open over MQTT (mutual TLS, MQTT v5), not HTTP, via a
    short challenge-response; see ``open_gate()`` and ``mqtt_remote``.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import aiohttp
from yarl import URL

from . import const
from .exceptions import (
    CentsysApiError,
    CentsysAuthError,
    CentsysCertExpiredError,
    CentsysError,
    OtpInvalidError,
)
from .models import (
    Device,
    DeviceInfo,
    GsmDevice,
    GsmDeviceStatus,
    GsmStatus,
    OperatorStatus,
    SharedAccess,
    SharedAction,
)

_LOGGER = logging.getLogger(__name__)

# AccessSharing ``ActionResponseEnum`` from the app: index -> meaning.
ACTION_RESPONSE_SUCCESS = 1
ACTION_RESPONSE_LABELS = {
    0: "failed",
    1: "success",
    2: "access revoked",
    3: "invalid device (IMEI)",
    4: "access expired",
    5: "communication failed",
    6: "refresh required",
    7: "access revoked by administrator",
}

# AccessSharing ``AccessLogResultEnum``; a successful open is reported as
# ``Success`` (2, "ActionSent") to the access log, which is also what increments
# a trigger-count share's counter server-side.
ACCESS_LOG_RESULT_SUCCESS = 2


def normalize_msisdn(number: str) -> str:
    """Normalize a phone number to the ``+<country><national>`` form the backend
    expects (matching what the official app sends).

    The backend matches the remote-user number exactly, so formatting matters:
    the app always sends full international form (e.g. ``+27821234567``). This
    strips spaces/separators and converts a leading international ``00`` prefix
    to ``+``. A number typed in national format (a single leading ``0`` with no
    country code) can't be resolved to E.164 here and is returned digits-only,
    which the config flow rejects so the user is prompted for the ``+`` form.
    """
    s = re.sub(r"[^\d+]", "", (number or "").strip())
    if s.startswith("+"):
        return "+" + re.sub(r"\D", "", s[1:])
    if s.startswith("00"):
        return "+" + s[2:]
    return s


def _remove_national_trunk_prefix(number: str, cc: str) -> str:
    """Strip a national trunk prefix, mirroring the app's per-country rules."""
    if number.startswith(cc) or len(number) < 3:
        return number
    if cc == "+52":
        return number[2:] if number.startswith("01") else number
    if cc == "+976":
        return number[2:] if number.startswith(("01", "02")) else number
    if cc == "+36":
        return number[2:] if number.startswith("06") else number
    if cc in ("+39", "+378", "+379", "+225"):
        return number
    return number[1:] if number.startswith("0") else number


def to_international_number(number: str, dial_code: int) -> str:
    """Combine a national number and a country dialing code into E.164 form.

    Mirrors the official app: the national number is trimmed to digits (a
    leading ``+`` block is kept as-is if it already carries the country code),
    the national trunk prefix is removed, and the ``+<dial_code>`` is prepended.
    """
    trimmed = re.sub(r"[^0-9+]", "", (number or "").strip())
    cc = f"+{dial_code}"
    if trimmed.startswith(cc):
        return trimmed
    return cc + _remove_national_trunk_prefix(trimmed, cc)


# How long a fetched MQTT client certificate is reused before being refetched.
# Well inside the certificate's own validity; a broker rejection clears it early.
CERT_CACHE_SECONDS = 6 * 3600


def _log_url(url: str | URL) -> str:
    """Drop the query string; it can carry tokens and phone numbers."""
    return str(url).split("?", 1)[0]


class CentsysRemoteClient:
    """Thin async wrapper around the two gate backends."""

    def __init__(
        self,
        mobile_number: str,
        *,
        session: aiohttp.ClientSession,
        device_info: DeviceInfo | None = None,
        session_token: str | None = None,
    ) -> None:
        """
        :param mobile_number: E.164 number, e.g. "+27821234567".
        :param session: an aiohttp ClientSession (caller owns its lifecycle).
        :param device_info: client identity sent to the backend.
        :param session_token: an existing long-lived JWT to reuse (skips OTP login).
        """
        self.mobile_number = normalize_msisdn(mobile_number)
        self._session = session
        self.device_info = device_info or DeviceInfo()
        self._session_token = session_token

        # GWeb session token ("<hex>|<base64>"), derived after login.
        self._gweb_token: str | None = None

        # Cached MQTT client certificate, as PEM (see _mqtt_session).
        self._cert_pem: bytes | None = None
        self._key_pem: bytes | None = None
        self._cert_expires = 0.0

    # -- properties --------------------------------------------------------

    @property
    def session_token(self) -> str | None:
        """The Centsys JWT used to authenticate API calls."""
        return self._session_token

    @property
    def gweb_token(self) -> str | None:
        return self._gweb_token

    # -- low-level request helper -----------------------------------------

    async def _request(
        self,
        method: str,
        url: str | URL,
        *,
        op: str,
        bearer: str | None = None,
        json_body: Any = None,
        data: Any = None,
        content_type: str | None = None,
        accept: str = "application/json",
        expected_status: tuple[int, ...] = (200,),
    ) -> tuple[int, str]:
        """Perform a request and return (status, text).

        Raises CentsysApiError on an unexpected status (with status/body/headers)
        and CentsysError on a transport/TLS failure.

        Requests and responses are logged as ``op`` plus a size only: bodies,
        headers and query strings here can all carry credentials.

        :param op: human-readable operation name, used in logs and errors.
        """
        headers = {
            "User-Agent": const.USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        }
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if content_type:
            headers["Content-Type"] = content_type

        _LOGGER.debug("[%s] -> %s %s", op, method, _log_url(url))
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                data=data,
            ) as resp:
                text = await resp.text()
                resp_headers = {k: v for k, v in resp.headers.items()}
                status = resp.status
        except aiohttp.ClientError as err:
            _LOGGER.warning("[%s] transport error: %r", op, err)
            raise CentsysError(f"{op}: transport error: {err!r}") from err
        except Exception as err:  # noqa: BLE001 - surface anything (e.g. TLS) clearly
            _LOGGER.warning("[%s] unexpected error: %r", op, err)
            raise CentsysError(f"{op}: {err!r}") from err

        _LOGGER.debug("[%s] <- HTTP %s, %d bytes", op, status, len(text))

        if status not in expected_status:
            _LOGGER.warning("[%s] failed: HTTP %s, %d bytes", op, status, len(text))
            if status in (401, 403):
                # Distinct from a generic API error so the coordinator can start
                # the reauth flow rather than just marking entities unavailable.
                raise CentsysAuthError(f"{op}: rejected (HTTP {status})")
            raise CentsysApiError(
                f"{op} failed", status=status, body=text, headers=resp_headers
            )
        return status, text

    @staticmethod
    def _parse_json(text: str) -> Any:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text

    def _require_token(self) -> str:
        if not self._session_token:
            raise CentsysAuthError("No session token; call login_with_otp() first.")
        return self._session_token

    def _otp_bearer(self) -> str:
        """Bearer to use for SendOtp/ValidateOtp.

        The backend rejects unauthenticated SendOtp with 401, so a service-level
        bearer is presented before any user token exists. This is what makes a
        from-scratch OTP login work; an existing session is preferred when
        re-authenticating.
        """
        return self._session_token or const.GATEWAY_API_SERVICE_LEVEL_JWT

    # -- authentication ----------------------------------------------------

    async def send_otp(
        self,
        *,
        otp_platform: int = const.OTP_PLATFORM,
        language: str = const.DEFAULT_LANGUAGE,
    ) -> bool:
        """Request an OTP for the configured mobile number.

        :param otp_platform: the ``OtpPlatform`` field (default 1).
        :param language: ISO 639-2/T 3-letter language code.

        Returns True if the backend reports the OTP was sent.
        """
        url = const.CENTSYS_BASE + const.EP_SEND_OTP
        body = {
            "MobileNumber": self.mobile_number,
            "OtpPlatform": otp_platform,
            "ThreeLetterIsoLanguageName": language,
        }
        _, text = await self._request(
            "POST",
            url,
            op="SendOtp",
            bearer=self._otp_bearer(),
            json_body=body,
            content_type="application/json",
        )
        sent = self._parse_json(text) is True
        _LOGGER.debug("[SendOtp] sent=%s (platform %s)", sent, otp_platform)
        return sent

    async def validate_otp(self, otp: str) -> str:
        """Validate an OTP code and store the returned session JWT.

        Returns the session token. Raises OtpInvalidError if the code is wrong
        (the backend signals this with an empty `response` string).
        """
        url = const.CENTSYS_BASE + const.EP_VALIDATE_OTP
        body = {"MobileNumber": self.mobile_number, "Otp": otp}
        _, text = await self._request(
            "POST",
            url,
            op="ValidateOtp",
            bearer=self._otp_bearer(),
            json_body=body,
            content_type="application/json",
        )

        data = self._parse_json(text)
        token = data.get("response") if isinstance(data, dict) else None
        if not token:
            raise OtpInvalidError("OTP rejected (empty response token).")
        _LOGGER.debug("[ValidateOtp] accepted")
        self._session_token = token
        return token

    async def login_with_otp(
        self, otp: str, *, send: bool = False, otp_platform: int = const.OTP_PLATFORM
    ) -> str:
        """Convenience: optionally send, then validate an OTP. Returns the token."""
        if send:
            await self.send_otp(otp_platform=otp_platform)
        return await self.validate_otp(otp)

    # -- GWeb token derivation --------------------------------------------

    async def fetch_gweb_token(self) -> str:
        """Derive and cache the legacy GWeb session token.

        Two-step, exactly as the app does it:
          1. POST /GetGwebToken on Centsys -> returns an encrypted blob.
          2. POST /api/MCROTPNumb on GWeb (Bearer = base64 of a composed string
             that embeds the blob) -> returns the GWeb session token.
        """
        token = self._require_token()

        # Step 1: encrypted blob from Centsys.
        gweb_url = (
            f"{const.CENTSYS_BASE}{const.EP_GET_GWEB_TOKEN}"
            f"?mobileNumber={quote(self.mobile_number)}"
        )
        _, text = await self._request(
            "POST",
            gweb_url,
            op="GetGwebToken",
            bearer=token,
            content_type="application/json; charset=utf-8",
        )
        blob = self._parse_json(text)
        if not isinstance(blob, str) or not blob:
            raise CentsysApiError(
                f"GetGwebToken returned unexpected body ({len(text)} bytes)"
            )

        # Step 2: exchange the blob for a GWeb session token.
        # Auth header = base64("<APP_NAME>|<number>|<device_string>:<blob>")
        composed = (
            f"{const.APP_NAME}|{self.mobile_number}|"
            f"{self.device_info.device_string}:{blob}"
        )
        gweb_bearer = base64.b64encode(composed.encode()).decode()

        otp_numb_url = const.GWEB_BASE + const.EP_GWEB_OTP_NUMB
        form = f"Part1={quote(const.APP_NAME)}&Part2={const.APP_VERSION}"
        _, text = await self._request(
            "POST",
            otp_numb_url,
            op="MCROTPNumb",
            bearer=gweb_bearer,
            data=form,
            content_type="application/x-www-form-urlencoded",
            accept="*/*",
        )
        encoded = self._parse_json(text)
        if not isinstance(encoded, str) or not encoded:
            raise CentsysApiError(
                f"MCROTPNumb returned unexpected body ({len(text)} bytes)"
            )
        # The response is base64; the real token ("<hex>|<base64>") is its decode.
        try:
            gweb_token = base64.b64decode(encoded.strip('"')).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as err:
            raise CentsysApiError(
                f"MCROTPNumb token could not be decoded: {err}"
            ) from err
        self._gweb_token = gweb_token
        return gweb_token

    async def _require_gweb_token(self) -> str:
        """Return the GWeb session token, deriving it on first use."""
        return self._gweb_token or await self.fetch_gweb_token()

    # -- discovery ---------------------------------------------------------

    async def get_devices(self) -> list[Device]:
        """List the gate operators linked to this number (Centsys)."""
        token = self._require_token()
        url = (
            f"{const.CENTSYS_BASE}{const.EP_GET_DEVICES}"
            f"?remoteUserNumber={quote(self.mobile_number)}"
        )
        _, text = await self._request(
            "POST",
            url,
            op="GetDevices",
            bearer=token,
            content_type="application/json; charset=utf-8",
        )
        data = self._parse_json(text)
        if not isinstance(data, list):
            _LOGGER.debug("[GetDevices] no operator list returned")
            return []
        _LOGGER.debug("[GetDevices] %d operator(s)", len(data))
        return [Device.from_json(d) for d in data]

    async def get_operator_overview(self, serial_numbers: list[str]) -> list[OperatorStatus]:
        """Fetch live status for one or more operator serial numbers (Centsys)."""
        token = self._require_token()
        url = const.CENTSYS_BASE + const.EP_GET_OPERATOR_OVERVIEW
        body = {"OperatorSerialNumbers": serial_numbers}
        _, text = await self._request(
            "POST",
            url,
            op="GetOperatorOverview",
            bearer=token,
            json_body=body,
            content_type="application/json",
        )
        data = self._parse_json(text)
        if not isinstance(data, list):
            return []
        return [OperatorStatus.from_json(d) for d in data]

    async def get_buttons(self) -> Any:
        """Fetch the configured remote buttons from GWeb (MCRConfEnV3).

        Body is a pipe-joined string of base64 fields:
            base64("-1") | base64(gweb_token) | base64(onesignal_id) | base64("production")
        Returns the parsed response: a ``Root`` config object (dict with
        ``DeviceConfigs``) for accounts with devices, or a message string such
        as "No Buttons for this number".
        """
        token = await self._require_gweb_token()

        parts = [
            base64.b64encode(b"-1").decode(),
            base64.b64encode(token.encode()).decode(),
            base64.b64encode(self.device_info.onesignal_player_id.encode()).decode(),
            base64.b64encode(b"production").decode(),
        ]
        body = "|".join(parts)

        url = const.GWEB_BASE + const.EP_GWEB_CONFIG
        _, text = await self._request(
            "POST",
            url,
            op="MCRConfEnV3",
            data=body,
            content_type="text/plain; charset=utf-8",
            accept="*/*",
        )
        # The body is double-encoded: a JSON string whose content is itself the
        # JSON config object. _parse_json unwraps one level (-> a string); if
        # that string is itself JSON, decode it again to get the config dict.
        # A plain message like "No Buttons for this number" stays a string.
        parsed = self._parse_json(text)
        if isinstance(parsed, str):
            inner = parsed.strip()
            if inner[:1] in ("{", "["):
                try:
                    return json.loads(inner)
                except (json.JSONDecodeError, ValueError):
                    return parsed
        return parsed

    async def get_gsm_config(self) -> list[GsmDevice]:
        """List legacy GSM/ULTRA operators from the GWeb config (MCRConfEnV3).

        Returns an empty list for Wi-Fi-only accounts (the gateway replies
        "No Buttons for this number", a plain string rather than a config
        object).
        """
        raw = await self.get_buttons()
        if not isinstance(raw, dict):
            return []
        configs = raw.get("DeviceConfigs") or raw.get("deviceConfigs") or []
        if not isinstance(configs, list):
            return []
        return [GsmDevice.from_json(c) for c in configs if isinstance(c, dict)]

    async def trigger_gsm_activation(self, device_id: int | str, io_number: int | str) -> str:
        """Trigger a button on a legacy GSM/ULTRA device via the GWeb gateway.

        This is the legacy equivalent of :meth:`open_gate`, for operators that
        reach the cloud through a GSM/ULTRA module rather than SMART Wi-Fi.

        ``device_id`` and ``io_number`` come from the device's configuration
        (see :meth:`get_buttons` -> operators and their activations). Returns
        the gateway's status message on success ("Activation Queued
        Successfully") and raises on a known failure state.
        """
        token = await self._require_gweb_token()
        return await self._mcract_en(device_id, token, io_number)

    async def _mcract_en(
        self, device_id: int | str, identity: str, io_number: int | str
    ) -> str:
        """Fire the GWeb ``MCRActEn`` cellular trigger and decode its status.

        ``data`` is ``base64(deviceId) | base64(identity) | base64(ioNumber)``.
        ``identity`` is the caller's gweb token for an owned device, or the
        account's number for a shared device (matching how the app addresses
        each). Returns the gateway's success message or raises on a known
        failure state.
        """
        # encoded=True: send the base64 exactly as the app does, without letting
        # the HTTP layer percent-encode the '+', '/' and '=' characters.
        parts = "|".join(
            base64.b64encode(str(v).encode()).decode()
            for v in (device_id, identity, io_number)
        )
        url = URL(
            f"{const.GWEB_BASE}{const.EP_GWEB_ACTIVATE}?data={parts}",
            encoded=True,
        )
        _, text = await self._request("GET", url, op="MCRActEn", accept="*/*")
        # Response is a JSON-ish quoted string; unwrap escapes and quotes.
        result = text.replace("\\", "").strip().strip('"')

        if result == "Activation Queued Successfully":
            return result
        if result == "Device is Offline":
            raise CentsysError("GSM device is offline")
        if result == "Number is time barred":
            raise CentsysError("Number is time barred (too many requests)")
        if result == "Config Required":
            raise CentsysError("Config required; refresh the device configuration")
        raise CentsysApiError(f"Activation failed: {result!r}", status=200, body=text)

    async def get_gsm_io_states(self, device_id: int | str) -> GsmStatus | None:
        """Fetch the live IO states for a legacy GSM/ULTRA device (AppIOStatesEN).

        Lightweight status poll for the gate's live position. Returns a
        :class:`GsmStatus`, or ``None`` if the device is offline.
        """
        data = base64.b64encode(str(device_id).encode()).decode()
        # encoded=True: send the base64 exactly (its '+', '/', '=' unescaped).
        url = URL(
            f"{const.GWEB_BASE}{const.EP_GWEB_IO_STATES}?data={data}",
            encoded=True,
        )
        _, text = await self._request(
            "GET",
            url,
            op="AppIOStatesEN",
            accept="*/*",
        )
        # Response is a JSON-ish quoted string; unwrap escapes and quotes.
        cleaned = text.replace("\\", "").strip().strip('"')
        if not cleaned or cleaned == "Device is Offline":
            return GsmStatus(
                device_id=int(device_id) if str(device_id).isdigit() else 0,
                online=False,
            )
        try:
            root = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(root, dict):
            return None
        return GsmStatus.from_root(device_id, root)

    async def get_gsm_status(self, device_id: int | str) -> GsmDeviceStatus | None:
        """Fetch cached diagnostics for a GSM/ULTRA device (MCRStatus): voltage,
        signal, firmware, antenna, connection, airtime tokens, etc.

        Reads last-known values only. Returns ``None`` on an unparseable body.
        """
        token = await self._require_gweb_token()

        parts = "|".join(
            base64.b64encode(str(v).encode()).decode()
            for v in (device_id, token, "1")
        )
        url = URL(f"{const.GWEB_BASE}{const.EP_GWEB_STATUS}?data={parts}", encoded=True)
        _, text = await self._request("GET", url, op="MCRStatus", accept="*/*")
        cleaned = text.replace("\\", "").strip().strip('"')
        if not cleaned or cleaned == "Device is Offline":
            did = int(device_id) if str(device_id).isdigit() else 0
            return GsmDeviceStatus(device_id=did, online=False)
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return GsmDeviceStatus.from_json(device_id, data)

    async def request_gsm_airtime(self, device_id: int | str) -> str:
        """Queue a network-balance (airtime) refresh for a GSM/ULTRA device.

        The operator queries its balance over the cellular network (a billable
        action), so this is on-demand only; the result is read back later via
        :meth:`get_gsm_status`. Returns the gateway's status message.
        """
        token = await self._require_gweb_token()
        parts = "|".join(
            base64.b64encode(str(v).encode()).decode()
            for v in (device_id, token, "3")
        )
        url = URL(f"{const.GWEB_BASE}{const.EP_GWEB_STATUS}?data={parts}", encoded=True)
        _, text = await self._request("GET", url, op="MCRStatus(airtime)", accept="*/*")
        result = text.replace("\\", "").strip().strip('"')

        if result == "Status Queued Successfully":
            return result
        if result == "Device is Offline":
            raise CentsysError("GSM device is offline")
        if result == "Config Required":
            raise CentsysError("Config required; refresh the device configuration")
        raise CentsysApiError(f"Airtime request failed: {result!r}", status=200, body=text)

    async def get_backup(self) -> Any:
        """Fetch the user's latest app backup from the GWeb ``RemotesAppBackup``
        store (``smart.gweb.co.za``).

        This is the same store the app restores from to populate its device
        list, so it can surface operators that are not returned by
        ``GetDevicesByRemoteUserNumber`` (e.g. gates only ever added locally).

        Returns the parsed response object. The operator list lives inside the
        ``SerializedVersionedBackup`` field as a nested JSON string.
        """
        url = const.GWEB_SMART_BASE + const.EP_GWEB_BACKUP
        status, text = await self._request(
            "POST",
            url,
            op="GetLatestRemotesAppBackup",
            json_body={"UserNumber": self.mobile_number},
            content_type="application/json",
            # 404 = "No backups found for user"; a normal empty result, not a fault.
            expected_status=(200, 404),
        )
        if status == 404:
            return None
        return self._parse_json(text)

    async def get_shared_accesses(self) -> Any:
        """Fetch community / shared-access "sites" granted to this number.

        This is the ``AccessSharing`` backend the official app uses for gates a
        user does not own but has been granted access to -- e.g. a residential
        estate's shared "site" exposing actions such as *Main Gate* and
        *Pedestrian Gate*. These operators are returned by neither
        ``GetDevicesByRemoteUserNumber`` (Wi-Fi remote users) nor
        ``MCRConfEnV3`` (GSM/ULTRA buttons), so a shared-access-only number
        looks empty to both and the integration reports "no gates linked".

        The request body carries a single ``Number`` field (the account's E.164
        number) -- confirmed from the backend's own validation error -- and the
        session bearer is presented in case the service requires it.

        A number with no shared access is a normal, common case, not a fault:
        the service answers it with ``404`` or a ``400`` (the number simply is
        not enrolled in AccessSharing). Both are treated as "no shared access"
        and return ``None`` quietly, so an ordinary account without a shared
        gate does not log a warning on every reload. Only a ``200`` carries
        data. Returns the parsed response, or ``None`` when there is none.
        """
        token = self._require_token()
        url = const.GWEB_ACCESS_BASE + const.EP_GWEB_ACCESS_SHARING
        status, text = await self._request(
            "POST",
            url,
            op="GetAccessesByUserNumber",
            bearer=token,
            json_body={"Number": self.mobile_number},
            content_type="application/json",
            # 400/404 = no shared access for this number; a normal empty result,
            # kept out of the warning path (see docstring).
            expected_status=(200, 400, 404),
        )
        if status != 200:
            _LOGGER.debug(
                "[GetAccessesByUserNumber] no shared access (HTTP %s, %d bytes)",
                status,
                len(text),
            )
            return None
        return self._parse_json(text)

    async def trigger_shared_action(
        self, access: "SharedAccess", action: "SharedAction"
    ) -> None:
        """Trigger one action on a shared-access gate.

        Only GSM/ULTRA shared gates can be triggered remotely: the cellular
        gateway relays the activation (``MCRActEn``). A shared SMART (Wi-Fi)
        gate has no remote path -- it is opened locally over Bluetooth -- so we
        refuse rather than fire a no-op that only notifies the owner.

        The GSM flow has three steps: ``SendActivation`` (authorise, enforce
        revoked/expired/count, notify the owner), ``MCRActEn`` (the actual
        trigger, addressed by the account number), then ``ActivationDeviceResult``
        (log the open and advance a trigger-count share). Raises
        :class:`CentsysError` on a SMART gate or if the backend rejects it.
        """
        if not access.is_ultra:
            raise CentsysError(
                "A shared SMART/Wi-Fi gate can't be triggered remotely; the "
                "app opens it over Bluetooth while at the gate."
            )
        token = self._require_token()
        io_number = action.io_number
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # 1. Authorise via AccessSharing (notifies the owner, enforces limits).
        body = {
            "PreviousTrigger": None,
            "AccessGuid": access.guid,
            "Timestamp": timestamp,
            "RequestingNumber": self.mobile_number,
            "PhoneMacAddress": None,
            "SharedAccessLastModifiedDateUtc": access.last_modified_utc,
            "UserSharedAccessLastModifiedDateUtc": access.user_last_modified_utc(
                self.mobile_number
            ),
            "TriggerId": io_number,
            "TriggerName": action.name,
        }
        url = const.GWEB_ACCESS_BASE + const.EP_GWEB_ACTIVATION_SEND
        _, text = await self._request(
            "POST",
            url,
            op="SendActivation",
            bearer=token,
            json_body=body,
            content_type="application/json",
            # The service replies 200 (and occasionally 202) with a JSON body.
            expected_status=(200, 202),
        )
        data = self._parse_json(text)
        code = data.get("response") if isinstance(data, dict) else None
        if code != ACTION_RESPONSE_SUCCESS:
            raise CentsysError(
                f"Shared gate rejected the trigger: "
                f"{ACTION_RESPONSE_LABELS.get(code, code)}"
            )

        # 2. Fire the actual cellular trigger (addressed by number, as the app).
        await self._mcract_en(access.ultra_device_id, self.mobile_number, io_number)

        # 3. Record the open (access log + trigger-count advance).
        await self._report_shared_result(access, action, io_number, timestamp, token)

    async def _report_shared_result(
        self,
        access: "SharedAccess",
        action: "SharedAction",
        trigger_id: int,
        timestamp: str,
        token: str,
    ) -> None:
        """Report a successful shared trigger to the access log (best-effort).

        Mirrors the app's ``ActivationDeviceResult`` call: it logs the open for
        the owner and increments a trigger-count share's server-side counter.
        The gate has already been triggered by this point, so a failure here is
        logged but never surfaced to the caller.
        """
        body = {
            "AccessGuid": access.guid,
            "ActivationTimestampUtc": timestamp,
            "RequestingNumber": self.mobile_number,
            "TriggerId": trigger_id,
            "TriggerName": action.name,
            "Result": ACCESS_LOG_RESULT_SUCCESS,
        }
        url = const.GWEB_ACCESS_BASE + const.EP_GWEB_ACTIVATION_RESULT
        try:
            await self._request(
                "POST",
                url,
                op="ActivationDeviceResult",
                bearer=token,
                json_body=body,
                content_type="application/json",
                expected_status=(200, 202),
            )
        except CentsysError as err:
            _LOGGER.debug("ActivationDeviceResult report failed: %s", err)

    # -- MQTT client certificate ------------------------------------------

    async def get_certificate(self) -> dict[str, str]:
        """Fetch the MQTT client certificate (mutual TLS) for the broker.

        ``POST /GetCertificate`` (Bearer-authenticated). Returns a dict with
        ``pfx_base64`` (a PKCS#12 blob, base64) and ``password`` -- the
        credential needed to connect to the MQTT broker where gate status and
        the open command flow.
        """
        token = self._require_token()
        url = const.CENTSYS_BASE + const.EP_GET_CERTIFICATE
        _, text = await self._request(
            "POST",
            url,
            op="GetCertificate",
            bearer=token,
            json_body={},
            content_type="application/json",
        )
        data = self._parse_json(text)
        if not isinstance(data, dict):
            raise CentsysApiError(
                f"GetCertificate returned unexpected body ({len(text)} bytes)"
            )

        # Be tolerant of camelCase / PascalCase key variants.
        def _pick(*names: str) -> str | None:
            for n in names:
                for key in data:
                    if key.lower() == n.lower():
                        return data[key]
            return None

        pfx = _pick("certificatePfxBase64", "CertificatePfxBase64", "pfxBase64")
        password = _pick("certificatePassword", "CertificatePassword", "password")
        if not pfx:
            raise CentsysApiError(
                f"GetCertificate: no pfx in body (keys: {sorted(data)})"
            )
        return {"pfx_base64": pfx, "password": password or ""}

    async def _mqtt_session(self, *, au: bool) -> tuple[bytes, bytes, str, str]:
        """Return ``(cert_pem, key_pem, client_id, host)`` for a broker session.

        The certificate is fetched once and reused: without this a single gate
        press costs two ``GetCertificate`` round trips (the trigger and the live
        status follow), plus a PKCS#12 parse each time. ``invalidate_certificate``
        clears it when the broker rejects the credential.
        """
        import asyncio

        from . import mqtt_remote

        if self._cert_pem is None or time.monotonic() >= self._cert_expires:
            cert = await self.get_certificate()
            self._cert_pem, self._key_pem = await asyncio.get_running_loop().run_in_executor(
                None, mqtt_remote.pfx_to_pem, cert["pfx_base64"], cert["password"]
            )
            self._cert_expires = time.monotonic() + CERT_CACHE_SECONDS

        return (
            self._cert_pem,
            self._key_pem,
            f"{const.MQTT_CLIENT_ID_PREFIX}{self.mobile_number}",
            const.MQTT_IP_AU if au else const.MQTT_IP_ZA,
        )

    def invalidate_certificate(self) -> None:
        """Drop the cached certificate so the next session fetches a fresh one."""
        self._cert_pem = self._key_pem = None
        self._cert_expires = 0.0

    async def _run_mqtt(self, fn):
        """Run a blocking MQTT call in a thread, refreshing a rejected cert."""
        import asyncio

        try:
            return await asyncio.get_running_loop().run_in_executor(None, fn)
        except CentsysCertExpiredError:
            self.invalidate_certificate()
            raise

    # -- gate control ------------------------------------------------------

    async def send_activation(
        self,
        serial: str,
        *,
        mac: str | bytes,
        activation_id: int,
        au: bool = False,
        timeout: float = 8.0,
    ) -> bool:
        """Run the MQTT activation handshake for one operator.

        The single path for every operator action (open, pedestrian, Holiday
        Lock, Keep Open ...). Fetches the per-session client certificate,
        connects to the broker as MQTT v5 with clientId ``mcr:<number>`` and runs
        the challenge-response handshake (see ``mqtt_remote``). Returns True if
        the operator acknowledged.

        ``serial`` must be the LONG operator serial (the MQTT topic prefix).
        ``mac`` is the operator's ``macAddress`` from the device listing, used to
        build the per-operator packets. ``activation_id`` selects the action (see
        ``packets``); the caller is responsible for choosing one valid for the
        operator's family.

        Runs the blocking MQTT handshake in a thread so it is safe to await.
        """
        from . import mqtt_remote, packets

        mac4 = packets.parse_mac(mac)
        cmd01 = packets.build_cmd01(self.mobile_number, mac4)
        cmd05 = packets.build_cmd05(mac4)

        cert_pem, key_pem, client_id, host = await self._mqtt_session(au=au)

        return await self._run_mqtt(
            lambda: mqtt_remote.open_gate_blocking(
                host=host,
                port=const.MQTT_PORT,
                client_id=client_id,
                serial=serial,
                cert_pem=cert_pem,
                key_pem=key_pem,
                cmd01=cmd01,
                cmd05=cmd05,
                build_cmd03=lambda cv: packets.build_cmd03_prefix(
                    mac4, cv, activation_id=activation_id
                ),
                decode_cmd04=lambda p: packets.decode_cmd04(mac4, p),
                timeout=timeout,
            ),
        )

    async def open_gate(
        self,
        serial: str,
        *,
        mac: str | bytes,
        is_garage: bool = False,
        activation_id: int | None = None,
        au: bool = False,
        timeout: float = 8.0,
    ) -> bool:
        """Trigger (open) the gate over MQTT. Returns True if acknowledged.

        ``is_garage`` selects the default trigger activation (a telemetry- or
        product-code-confirmed garage-door operator uses RUN, everything else
        TRG). Pass ``activation_id`` to override (e.g. PED). Thin wrapper around
        :meth:`send_activation`.
        """
        from . import packets

        if activation_id is None:
            activation_id = packets.trigger_activation_id(is_garage=is_garage)
        return await self.send_activation(
            serial, mac=mac, activation_id=activation_id, au=au, timeout=timeout
        )

    def _wake_packet(self, mac: str | bytes | None) -> bytes:
        """Build the cmd 01 identity packet used to wake telemetry (no actuation).

        Returns b"" when no MAC is known, so the caller listens passively rather
        than sending an identity the gate would reject.
        """
        if not mac:
            return b""
        from . import packets

        return packets.build_cmd01(self.mobile_number, packets.parse_mac(mac))

    async def get_overview(
        self,
        serial: str,
        *,
        mac: str | bytes | None = None,
        au: bool = False,
        timeout: float = 15.0,
    ):
        """Fetch live telemetry from the gate over MQTT.

        Returns a ``mqtt_remote.DeviceOverview`` (battery voltage, gate status,
        power status, beams, temperature, ...) decoded from the gate's
        ``deviceOverview`` push, or None if the device didn't respond.

        A battery-backed operator keeps its Wi-Fi telemetry asleep, so this
        sends a cmd 01 identity packet (built from ``mac``) to wake it -- this
        does NOT open the gate (see ``mqtt_remote.fetch_overview_blocking``).
        With no ``mac`` it listens passively.

        ``serial`` must be the LONG operator serial (the MQTT topic prefix).
        Runs the blocking MQTT exchange in a thread so it is safe to await.
        """
        from . import mqtt_remote

        wake_cmd01 = self._wake_packet(mac)
        cert_pem, key_pem, client_id, host = await self._mqtt_session(au=au)

        return await self._run_mqtt(
            lambda: mqtt_remote.fetch_overview_blocking(
                host=host,
                port=const.MQTT_PORT,
                client_id=client_id,
                serial=serial,
                cert_pem=cert_pem,
                key_pem=key_pem,
                wake_cmd01=wake_cmd01,
                timeout=timeout,
            ),
        )

    async def follow_overview(
        self,
        serial: str,
        *,
        callback,
        duration: float,
        mac: str | bytes | None = None,
        au: bool = False,
    ) -> None:
        """Stream live telemetry for ``duration`` s, calling ``callback(ov)``.

        Used to follow an open/close cycle in real time. ``callback`` is invoked
        from a worker thread for each ``mqtt_remote.DeviceOverview``; marshal it
        onto your event loop. Best-effort -- failures are swallowed.

        ``serial`` must be the LONG operator serial (the MQTT topic prefix).
        ``mac`` builds the telemetry wake packet (see :meth:`get_overview`).
        """
        from . import mqtt_remote

        wake_cmd01 = self._wake_packet(mac)
        cert_pem, key_pem, client_id, host = await self._mqtt_session(au=au)

        await self._run_mqtt(
            lambda: mqtt_remote.follow_overview_blocking(
                host=host,
                port=const.MQTT_PORT,
                client_id=client_id,
                serial=serial,
                cert_pem=cert_pem,
                key_pem=key_pem,
                on_overview=callback,
                duration=duration,
                wake_cmd01=wake_cmd01,
            ),
        )
