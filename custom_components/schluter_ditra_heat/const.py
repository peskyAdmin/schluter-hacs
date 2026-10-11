"""Constants for the Schluter DITRA-HEAT integration."""

from datetime import timedelta

DOMAIN = "schluter_ditra_heat"

BASE_URL = "https://ditra-heat-e-wifi.schluter.com"
DOCS_LOCAL_URL = "https://github.com/peskyAdmin/schluter-hacs#local-control-advanced"
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
# An empty long-poll answer faster than this means the server is not holding
# the request, so wait before re-opening instead of looping.
PUSH_MIN_EMPTY_SECONDS = 10

ACTION_UPDATED = 2
ACTION_REMOVED = 3

# RegulationMode values used by the Schluter web app.
MODE_SCHEDULE = 1
MODE_COMFORT = 2  # temporary hold until ComfortEndTime
MODE_MANUAL = 3  # permanent hold at ManualTemperature
MODE_VACATION = 4

# The web app's "away" puts the thermostat in manual mode at 5 °C; we use it as "off".
OFF_TEMPERATURE = 500

# --- Connection mode (advanced local control) -------------------------------
# The integration can reach the thermostat three ways. Cloud is the default and
# needs no setup. The two local modes run a gateway on this host that the
# thermostat connects to directly, so Home Assistant keeps working when the
# internet or OJ's cloud is down. They require a one-time LAN DNS redirect
# (see the README); that is why they are marked advanced in the options flow.
CONF_MODE = "connection_mode"
CONN_CLOUD = "cloud"  # talk to OJ's cloud REST API (default, unchanged)
CONN_RELAY = "relay"  # run the local gateway and also relay to OJ's cloud
CONN_LOCAL = "local"  # run the local gateway only, fully offline
DEFAULT_MODE = CONN_CLOUD
LOCAL_MODES = (CONN_RELAY, CONN_LOCAL)

# The hostname the thermostat dials for its device gateway. In a local mode a
# LAN DNS override points this name at the Home Assistant host so our gateway
# answers it instead of OJ's cloud.
GATEWAY_HOST = "na-thermos.ojelectronics.com"
# The device firmware dials this fixed port, so it is not configurable.
GATEWAY_PORT = 9533
# OJ's real gateway, reached by IP in relay mode so the DNS override does not
# loop back to us. If OJ ever moves it, relay mode can't reach the cloud and
# falls back to serving the thermostat locally (Home Assistant keeps working,
# the Schluter app goes offline) until this is updated.
UPSTREAM_GATEWAY_IP = "52.165.43.242"

# In a local mode, raise a repair issue if no thermostat has connected to the
# gateway this long after it starts (almost always a missing DNS record).
LOCAL_CONNECT_GRACE = 600

# Device wire protocol. The setpoint field (f9.f4.1) is Celsius x200, while the
# cloud API and this integration's coordinator use Celsius x100.
DEVICE_TEMP_SCALE = 200
# Device timestamps are unix seconds minus this (2000-01-01).
EPOCH_2000 = 946684800
