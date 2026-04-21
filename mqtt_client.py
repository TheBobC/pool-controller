"""
mqtt_client.py — paho-mqtt v2 client with HA auto-discovery, LWT, reconnect.

paho runs its network loop in a background thread (loop_start).
Inbound messages are forwarded to the asyncio event loop via
asyncio.call_soon_threadsafe() into an asyncio.Queue.

MQTT topics  (prefix = jarvis/pool/TudorPool):
  jarvis/pool/TudorPool/system/status           LWT  "online" / "offline"
  jarvis/pool/TudorPool/system/cpu_percent      published  float %
  jarvis/pool/TudorPool/system/cpu_temp         published  float °F
  jarvis/pool/TudorPool/system/memory_percent   published  float %
  jarvis/pool/TudorPool/system/disk_percent     published  float %
  jarvis/pool/TudorPool/system/wifi_signal      published  integer dBm
  jarvis/pool/TudorPool/system/uptime_seconds   published  integer s
  jarvis/pool/TudorPool/pump/speed              published  0–100
  jarvis/pool/TudorPool/pump/speed/set          subscribed 0–100
  jarvis/pool/TudorPool/pump/running            published  "ON" / "OFF"
  jarvis/pool/TudorPool/pump/rpm                published  integer RPM (from EcoStar telemetry)
  jarvis/pool/TudorPool/pump/power              published  watts (from EcoStar telemetry)
  jarvis/pool/TudorPool/cell/state                        published  "ON" / "OFF"
  jarvis/pool/TudorPool/cell/set                          subscribed "ON" / "OFF"
  jarvis/pool/TudorPool/cell/interlock                    published  "ON" / "OFF"
  jarvis/pool/TudorPool/cell/polarity                     published  "forward" / "reverse"
  jarvis/pool/TudorPool/cell/cmd/polarity                 subscribed "toggle"
  jarvis/pool/TudorPool/cell/polarity_accumulated_s       published  integer s (on-time this polarity period)
  jarvis/pool/TudorPool/cell/polarity_remaining_s         published  integer s (until next auto-reverse)
  jarvis/pool/TudorPool/cell/super_chlorinate             published  "ON" / "OFF"
  jarvis/pool/TudorPool/cell/super_chlorinate/set         subscribed "ON" / "OFF"
  jarvis/pool/TudorPool/cell/super_chlorinate_remaining_s published  integer s (until super chlorinate expires)
  jarvis/pool/TudorPool/fans/state                        published  "ON" / "OFF"
  jarvis/pool/TudorPool/sensors/water_temp      published  °F
  jarvis/pool/TudorPool/sensors/air_temp        published  °F
  jarvis/pool/TudorPool/sensors/pump_current    published  A  (derived: watts / PUMP_VOLTAGE, from RS-485)
  jarvis/pool/TudorPool/sensors/cell_current    published  A  (salt cell circuit, ACS712 on AIN3)
  jarvis/pool/TudorPool/sensors/conductivity    published  μS/cm
  jarvis/pool/TudorPool/sensors/flow            published  "ON" / "OFF"
"""

import asyncio
import json
import logging
from typing import Callable, Optional

import paho.mqtt.client as mqtt

import config

logger = logging.getLogger(__name__)

T = config.TOPIC_PREFIX
D = config.HA_DISCOVERY_PREFIX

_DEVICE = {
    "identifiers": ["jarvis_pool"],
    "name": "Jarvis Pool Controller",
    "model": "Raspberry Pi 3B+",
    "manufacturer": "Jarvis Home Automation",
}

# Stale retained discovery entries to delete from the broker on connect.
# Publish empty payload to remove them from HA.
_TOMBSTONES: list[tuple[str, str]] = [
    ("binary_sensor", "jarvis_pool_cell_allowed"),   # v1 "Cell Interlock" → duplicate of jarvis_pool_cell_interlock
    ("sensor",        "jarvis_pool_pump_watts"),      # v1 "Pump Power" → duplicate of jarvis_pool_pump_power
    ("number",        "jarvis_pool_pump_set_rpm"),    # v1 RPM control — removed, RPM is read-only telemetry
    ("sensor",        "jarvis_pool_spa_temp"),        # v1 "Spa Temperature" — no spa in system
]

