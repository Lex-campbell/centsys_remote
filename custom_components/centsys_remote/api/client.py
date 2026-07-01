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
import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from . import const
from .exceptions import CentsysApiError, CentsysAuthError, OtpInvalidError
from .models import Device, DeviceInfo, OperatorStatus

_LOGGER = logging.getLogger(__name__)


def normalize_msisdn(number: str) -> str:
    """Normalize a phone number to the ``+<country><national>`` form the backend
    expects (matching what the official app sends).

    The backend matches the remote-user number exactly, so formatting matters:
    the app always sends full international form (e.g. ``+27832505442``). This
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


# Each token's exp is ~30 years past its creation time.
TOKEN_TTL_SECONDS = 30 * 365 * 24 * 3600


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint_bootstrap_token(
    mobile_number: str, secret: str, *, ttl_seconds: int = TOKEN_TTL_SECONDS
) -> str:
    """Mint an HS256 bearer for SendOtp/ValidateOtp from a known signing secret.

    Token shape:
        header  = {"alg":"HS256","typ":"JWT"}
        payload = {<mobilephone claim>: number, "exp": now+~30y,
                   "iss":"GateWayApi.com", "aud":"GateWayApi.com"}

    Optional: only used when a ``bootstrap_secret`` is supplied to the client.
    The default OTP login does not need this.
    """
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        const.JWT_MOBILE_CLAIM: mobile_number,
        "exp": int(time.time()) + ttl_seconds,
        "iss": const.JWT_ISS,
        "aud": const.JWT_AUD,
    }
    signing_input = (
        f"{_b64url_nopad(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64url_nopad(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_nopad(signature)}"


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Copy headers with the bearer token shortened, for safe logging."""
    redacted = dict(headers)
    auth = redacted.get("Authorization")
    if auth and len(auth) > 24:
        redacted["Authorization"] = f"{auth[:18]}...{auth[-6:]} (len={len(auth)})"
    return redacted


