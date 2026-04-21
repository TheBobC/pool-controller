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
# Pump — Hayward EcoStar SP3400VSP via USB RS485 adapter
# ---------------------------------------------------------------------------
PUMP_PORT = os.getenv("PUMP_PORT", "/dev/ttyUSB0")
PUMP_BAUD = int(os.getenv("PUMP_BAUD", "19200"))
PUMP_ADDR = 0x01        # EcoStar pump node address
CTRL_ADDR = 0x0C        # This controller's address on the bus
PUMP_KEEPALIVE_S = 0.5  # Packet interval; pump reverts to panel if > ~2s gap
PUMP_VOLTAGE = float(os.getenv("PUMP_VOLTAGE", "230.0"))  # SP3400VSP is 230 V single-phase

# ---------------------------------------------------------------------------
# Salt cell / enclosure fans — GeeekPi 4-channel relay HAT
# CH1 = Gate          — cell mains power (must be OFF during any polarity change)
# CH2 = Polarity      — drives A+B coils tied in parallel; one channel flips both
# CH3 = Fans          — enclosure cooling
# CH4 = ACS712 power  — gates ACS712 Vcc; energized 5 s after boot, de-energized at shutdown
#
# HAT uses per-channel I2C registers (register N = channel N, 1-based) with
# INVERTED logic, confirmed by bench testing:
#   write 0x00 → channel ENERGISED (relay ON)
#   write 0xFF → channel DE-ENERGISED (relay OFF)
# This is opposite to the HAT's datasheet. Init writes 0xFF to all four
# registers as the first I2C operation so no channel floats energised at
# startup. Silkscreen NO/NC is also reversed on this HAT.
# ---------------------------------------------------------------------------
CELL_I2C_ADDR          = int(os.getenv("CELL_I2C_ADDR",          "0x10"), 16)
CELL_RELAY_CH_GATE     = int(os.getenv("CELL_RELAY_CH_GATE",     "1"))  # 1-based
CELL_RELAY_CH_POLARITY = int(os.getenv("CELL_RELAY_CH_POLARITY", "2"))  # 1-based
FAN_RELAY_CH           = int(os.getenv("FAN_RELAY_CH",           "3"))  # 1-based
ACS712_POWER_CHANNEL   = int(os.getenv("ACS712_POWER_CHANNEL",   "4"))  # 1-based

# Polarity switching — MUST NOT run while gate is energised
POLARITY_SWITCH_DELAY_S = float(os.getenv("POLARITY_SWITCH_DELAY_S", "3.0"))

# Enclosure fans — on if cell is active OR air temp exceeds threshold (°F)
FAN_TEMP_THRESHOLD = float(os.getenv("FAN_TEMP_THRESHOLD", "90.0"))

# ---------------------------------------------------------------------------
# ADS1115 ADC
# ---------------------------------------------------------------------------
ADS_I2C_ADDR    = int(os.getenv("ADS_I2C_ADDR", "0x48"), 16)
ADS_CH_POLARITY    = 0   # AIN0 — salt cell polarity verify (voltage divider)
ADS_CH_AIR_TEMP    = 1   # AIN1 — air temp thermistor
ADS_CH_WATER_TEMP  = 2   # AIN2 — water temp thermistor (not connected)
ADS_CH_CURRENT     = 3   # AIN3 — ACS712 30A current sensor
ADS_VCC = float(os.getenv("ADS_VCC", "3.3"))

# Thermistor — Steinhart-Hart B-parameter
# Divider: VCC → R_REF → (AIN) → Thermistor → GND
THERM_B     = 3950.0    # B coefficient
THERM_R0    = 10_000.0  # Resistance at T0 (Ω)
THERM_T0    = 25.0      # Reference temperature (°C)
THERM_R_REF = 10_000.0  # Series reference resistor (Ω)

# ACS712-30A current sensor — powered from 5V. Datasheet: 66 mV/A, Vout = Vcc/2 at 0A.
ACS_SENSITIVITY = 0.066  # V/A  (66 mV/A for 30 A model)
ACS_ZERO_V      = 2.5    # Volts at zero current (Vcc/2 = 2.5 V at 5 V supply)

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
CELL_POLARITY_REVERSE_INTERVAL_S = float(os.getenv("CELL_POLARITY_REVERSE_INTERVAL_S", "7200"))  # 2 h
SUPER_CHLORINATE_DURATION_S = float(os.getenv("SUPER_CHLORINATE_DURATION_S", "86400"))  # 24 h

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