# (component, unique_id, discovery_payload)
_DISCOVERY: list[tuple[str, str, dict]] = [
    # ---- Controls ----
    ("number", "jarvis_pool_pump_speed", {
        "name": "Pool Pump Speed",
        "unique_id": "jarvis_pool_pump_speed",
        "command_topic": f"{T}/pump/speed/set",
        "state_topic": f"{T}/pump/speed",
        "min": 0, "max": 100, "step": 1,
        "unit_of_measurement": "%",
        "icon": "mdi:pump",
        "retain": True,
        "device": _DEVICE,
    }),
    ("switch", "jarvis_pool_cell", {
        "name": "Pool Salt Cell",
        "unique_id": "jarvis_pool_cell",
        "command_topic": f"{T}/cell/set",
        "state_topic": f"{T}/cell/state",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:lightning-bolt-circle",
        "retain": True,
        "device": _DEVICE,
    }),
    # ---- Temperature sensors ----
    ("sensor", "jarvis_pool_water_temp", {
        "name": "Pool Water Temperature",
        "unique_id": "jarvis_pool_water_temp",
        "state_topic": f"{T}/sensors/water_temp",
        "unit_of_measurement": "°F",
        "device_class": "temperature",
        "state_class": "measurement",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_air_temp", {
        "name": "Pool Air Temperature",
        "unique_id": "jarvis_pool_air_temp",
        "state_topic": f"{T}/sensors/air_temp",
        "unit_of_measurement": "°F",
        "device_class": "temperature",
        "state_class": "measurement",
        "device": _DEVICE,
    }),
    # ---- Pump telemetry (populated when EcoStar telemetry reading is implemented) ----
    ("sensor", "jarvis_pool_pump_rpm", {
        "name": "Pump RPM",
        "unique_id": "jarvis_pool_pump_rpm",
        "state_topic": f"{T}/pump/rpm",
        "unit_of_measurement": "RPM",
        "state_class": "measurement",
        "icon": "mdi:fan",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_pump_power", {
        "name": "Pump Power",
        "unique_id": "jarvis_pool_pump_power",
        "state_topic": f"{T}/pump/power",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "device": _DEVICE,
    }),
    # ---- Current sensors ----
    ("sensor", "jarvis_pool_current", {
        "name": "Pool Pump Current",
        "unique_id": "jarvis_pool_current",
        "state_topic": f"{T}/sensors/pump_current",
        "unit_of_measurement": "A",
        "device_class": "current",
        "state_class": "measurement",
        "icon": "mdi:current-ac",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_cell_current", {
        "name": "Salt Cell Current",
        "unique_id": "jarvis_pool_cell_current",
        "state_topic": f"{T}/sensors/cell_current",
        "unit_of_measurement": "A",
        "device_class": "current",
        "state_class": "measurement",
        "icon": "mdi:current-dc",
        "device": _DEVICE,
    }),
    # ---- Conductivity ----
    ("sensor", "jarvis_pool_conductivity", {
        "name": "Pool Conductivity",
        "unique_id": "jarvis_pool_conductivity",
        "state_topic": f"{T}/sensors/conductivity",
        "unit_of_measurement": "µS/cm",
        "icon": "mdi:water-percent",
        "state_class": "measurement",
        "device": _DEVICE,
    }),
    # ---- System health ----
    ("binary_sensor", "jarvis_pool_controller_online", {
        "name": "Pool Controller Online",
        "unique_id": "jarvis_pool_controller_online",
        "state_topic": f"{T}/system/status",
        "payload_on": "online",
        "payload_off": "offline",
        "device_class": "connectivity",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_system_cpu_percent", {
        "name": "Pool Controller CPU",
        "unique_id": "jarvis_pool_system_cpu_percent",
        "state_topic": f"{T}/system/cpu_percent",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "icon": "mdi:cpu-64-bit",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_system_cpu_temp", {
        "name": "Pool Controller CPU Temperature",
        "unique_id": "jarvis_pool_system_cpu_temp",
        "state_topic": f"{T}/system/cpu_temp",
        "unit_of_measurement": "°F",
        "device_class": "temperature",
        "state_class": "measurement",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_system_memory_percent", {
        "name": "Pool Controller Memory",
        "unique_id": "jarvis_pool_system_memory_percent",
        "state_topic": f"{T}/system/memory_percent",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "icon": "mdi:memory",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_system_disk_percent", {
        "name": "Pool Controller Disk",
        "unique_id": "jarvis_pool_system_disk_percent",
        "state_topic": f"{T}/system/disk_percent",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "icon": "mdi:harddisk",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_system_wifi_signal", {
        "name": "Pool Controller WiFi Signal",
        "unique_id": "jarvis_pool_system_wifi_signal",
        "state_topic": f"{T}/system/wifi_signal",
        "unit_of_measurement": "dBm",
        "device_class": "signal_strength",
        "state_class": "measurement",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_system_uptime_seconds", {
        "name": "Pool Controller Uptime",
        "unique_id": "jarvis_pool_system_uptime_seconds",
        "state_topic": f"{T}/system/uptime_seconds",
        "unit_of_measurement": "s",
        "device_class": "duration",
        "state_class": "total_increasing",
        "device": _DEVICE,
    }),
    # ---- Binary sensors ----
    ("binary_sensor", "jarvis_pool_flow", {
        "name": "Pool Flow",
        "unique_id": "jarvis_pool_flow",
        "state_topic": f"{T}/sensors/flow",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "opening",
        "device": _DEVICE,
    }),
    ("binary_sensor", "jarvis_pool_pump_running", {
        "name": "Pool Pump Running",
        "unique_id": "jarvis_pool_pump_running",
        "state_topic": f"{T}/pump/running",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "running",
        "device": _DEVICE,
    }),
    ("binary_sensor", "jarvis_pool_fans", {
        "name": "Pool Enclosure Fans",
        "unique_id": "jarvis_pool_fans",
        "state_topic": f"{T}/fans/state",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "running",
        "icon": "mdi:fan",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_cell_polarity", {
        "name": "Salt Cell Polarity",
        "unique_id": "jarvis_pool_cell_polarity",
        "state_topic": f"{T}/cell/polarity",
        "icon": "mdi:swap-horizontal",
        "device": _DEVICE,
    }),
    ("button", "jarvis_pool_cell_polarity_toggle", {
        "name": "Salt Cell Polarity Toggle",
        "unique_id": "jarvis_pool_cell_polarity_toggle",
        "command_topic": f"{T}/cell/cmd/polarity",
        "payload_press": "toggle",
        "icon": "mdi:swap-horizontal-bold",
        "device": _DEVICE,
    }),
    ("binary_sensor", "jarvis_pool_cell_interlock", {
        "name": "Cell Interlock",
        "unique_id": "jarvis_pool_cell_interlock",
        "state_topic": f"{T}/cell/interlock",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:lock-check",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_polarity_on_time", {
        "name": "Salt Cell Polarity Timer",
        "unique_id": "jarvis_pool_polarity_on_time",
        "state_topic": f"{T}/cell/polarity_on_time_s",
        "unit_of_measurement": "s",
        "device_class": "duration",
        "state_class": "measurement",
        "icon": "mdi:timer",
        "device": _DEVICE,
    }),
    # ---- Polarity accumulated / remaining (Task 2) ----
    ("sensor", "jarvis_pool_polarity_accumulated", {
        "name": "Salt Cell Polarity Accumulated",
        "unique_id": "jarvis_pool_polarity_accumulated",
        "state_topic": f"{T}/cell/polarity_accumulated_s",
        "unit_of_measurement": "s",
        "device_class": "duration",
        "state_class": "measurement",
        "icon": "mdi:timer-play",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_polarity_remaining", {
        "name": "Salt Cell Polarity Remaining",
        "unique_id": "jarvis_pool_polarity_remaining",
        "state_topic": f"{T}/cell/polarity_remaining_s",
        "unit_of_measurement": "s",
        "device_class": "duration",
        "state_class": "measurement",
        "icon": "mdi:timer-sand",
        "device": _DEVICE,
    }),
    # ---- Super Chlorinate (Task 3) ----
    ("switch", "jarvis_pool_super_chlorinate", {
        "name": "Super Chlorinate",
        "unique_id": "jarvis_pool_super_chlorinate",
        "command_topic": f"{T}/cell/super_chlorinate/set",
        "state_topic": f"{T}/cell/super_chlorinate",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:water-plus",
        "retain": True,
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_super_chlorinate_remaining", {
        "name": "Super Chlorinate Remaining",
        "unique_id": "jarvis_pool_super_chlorinate_remaining",
        "state_topic": f"{T}/cell/super_chlorinate_remaining_s",
        "unit_of_measurement": "s",
        "device_class": "duration",
        "state_class": "measurement",
        "icon": "mdi:timer-outline",
        "device": _DEVICE,
    }),
]


class MQTTClient:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()
        self._connected = False

        self._on_speed_set: Optional[Callable[[int], None]] = None
        self._on_cell_set: Optional[Callable[[bool], None]] = None
        self._on_polarity_toggle: Optional[Callable[[], None]] = None
        self._on_super_chlorinate_set: Optional[Callable[[bool], None]] = None

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="jarvis-pool",
        )
        self._client.will_set(f"{T}/system/status", "offline", qos=1, retain=True)
        if config.MQTT_USER:
            self._client.username_pw_set(config.MQTT_USER, config.MQTT_PASSWORD)

        self._client.on_connect    = self._cb_connect
        self._client.on_disconnect = self._cb_disconnect
        self._client.on_message    = self._cb_message

    # ------------------------------------------------------------------
    # paho callbacks — run in paho's thread
    # ------------------------------------------------------------------

    def _cb_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            logger.warning("MQTT connect failed: %s", reason_code)
            return
        self._connected = True
        logger.info("MQTT connected → %s:%d", config.MQTT_HOST, config.MQTT_PORT)
        client.subscribe(f"{T}/pump/speed/set")
        client.subscribe(f"{T}/cell/set")
        client.subscribe(f"{T}/cell/cmd/polarity")
        client.subscribe(f"{T}/cell/super_chlorinate/set")
        client.publish(f"{T}/system/status", "online", qos=1, retain=True)
        self._publish_discovery(client)

    def _cb_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected = False
        logger.warning("MQTT disconnected: %s (auto-reconnect active)", reason_code)

    def _cb_message(self, client, userdata, msg):
        # Bridge paho thread → asyncio
        self._loop.call_soon_threadsafe(self._queue.put_nowait, msg)

    # ------------------------------------------------------------------
    # HA discovery
    # ------------------------------------------------------------------

    def _publish_discovery(self, client) -> None:
        for component, uid in _TOMBSTONES:
            topic = f"{D}/{component}/{uid}/config"
            logger.info("Tombstoning: %s", topic)
            client.publish(topic, "", qos=1, retain=True)
        for component, uid, payload in _DISCOVERY:
            topic = f"{D}/{component}/{uid}/config"
            client.publish(topic, json.dumps(payload), qos=1, retain=True)
        logger.info("HA auto-discovery published (%d entities, %d tombstones)",
                    len(_DISCOVERY), len(_TOMBSTONES))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self._client.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=60)
        self._client.loop_start()

    def disconnect(self) -> None:
        try:
            self._client.publish(f"{T}/system/status", "offline", qos=1, retain=True)
        except Exception:
            pass
        self._client.loop_stop()
        self._client.disconnect()

    def publish(self, subtopic: str, value, retain: bool = False) -> None:
        if not self._connected:
            return
        try:
            self._client.publish(f"{T}/{subtopic}", str(value), retain=retain)
        except Exception as exc:
            logger.debug("publish error: %s", exc)

    def is_connected(self) -> bool:
        return self._connected

    def register_speed_handler(self, fn: Callable[[int], None]) -> None:
        self._on_speed_set = fn

    def register_cell_handler(self, fn: Callable[[bool], None]) -> None:
        self._on_cell_set = fn

    def register_polarity_toggle_handler(self, fn: Callable[[], None]) -> None:
        self._on_polarity_toggle = fn

    def register_super_chlorinate_handler(self, fn: Callable[[bool], None]) -> None:
        self._on_super_chlorinate_set = fn

    async def message_loop(self) -> None:
        """Dispatch inbound commands.  Run as an asyncio task."""
        try:
            while True:
                msg = await self._queue.get()
                topic   = msg.topic
                payload = msg.payload.decode("utf-8", errors="replace").strip()
                logger.debug("MQTT ← %s: %s", topic, payload)

                if topic == f"{T}/pump/speed/set":
                    try:
                        speed = int(float(payload))
                        if self._on_speed_set:
                            self._on_speed_set(speed)
                    except ValueError:
                        logger.warning("Bad speed payload: %r", payload)

                elif topic == f"{T}/cell/set":
                    if self._on_cell_set:
                        self._on_cell_set(payload.upper() == "ON")

                elif topic == f"{T}/cell/cmd/polarity":
                    if self._on_polarity_toggle:
                        self._on_polarity_toggle()

                elif topic == f"{T}/cell/super_chlorinate/set":
                    if self._on_super_chlorinate_set:
                        self._on_super_chlorinate_set(payload.upper() == "ON")
        except asyncio.CancelledError:
            pass
