"""Constants for the Schluter DITRA-HEAT integration."""

from datetime import timedelta

DOMAIN = "schluter_ditra_heat"

BASE_URL = "https://ditra-heat-e-wifi.schluter.com"
API_URL = f"{BASE_URL}/api"

# Platform id the Schluter web app sends to /api/authenticate/user.
APPLICATION_ID = 7

UPDATE_INTERVAL = timedelta(seconds=60)

# RegulationMode values used by the Schluter web app.
MODE_SCHEDULE = 1
MODE_COMFORT = 2  # temporary hold until ComfortEndTime
MODE_MANUAL = 3  # permanent hold at ManualTemperature
MODE_VACATION = 4

# The web app's "away" puts the thermostat in manual mode at 5 °C; we use it as "off".
OFF_TEMPERATURE = 500
