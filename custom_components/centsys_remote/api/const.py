"""Endpoints and constants for the Centsys cloud service."""

# --- Backends -------------------------------------------------------------

# Centsys API (JWT/HS256 bearer auth). Regional endpoints; ZA is the default.
GATEWAY_URL_ZA = "https://centsys.southafricanorth.cloudapp.azure.com:4445"
GATEWAY_URL_AU = "https://centsys.australiaeast.cloudapp.azure.com:4445"
CENTSYS_BASE = GATEWAY_URL_ZA

# MQTT brokers. SMART Wi-Fi operators are controlled over MQTT (mutual TLS,
# MQTT v5) rather than HTTP -- this is the open_gate() transport.
MQTT_IP_ZA = "20.87.192.195"
MQTT_IP_AU = "20.213.185.54"
MQTT_PORT = 8880

# The user is identified to the gate by clientId "mcr:<number>", used as both
# the MQTT clientId and a "ClientId" user property on every publish.
MQTT_CLIENT_ID_PREFIX = "mcr:"

# Trigger packets are built per operator from its macAddress (see api/packets.py).

# Service-level bearer used for the initial SendOtp/ValidateOtp calls, before a
# user session token exists. This is what allows a from-scratch OTP login.
GATEWAY_API_SERVICE_LEVEL_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJodHRwOi8vc2NoZW1hcy54bWxzb2FwLm9yZy93cy8yMDA1LzA1L2lkZW50aXR5L2NsYWltcy9z"
    "eXN0ZW0iOiJHYXRld2F5QXBpIiwiaHR0cDovL3NjaGVtYXMueG1sc29hcC5vcmcvd3MvMjAwNS8w"
    "NS9pZGVudGl0eS9jbGFpbXMvaGFzaCI6InJzZmEhJDZWMzJHTlpWS29zJnglIiwiZXhwIjoyNzA0"
    "MDIxMzYxLCJpc3MiOiJHYXRlV2F5QXBpLmNvbSIsImF1ZCI6IkdhdGVXYXlBcGkuY29tIn0."
    "ZhCrEe6WUCkI-uxlcZlyvPQ5FfAe8P3EiVMswFbquLE"
)

# Legacy GWeb backend (custom token scheme).
GWEB_BASE = "https://www.gweb.co.za"
GWEB_ACCESS_BASE = "https://www.gweb.co.za:4446"
GWEB_SMART_BASE = "https://smart.gweb.co.za"

# --- Centsys endpoints ----------------------------------------------------

EP_SEND_OTP = "/SendOtp"
EP_VALIDATE_OTP = "/ValidateOtp"
EP_GET_GWEB_TOKEN = "/GetGwebToken"  # ?mobileNumber=<phone>
EP_ADD_OR_UPDATE_USER = "/AddOrUpdateMyCentsysRemoteUserInformation"
EP_GET_DEVICES = "/GetDevicesByRemoteUserNumber"  # ?remoteUserNumber=<phone>
EP_GET_OPERATOR_OVERVIEW = "/GetOperatorOverview"
EP_GET_ELIGIBLE_TO_RATE = "/GetEligibleUserToRate"  # ?PhoneNumber=<phone>
# Returns the MQTT client certificate (mutual TLS) used to reach the broker.
EP_GET_CERTIFICATE = "/GetCertificate"

# --- GWeb endpoints -------------------------------------------------------

EP_GWEB_OTP_NUMB = "/api/MCROTPNumb"     # exchanges gweb blob -> gweb session token
EP_GWEB_CONFIG = "/api/MCRConfEnV3"      # fetch configured buttons
EP_GWEB_ACTIVATE = "/api/MCRActEn"       # trigger a GSM/legacy button (?data=...)
EP_GWEB_IO_STATES = "/api/AppIOStatesEN"  # live IO states for a GSM device (?data=b64(id))
EP_GWEB_STATUS = "/api/MCRStatus"        # GSM device diagnostics (voltage/signal/airtime)
EP_GWEB_MUTE_STATUS = "/api/MCRMuteStatus"
EP_GWEB_ADD_USER = "/api/AddOrUpdateRemotesUserUpdated/"          # smart.gweb
EP_GWEB_BACKUP_META = "/api/RemotesAppBackup/GetLatestRemotesAppBackupMetaData"  # smart.gweb
EP_GWEB_BACKUP = "/api/RemotesAppBackup/GetLatestRemotesAppBackup"  # smart.gweb
EP_GWEB_ACCESS_SHARING = "/api/AccessSharing/GetAccessesByUserNumber"  # :4446
EP_GWEB_ACTIVATION_SEND = "/api/Activation/SendActivation"  # :4446 (shared-gate trigger)
EP_GWEB_ACTIVATION_RESULT = "/api/Activation/ActivationDeviceResult"  # :4446 (log/count)

# --- Client identity ------------------------------------------------------

APP_NAME = "MyCentsys Remote"
APP_VERSION = "2.1.0.35"
USER_AGENT = "MyCentsysRemoteMaui/2.1.0.35 CFNetwork/3860.600.12 Darwin/25.5.0"

# OtpPlatform=1. Language is an ISO 639-2/T 3-letter code.
OTP_PLATFORM = 1
DEFAULT_LANGUAGE = "eng"
