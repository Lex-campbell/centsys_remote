"""Constants for the Centsys Gate Remote integration."""

from __future__ import annotations

DOMAIN = "centsys_remote"

CONF_MOBILE_NUMBER = "mobile_number"
CONF_COUNTRY = "country"
CONF_TOKEN = "token"
CONF_NAME = "name"
CONF_EMAIL = "email"
# Per-operator product codes the operator reported over telemetry, kept so a
# gate/garage stays correctly classified across restarts and telemetry gaps.
CONF_PRODUCT_CODES = "product_codes"

# OTP delivery channel (matches the app's OtpPlatformEnum).
OTP_PLATFORM_WHATSAPP = 1
OTP_PLATFORM_SMS = 2
DEFAULT_OTP_PLATFORM = OTP_PLATFORM_WHATSAPP

CONF_OTP_PLATFORM = "otp_platform"

# Datacentre region for this account ("za" or "au"). Stored resolved on the entry;
# absent on entries created before region support, which default to "za".
CONF_REGION = "region"

# Opt-in: keep a persistent MQTT connection open (on a distinct clientId) that
# streams live gate movement in real time -- catching operation from a physical
# remote/app/schedule that the periodic cloud poll can miss. Hardware-confirmed
# collision-free with the phone app (see DECISIONS D15).
CONF_ENABLE_LIVE_LISTENER = "enable_live_listener"

# Cloud polling cadence (seconds) for device list + operator status.
DEFAULT_SCAN_INTERVAL = 60

# How long a live gate-status frame (pushed by a cover's follow after a press)
# stays authoritative; expired positions become unknown, never HTTP fallback.
LIVE_STATUS_TTL = 20

# Release-candidate policy: one read-only idle wake per minute, with 30s
# delivery tolerance. Moving states retain the shorter 20s lifetime above.
# Hardware acceptance must check idle cadence and radio/power impact.
POSITION_IDLE_REFRESH_INTERVAL = 60
STATIONARY_POSITION_TTL = 90

# How long to follow the MQTT status stream after a TRG/PED press (covers the
# open + auto-close cycle).
LIVE_FOLLOW_SECONDS = 75.0

# MQTT telemetry (battery voltage etc.) is far heavier than the HTTP poll: it
# opens a TLS session and wakes the operator's Wi-Fi radio, so we refresh it on
# a much slower cadence than the cloud status.
TELEMETRY_SCAN_INTERVAL = 600

# Floor applied when a user explicitly asks an entity to update
# (``homeassistant.update_entity``). That request bypasses the slow cadence
# above so a change made elsewhere is picked up on demand, but the floor stops a
# tight automation from holding the operator's radio awake.
TELEMETRY_FORCE_MIN_INTERVAL = 30

# Legacy GWeb (GSM/ULTRA) device config changes rarely; refresh it on a slower
# cadence than the main cloud status to avoid extra round-trips every poll.
GSM_SCAN_INTERVAL = 300

# Community / shared-access (AccessSharing) sites change rarely (a share added or
# revoked), so refresh them on the same slower cadence rather than every poll.
SHARED_SCAN_INTERVAL = 300

# Live listener reconnect backoff (seconds): start short, grow on repeated
# immediate failures, and cap. A connection that stayed up a while resets to the
# minimum on its next drop (see live_listener.LiveListener).
LISTENER_RECONNECT_MIN = 5
LISTENER_RECONNECT_MAX = 300

# After an on-demand airtime request, the operator queries its balance over the
# cellular network and syncs back asynchronously, so poll the cached status a
# few times to pick up the result.
AIRTIME_POLL_INTERVAL = 8
AIRTIME_POLL_ATTEMPTS = 12

PLATFORMS = ["binary_sensor", "button", "cover", "sensor", "switch"]

MANUFACTURER = "Centurion Systems (Centsys)"

# Linked from the "no gates linked" repair, which the user resolves in the app.
NO_GATES_HELP_URL = (
    "https://github.com/lex-campbell/centsys_remote#requirements"
)

# Linked from the "shared Wi-Fi gate is read-only" repair (explains the BLE limit).
SHARED_HELP_URL = (
    "https://github.com/lex-campbell/centsys_remote#supported-devices"
)
