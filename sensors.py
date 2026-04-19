"""
sensors.py — All sensor reads.  Every hardware path is non-fatal.

ADS1115 (I2C 0x48):
  AIN0 → salt cell polarity verify    (voltage divider, read-only)
  AIN1 → air  temperature thermistor  (10kΩ NTC, B=3950)
  AIN2 → water temperature thermistor (10kΩ NTC, B=3950) — NOT CONNECTED
  AIN3 → ACS712 30A current sensor    (66 mV/A, zero = 2.5 V)

Thermistor voltage-divider: VCC → R_REF(10kΩ) → AINx → Thermistor → GND
Steinhart-Hart B-parameter:  1/T = 1/T₀ + (1/B)·ln(R/R₀)

Atlas EZO-EC (UART /dev/serial0 @ 9600):
  Send "R\r" → wait 600 ms → read ASCII conductivity in μS/cm.

Flow switch (GPIO 17, active LOW):
  GPIO LOW = switch closed = water flowing.
"""

import logging
import math
import time
from typing import Optional

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ADS1115
# ---------------------------------------------------------------------------
_ads = None
_ads_channels: list = []
_ads_ok = False
_acs712_powered = False


def _init_ads() -> bool:
    global _ads, _ads_channels, _ads_ok
    try:
        import board  # type: ignore
        import busio  # type: ignore
        import adafruit_ads1x15.ads1115 as ADS  # type: ignore
        from adafruit_ads1x15.analog_in import AnalogIn  # type: ignore

        i2c = busio.I2C(board.SCL, board.SDA)
        _ads = ADS.ADS1115(i2c, address=config.ADS_I2C_ADDR, gain=2/3)
        _ads_channels = [
            AnalogIn(_ads, 0),
            AnalogIn(_ads, 1),
            AnalogIn(_ads, 2),
            AnalogIn(_ads, 3),
        ]
        _ads_ok = True
        logger.info("ADS1115 ready at I2C 0x%02X gain=%.4f (±6.144V)", config.ADS_I2C_ADDR, _ads.gain)
        return True
    except Exception as exc:
        logger.warning("ADS1115 unavailable: %s", exc)
        return False


def _ads_voltage(ch: int) -> Optional[float]:
    if not _ads_ok or ch >= len(_ads_channels):
        return None
    try:
        return float(_ads_channels[ch].voltage)
    except Exception as exc:
        logger.debug("ADS1115 ch%d read error: %s", ch, exc)
        return None


def _steinhart_hart(v: float) -> Optional[float]:
    """Thermistor voltage → °C using B-parameter Steinhart-Hart."""
    try:
        vcc = config.ADS_VCC
        if v <= 0.0 or v >= vcc:
            return None
        r = config.THERM_R_REF * v / (vcc - v)
        t0k = config.THERM_T0 + 273.15
        inv_t = 1.0 / t0k + (1.0 / config.THERM_B) * math.log(r / config.THERM_R0)
        return round(1.0 / inv_t - 273.15, 2)
    except (ValueError, ZeroDivisionError):
        return None


def read_water_temp() -> Optional[float]:
    v = _ads_voltage(config.ADS_CH_WATER_TEMP)
    return _steinhart_hart(v) if v is not None else None


def read_air_temp() -> Optional[float]:
    v = _ads_voltage(config.ADS_CH_AIR_TEMP)
    return _steinhart_hart(v) if v is not None else None


def set_acs712_powered(on: bool) -> None:
    global _acs712_powered
    _acs712_powered = on


def read_current() -> Optional[float]:
    """Return current in Amperes (signed; positive = load direction).
    Returns None if ACS712 Vcc has not been energized yet (CH4 gate)."""
    if not _acs712_powered:
        return None
    v = _ads_voltage(config.ADS_CH_CURRENT)
    if v is None:
        return None
    return round((v - config.ACS_ZERO_V) / config.ACS_SENSITIVITY, 3)


# ---------------------------------------------------------------------------
# Atlas EZO-EC conductivity probe
# ---------------------------------------------------------------------------
_ec_serial = None
_ec_ok = False


def _init_ec() -> bool:
    global _ec_serial, _ec_ok
    try:
        import serial  # type: ignore
        _ec_serial = serial.Serial(
            config.EC_PORT,
            baudrate=config.EC_BAUD,
            timeout=config.EC_TIMEOUT,
        )
        _ec_ok = True
        logger.info("EZO-EC serial opened on %s", config.EC_PORT)
        return True
    except Exception as exc:
        logger.warning("EZO-EC unavailable (%s): %s", config.EC_PORT, exc)
        return False


def read_conductivity() -> Optional[float]:
    """Request one reading.  Blocks ~650 ms while probe responds."""
    if not _ec_ok or _ec_serial is None:
        return None
    try:
        _ec_serial.read(_ec_serial.in_waiting)
        _ec_serial.write(b"R\r")
        time.sleep(0.65)
        line = _ec_serial.readline().decode("ascii", errors="replace").strip()
        # Probe prefixes error lines with '*'; a valid line is just the number
        if not line or line.startswith("*"):
            return None
        return round(float(line), 2)
    except Exception as exc:
        logger.debug("EZO-EC read error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Flow switch (GPIO 17, active LOW)
# ---------------------------------------------------------------------------
_gpio_ok = False


def _init_flow() -> bool:
    global _gpio_ok
    try:
        import RPi.GPIO as GPIO  # type: ignore
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        pull = GPIO.PUD_UP if config.FLOW_ACTIVE_LOW else GPIO.PUD_DOWN
        GPIO.setup(config.FLOW_GPIO, GPIO.IN, pull_up_down=pull)
        _gpio_ok = True
        logger.info("Flow switch ready on GPIO %d", config.FLOW_GPIO)
        return True
    except Exception as exc:
        logger.warning("Flow switch GPIO unavailable: %s", exc)
        return False


def read_flow() -> bool:
    """Return True if water is confirmed flowing."""
    if not _gpio_ok:
        return False
    try:
        import RPi.GPIO as GPIO  # type: ignore
        raw = GPIO.input(config.FLOW_GPIO)
        # Active LOW: pin LOW → switch closed → flow present
        return (raw == 0) if config.FLOW_ACTIVE_LOW else (raw == 1)
    except Exception as exc:
        logger.debug("Flow GPIO read error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Module lifecycle
# ---------------------------------------------------------------------------

def init() -> None:
    _init_ads()
    _init_ec()
    _init_flow()


def cleanup() -> None:
    if _ec_serial is not None:
        try:
            _ec_serial.close()
        except Exception:
            pass
    if _gpio_ok:
        try:
            import RPi.GPIO as GPIO  # type: ignore
            GPIO.cleanup()
        except Exception:
            pass
