"""
main.py — Jarvis Pool Controller

Connects to MQTT and publishes/subscribes to pool sensor topics.
All hardware (pump GPIO, ADS1115 ADC, EZO-EC probe) is initialised
with a try/except so failures are logged but never crash the service.
"""

import logging
import os
import signal
import sys
import time

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pool")

# ---------------------------------------------------------------------------
# Optional hardware: ADS1115
# ---------------------------------------------------------------------------
ads = None
chan_orp = None
chan_ph = None
try:
    import board  # type: ignore
    import busio  # type: ignore
    import adafruit_ads1x15.ads1115 as ADS  # type: ignore
    from adafruit_ads1x15.analog_in import AnalogIn  # type: ignore

    i2c = busio.I2C(board.SCL, board.SDA)
    ads = ADS.ADS1115(i2c)
    chan_orp = AnalogIn(ads, ADS.P0)
    chan_ph = AnalogIn(ads, ADS.P1)
    logger.info("ADS1115 initialised")
except Exception as exc:
    logger.warning("ADS1115 unavailable: %s", exc)

# ---------------------------------------------------------------------------
# Optional hardware: EZO-EC conductivity probe (I2C address 0x64)
# ---------------------------------------------------------------------------
ec_sensor = None
try:
    import smbus2  # type: ignore

    bus = smbus2.SMBus(1)
    EC_ADDR = 0x64
    # Probe presence check — write a no-op 'i' command
    bus.write_i2c_block_data(EC_ADDR, 0, [ord("i")])
    time.sleep(0.3)
    ec_sensor = bus
    logger.info("EZO-EC probe found at 0x%02X", EC_ADDR)
except Exception as exc:
    logger.warning("EZO-EC unavailable: %s", exc)

# ---------------------------------------------------------------------------
# Pump module (already non-fatal internally)
# ---------------------------------------------------------------------------
import pump  # noqa: E402

# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
import paho.mqtt.client as mqtt  # noqa: E402

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

BASE_TOPIC = "pool"
PUMP_CMD_TOPIC = f"{BASE_TOPIC}/pump/set"
PUMP_STATE_TOPIC = f"{BASE_TOPIC}/pump/state"
STATUS_TOPIC = f"{BASE_TOPIC}/status"


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        logger.warning("MQTT connect failed: %s", reason_code)
    else:
        logger.info("MQTT connected to %s:%s", MQTT_HOST, MQTT_PORT)
        client.subscribe(PUMP_CMD_TOPIC)
        client.publish(STATUS_TOPIC, "online", retain=True)


def on_disconnect(client, userdata, flags, reason_code, properties):
    logger.warning("MQTT disconnected: %s — will auto-reconnect", reason_code)


def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode("utf-8", errors="replace").strip().lower()
    logger.info("MQTT ← %s: %s", topic, payload)

    if topic == PUMP_CMD_TOPIC:
        state = payload in ("on", "1", "true")
        pump.set_pump(state)
        client.publish(PUMP_STATE_TOPIC, "ON" if state else "OFF", retain=True)


def read_sensors(client):
    """Read available sensors and publish results."""
    if chan_orp is not None:
        try:
            orp_v = chan_orp.voltage
            client.publish(f"{BASE_TOPIC}/orp/voltage", round(orp_v, 4))
        except Exception as exc:
            logger.debug("ORP read error: %s", exc)

    if chan_ph is not None:
        try:
            ph_v = chan_ph.voltage
            client.publish(f"{BASE_TOPIC}/ph/voltage", round(ph_v, 4))
        except Exception as exc:
            logger.debug("pH read error: %s", exc)

    if ec_sensor is not None:
        try:
            ec_sensor.write_i2c_block_data(0x64, 0, [ord("r")])
            time.sleep(0.6)
            raw = ec_sensor.read_i2c_block_data(0x64, 0, 20)
            response = bytes(raw[1:]).split(b"\x00")[0].decode()
            client.publish(f"{BASE_TOPIC}/ec", response)
        except Exception as exc:
            logger.debug("EC read error: %s", exc)


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="jarvis-pool")
    client.will_set(STATUS_TOPIC, "offline", retain=True)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    # Non-blocking connect with automatic reconnect
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    def _shutdown(sig, frame):
        logger.info("Shutting down…")
        client.publish(STATUS_TOPIC, "offline", retain=True)
        client.loop_stop()
        client.disconnect()
        pump.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("Pool controller running (Ctrl-C to stop)")
    while True:
        read_sensors(client)
        time.sleep(30)


if __name__ == "__main__":
    main()