class CentsysRemoteClient:
    """Thin async wrapper around the two gate backends."""

    def __init__(
        self,
        mobile_number: str,
        *,
        session: aiohttp.ClientSession,
        device_info: DeviceInfo | None = None,
        session_token: str | None = None,
        bootstrap_token: str | None = None,
        bootstrap_secret: str | None = None,
        verify_ssl: bool = True,
    ) -> None:
        """
        :param mobile_number: E.164 number, e.g. "+27832505442".
        :param session: an aiohttp ClientSession (caller owns its lifecycle).
        :param device_info: client identity sent to the backend.
        :param session_token: an existing long-lived JWT to reuse (skips OTP login).
        :param bootstrap_token: explicit bearer to use for SendOtp/ValidateOtp.
        :param bootstrap_secret: optional HS256 secret; if set (and no token is
            available) a fresh bearer is minted locally for the OTP calls.
        :param verify_ssl: set False only for debugging behind a proxy.
        """
        self.mobile_number = normalize_msisdn(mobile_number)
        self._session = session
        self.device_info = device_info or DeviceInfo()
        self._session_token = session_token
        self._bootstrap_token = bootstrap_token
        self._bootstrap_secret = bootstrap_secret
        self._verify_ssl = verify_ssl

        # GWeb session token ("<hex>|<base64>"), derived after login.
        self._gweb_token: str | None = None

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
        url: str,
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
        and CentsysError on a transport/TLS failure. Logs full request/response
        detail at DEBUG, and a concise failure line at WARNING.

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

        _LOGGER.debug(
            "[%s] -> %s %s\n  req headers: %s\n  json: %s\n  data: %s",
            op, method, url, _redact_headers(headers), json_body, data,
        )
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                data=data,
                ssl=self._verify_ssl,
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

        _LOGGER.debug(
            "[%s] <- HTTP %s\n  resp headers: %s\n  body: %s",
            op, status, resp_headers, text,
        )

        if status not in expected_status:
            _LOGGER.warning(
                "[%s] failed: HTTP %s | body=%r | resp headers=%s",
                op, status, text, resp_headers,
            )
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

    def _otp_bearer(self) -> str | None:
        """Bearer to use for SendOtp/ValidateOtp.

        The backend rejects unauthenticated SendOtp with 401, so a service-level
        bearer is presented before any user token exists. This is what makes a
        from-scratch OTP login work.

        Priority: explicit bootstrap_token > existing session_token >
        locally-minted token (if a secret was supplied) > built-in service JWT.
        """
        if self._bootstrap_token:
            return self._bootstrap_token
        if self._session_token:
            return self._session_token
        if self._bootstrap_secret:
            return mint_bootstrap_token(self.mobile_number, self._bootstrap_secret)
        return const.GATEWAY_API_SERVICE_LEVEL_JWT

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
        return self._parse_json(text) is True

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
            raise CentsysApiError(f"GetGwebToken returned unexpected body: {text}")

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
        gweb_token = self._parse_json(text)
        if not isinstance(gweb_token, str) or not gweb_token:
            raise CentsysApiError(f"MCROTPNumb returned unexpected body: {text}")
        self._gweb_token = gweb_token
        return gweb_token

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
            return []
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
        Returns the parsed response (a list of buttons, or a message string such
        as "No Buttons for this number").
        """
        if not self._gweb_token:
            await self.fetch_gweb_token()
        assert self._gweb_token is not None

        parts = [
            base64.b64encode(b"-1").decode(),
            base64.b64encode(self._gweb_token.encode()).decode(),
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
        return self._parse_json(text)

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
            raise CentsysApiError(f"GetCertificate returned unexpected body: {text}")

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
            raise CentsysApiError(f"GetCertificate: no pfx in body: {text}")
        return {"pfx_base64": pfx, "password": password or ""}

    # -- gate control ------------------------------------------------------

    async def open_gate(
        self,
        serial: str,
        *,
        au: bool = False,
        cmd01: bytes | None = None,
        cmd05: bytes | None = None,
        cmd03_prefix: bytes | None = None,
        timeout: float = 8.0,
    ) -> bool:
        """Trigger (open) the gate over MQTT.

        Fetches the per-session client certificate, connects to the broker as
        MQTT v5 with clientId ``mcr:<number>`` and runs the challenge-response
        handshake (see ``mqtt_remote``). Returns True if the gate acknowledged.

        ``serial`` must be the LONG operator serial (the MQTT topic prefix). The
        ``cmd01/cmd05/cmd03_prefix`` packet templates default to the built-in
        values; pass per-operator templates to override.

        Runs the blocking MQTT handshake in a thread so it is safe to await.
        """
        import asyncio

        from . import mqtt_remote

        cert = await self.get_certificate()
        cert_pem, key_pem = await asyncio.get_running_loop().run_in_executor(
            None, mqtt_remote.pfx_to_pem, cert["pfx_base64"], cert["password"]
        )

        client_id = f"{const.MQTT_CLIENT_ID_PREFIX}{self.mobile_number}"
        host = const.MQTT_IP_AU if au else const.MQTT_IP_ZA

        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: mqtt_remote.open_gate_blocking(
                host=host,
                port=const.MQTT_PORT,
                client_id=client_id,
                serial=serial,
                cert_pem=cert_pem,
                key_pem=key_pem,
                cmd01=cmd01 or const.MQTT_OPEN_CMD01,
                cmd05=cmd05 or const.MQTT_OPEN_CMD05,
                cmd03_prefix=cmd03_prefix or const.MQTT_OPEN_CMD03_PREFIX,
                timeout=timeout,
            ),
        )

    async def get_overview(
        self,
        serial: str,
        *,
        au: bool = False,
        wake_cmd01: bytes | None = None,
        timeout: float = 15.0,
    ):
        """Fetch live telemetry from the gate over MQTT.

        Returns a ``mqtt_remote.DeviceOverview`` (battery voltage, gate status,
        power status, beams, temperature, ...) decoded from the gate's
        ``deviceOverview`` push, or None if the device didn't respond.

        A battery-backed operator keeps its Wi-Fi telemetry asleep, so this
        sends a cmd 01 identity packet to wake it (does NOT open the gate; see
        ``mqtt_remote.fetch_overview_blocking``). Defaults to the built-in
        identity packet; override per-operator with ``wake_cmd01``.

        ``serial`` must be the LONG operator serial (the MQTT topic prefix).
        Runs the blocking MQTT exchange in a thread so it is safe to await.
        """
        import asyncio

        from . import mqtt_remote

        cert = await self.get_certificate()
        cert_pem, key_pem = await asyncio.get_running_loop().run_in_executor(
            None, mqtt_remote.pfx_to_pem, cert["pfx_base64"], cert["password"]
        )

        client_id = f"{const.MQTT_CLIENT_ID_PREFIX}{self.mobile_number}"
        host = const.MQTT_IP_AU if au else const.MQTT_IP_ZA

        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: mqtt_remote.fetch_overview_blocking(
                host=host,
                port=const.MQTT_PORT,
                client_id=client_id,
                serial=serial,
                cert_pem=cert_pem,
                key_pem=key_pem,
                wake_cmd01=const.MQTT_OPEN_CMD01 if wake_cmd01 is None else wake_cmd01,
                timeout=timeout,
            ),
        )

    async def follow_overview(
        self,
        serial: str,
        *,
        callback,
        duration: float,
        au: bool = False,
        wake_cmd01: bytes | None = None,
    ) -> None:
        """Stream live telemetry for ``duration`` s, calling ``callback(ov)``.

        Used to follow an open/close cycle in real time. ``callback`` is invoked
        from a worker thread for each ``mqtt_remote.DeviceOverview``; marshal it
        onto your event loop. Best-effort -- failures are swallowed.

        ``serial`` must be the LONG operator serial (the MQTT topic prefix).
        """
        import asyncio

        from . import mqtt_remote

        cert = await self.get_certificate()
        cert_pem, key_pem = await asyncio.get_running_loop().run_in_executor(
            None, mqtt_remote.pfx_to_pem, cert["pfx_base64"], cert["password"]
        )

        client_id = f"{const.MQTT_CLIENT_ID_PREFIX}{self.mobile_number}"
        host = const.MQTT_IP_AU if au else const.MQTT_IP_ZA

        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: mqtt_remote.follow_overview_blocking(
                host=host,
                port=const.MQTT_PORT,
                client_id=client_id,
                serial=serial,
                cert_pem=cert_pem,
                key_pem=key_pem,
                on_overview=callback,
                duration=duration,
                wake_cmd01=const.MQTT_OPEN_CMD01 if wake_cmd01 is None else wake_cmd01,
            ),
        )
