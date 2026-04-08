"""config.py — All settings loaded from .env with sensible defaults."""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
MQTT_HOST = os.getenv("MQTT_HOST", "10.0.0.16")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "jarvis/pool/TudorPool")
HA_DISCOVERY_PREFIX = os.getenv("HA_DISCOVERY_PREFIX", "homeassistant")

# ---------------------------------------------------------------------------
# Pump — Hayward EcoStar SP3400VSP via Waveshare 2-CH RS485 HAT
# ---------------------------------------------------------------------------
PUMP_PORT = os.getenv("PUMP_PORT", "/dev/ttySC0")
PUMP_BAUD = int(os.getenv("PUMP_BAUD", "19200"))
PUMP_ADDR = 0x01        # EcoStar pump node address
CTRL_ADDR = 0x0C        # This controller's address on the bus
PUMP_KEEPALIVE_S = 0.5  # Packet interval; pump reverts to panel if > ~2s gap

# ---------------------------------------------------------------------------
# Salt cell — GeeekPi 4-channel relay HAT
# ---------------------------------------------------------------------------
CELL_I2C_ADDR = int(os.getenv("CELL_I2C_ADDR", "0x10"), 16)
CELL_RELAY_CH = int(os.getenv("CELL_RELAY_CH", "1"))     # 1-based channel
CELL_RELAY_INVERT = os.getenv("CELL_RELAY_INVERT", "false").lower() == "true"

# ---------------------------------------------------------------------------
# ADS1115 ADC
# ---------------------------------------------------------------------------
ADS_I2C_ADDR = int(os.getenv("ADS_I2C_ADDR", "0x48"), 16)
ADS_CH_WATER_TEMP = 0   # AIN0
ADS_CH_AIR_TEMP   = 1   # AIN1
ADS_CH_CURRENT    = 2   # AIN2
ADS_VCC = float(os.getenv("ADS_VCC", "3.3"))

# Thermistor — Steinhart-Hart B-parameter
# Divider: VCC → R_REF → (AIN) → Thermistor → GND
THERM_B     = 3950.0    # B coefficient
THERM_R0    = 10_000.0  # Resistance at T0 (Ω)
THERM_T0    = 25.0      # Reference temperature (°C)
THERM_R_REF = 10_000.0  # Series reference resistor (Ω)

# ACS712 30A current sensor
ACS_SENSITIVITY = 0.066  # V/A  (66 mV/A for 30 A model)
ACS_ZERO_V      = 2.5    # Volts at zero current (VCC/2)

# ---------------------------------------------------------------------------
# Atlas EZO-EC conductivity probe
# ---------------------------------------------------------------------------
EC_PORT    = os.getenv("EC_PORT", "/dev/serial0")
EC_BAUD    = int(os.getenv("EC_BAUD", "9600"))
EC_TIMEOUT = 2.0  # Serial read timeout (s)

# ---------------------------------------------------------------------------
# Flow switch
# ---------------------------------------------------------------------------
FLOW_GPIO       = int(os.getenv("FLOW_GPIO", "17"))
FLOW_ACTIVE_LOW = True   # GPIO LOW = switch closed = water flowing

# ---------------------------------------------------------------------------
# Safety interlocks
# ---------------------------------------------------------------------------
CELL_PUMP_MIN_SPEED = 1      # Pump must be at least this % for cell to run
CELL_FLOW_DELAY_S   = 60.0   # Continuous flow + pump required before cell on

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# ---------------------------------------------------------------------------
# Home Assistant / Anthropic (optional integrations)
# ---------------------------------------------------------------------------
HA_HOST            = os.getenv("HA_HOST", "")
HA_PORT            = int(os.getenv("HA_PORT", "8123"))
HA_TOKEN           = os.getenv("HA_TOKEN", "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
