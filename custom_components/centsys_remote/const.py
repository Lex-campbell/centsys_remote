"""Constants for the CenSys Gate Remote integration."""

from __future__ import annotations

DOMAIN = "centsys_remote"

CONF_MOBILE_NUMBER = "mobile_number"
CONF_COUNTRY = "country"
CONF_TOKEN = "token"
CONF_NAME = "name"
CONF_EMAIL = "email"

# OTP delivery channel (matches the app's OtpPlatformEnum).
OTP_PLATFORM_WHATSAPP = 1
OTP_PLATFORM_SMS = 2
DEFAULT_OTP_PLATFORM = OTP_PLATFORM_WHATSAPP

CONF_OTP_PLATFORM = "otp_platform"

# Cloud polling cadence (seconds) for device list + operator status.
DEFAULT_SCAN_INTERVAL = 60

# How long a live gate-status frame (pushed by a cover's follow after a press)
# stays authoritative before entities fall back to the slower cloud poll.
LIVE_STATUS_TTL = 20

# How long to follow the MQTT status stream after a TRG/PED press (covers the
# open + auto-close cycle).
LIVE_FOLLOW_SECONDS = 75.0

# MQTT telemetry (battery voltage etc.) is far heavier than the HTTP poll: it
# opens a TLS session and wakes the operator's Wi-Fi radio, so we refresh it on
# a much slower cadence than the cloud status.
TELEMETRY_SCAN_INTERVAL = 900

# Legacy GWeb (GSM/ULTRA) device config changes rarely; refresh it on a slower
# cadence than the main cloud status to avoid extra round-trips every poll.
GSM_SCAN_INTERVAL = 300

# After an on-demand airtime request, the operator queries its balance over the
# cellular network and syncs back asynchronously, so poll the cached status a
# few times to pick up the result.
AIRTIME_POLL_INTERVAL = 8
AIRTIME_POLL_ATTEMPTS = 12

PLATFORMS = ["binary_sensor", "button", "cover", "sensor", "switch"]

MANUFACTURER = "Centurion Systems (CenSys)"
