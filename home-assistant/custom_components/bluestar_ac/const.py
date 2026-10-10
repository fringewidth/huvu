"""Constants for the Blue Star Smart AC integration."""

DOMAIN = "bluestar_ac"

CONF_PHONE = "phone"

API_BASE = "https://n3on22cp53.execute-api.ap-south-1.amazonaws.com/prod"
REGION = "ap-south-1"

# Headers the Blue Star app sends. The API may refuse requests without them.
APP_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "com.bluestarindia.bluesmart",
    "X-APP-VER": "v4.13.12-148",
    "X-OS-NAME": "Android",
    "X-OS-VER": "v15-35",
}

# What the app writes as `src` on a command it sends over the cloud.
COMMAND_SOURCE = "anmq"

# What `src` says on a report the integration asked for, which changes nothing.
STATE_REQUEST = "fpush"

# Who made the last change, from `src` on a state report. Anything else
# shows as Blue Star sent it.
SOURCES = {
    "anmq": "app",
    "anble": "app over Bluetooth",
    "alx": "Alexa",
    "spush.power": "power restored",
}

# Mode numbers, from the AC's model config. 1 is heat, on models that have it.
MODE_FAN = 0
MODE_HEAT = 1
MODE_COOL = 2
MODE_DRY = 3
MODE_AUTO = 4

# Swing values. 0 swings, 6 stops; 1 to 5 hold a vertical position.
SWING_ON = 0
SWING_OFF = 6

POLL_SECONDS = 60
CONFIRM_SECONDS = 10
