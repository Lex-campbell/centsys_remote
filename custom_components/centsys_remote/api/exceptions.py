"""Exception hierarchy for the Centsys Remote client."""

from __future__ import annotations


class CentsysError(Exception):
    """Base error for anything raised by this client."""


class CentsysAuthError(CentsysError):
    """Authentication failed or no valid session token is available."""


class CentsysCertExpiredError(CentsysError):
    """The MQTT broker rejected the TLS handshake citing an expired certificate.

    This is a provider-side condition: the broker itself refuses the connection,
    so it affects every client (the official app included), not just this
    integration. There is nothing to fix locally; gate control resumes once
    Centsys resolves it on their end. Note the broker normally tolerates a
    client certificate a little past its notAfter, so this is raised only when
    the broker actively rejects the connection.
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
