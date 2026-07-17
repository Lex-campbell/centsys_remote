"""Exception hierarchy for the Centsys Remote client."""

from __future__ import annotations


class CentsysError(Exception):
    """Base error for anything raised by this client."""


class CentsysAuthError(CentsysError):
    """Authentication failed or no valid session token is available."""


class CentsysCertExpiredError(CentsysError):
    """The client certificate issued by the backend is expired or not yet valid.

    This is a provider-side condition: the backend hands out a shared,
    time-limited certificate used for the MQTT connection. When it lapses the
    broker rejects the TLS handshake for every client (the official app
    included) until the provider rotates it. There is nothing to fix locally;
    the integration recovers automatically once a valid certificate is served.
    """


class OtpInvalidError(CentsysAuthError):
    """ValidateOtp returned an empty response, meaning the OTP was rejected."""


class CentsysApiError(CentsysError):
    """An API call returned a non-success HTTP status."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        body: str | None = None,
        headers: dict | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body
        self.headers = headers or {}

    def __str__(self) -> str:
        parts = [self.message]
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.body:
            parts.append(f"body={self.body!r}")
        return " | ".join(parts)
