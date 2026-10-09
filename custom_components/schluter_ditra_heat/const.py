"""Constants for the Schluter DITRA-HEAT integration."""

from datetime import timedelta

DOMAIN = "schluter_ditra_heat"

BASE_URL = "https://ditra-heat-e-wifi.schluter.com"
API_URL = f"{BASE_URL}/api"

# Platform id the Schluter web app sends to /api/authenticate/user.
APPLICATION_ID = 7

# Changes arrive by long-poll push. Polling is only a safety net, and speeds
# up while the push connection is down.
SAFETY_POLL_INTERVAL = timedelta(minutes=15)
FALLBACK_POLL_INTERVAL = timedelta(seconds=60)

# The server holds /api/notification open for about 60 s.
NOTIFICATION_TIMEOUT = 90
PUSH_RETRY_MIN = 5
PUSH_RETRY_MAX = 300

ACTION_UPDATED = 2
ACTION_REMOVED = 3

# RegulationMode values used by the Schluter web app.
MODE_SCHEDULE = 1
MODE_COMFORT = 2  # temporary hold until ComfortEndTime
MODE_MANUAL = 3  # permanent hold at ManualTemperature
MODE_VACATION = 4

# The web app's "away" puts the thermostat in manual mode at 5 °C; we use it as "off".
OFF_TEMPERATURE = 500
