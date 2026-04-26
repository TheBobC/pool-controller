"""
mqtt_client.py — paho-mqtt v2 client with HA auto-discovery, LWT, reconnect.

paho runs its network loop in a background thread (loop_start).
Inbound messages are forwarded to the asyncio event loop via
asyncio.call_soon_threadsafe() into an asyncio.Queue.

Topic prefix: pool/  (SPEC §10.1; configurable via MQTT_TOPIC_PREFIX)
Bridge prefix: jarvis/pool/TudorPool  (24-hour dual-publish window; set MQTT_BRIDGE_OLD_PREFIX)

State topics (published by sat4):
  pool/lwt                      "online" / "offline"  (LWT)
  pool/pump/state               "ON" / "OFF"
  pool/pump/output_percent      0–100
  pool/pump/running             "ON" / "OFF"
  pool/pump/stable              "ON" / "OFF"
  pool/pump/countdown           integer s (pump-stable countdown)
  pool/pump/preload_active      "ON" / "OFF"
  pool/pump/preload_remaining_s integer s
  pool/pump/rpm                 integer RPM
  pool/pump/power               float W
  pool/cell/on                  "ON" / "OFF"  (user-requested enable state)
  pool/cell/gate_state          "ON" / "OFF"  (physical gate relay state)
  pool/cell/output_percent      0–100
  pool/cell/runtime_percent     0–100 % (rolling 30-min ACS712 duty)
  pool/cell/actual_duty_confidence 0–100 %
  pool/cell/countdown           integer s (cell boot-grace countdown)
  pool/cell/interlock           "ON" / "OFF"
  pool/cell/polarity_direction  "forward" / "reverse"
  pool/cell/polarity_accumulator integer s (on-time this polarity period)
  pool/cell/polarity_accumulated_s formatted HH:MM accumulated
  pool/cell/polarity_remaining_s   formatted HH:MM until next auto-reverse
  pool/cell/current_amps        float A
  pool/sc/active                "ON" / "OFF"
  pool/sc/remaining             integer s
  pool/service_mode             "ON" / "OFF"
  pool/system/mode              "on" / "off" / "service"
  pool/fault/state              fault name string or "none"
  pool/notifications            JSON {severity, message}
  pool/sensors/water_temp       float °F
  pool/sensors/air_temp         float °F
  pool/sensors/pump_current     float A
  pool/sensors/ec               float µS/cm
  pool/sensors/flow             "ON" / "OFF"
  pool/fans/state               "ON" / "OFF"
  pool/system/cpu               float %
  pool/system/temp              float °F
  pool/system/memory            float %
  pool/system/disk              float %
  pool/system/wifi_signal       integer dBm
  pool/system/uptime            integer s

Command topics (subscribed by sat4):
  pool/pump/set                 "ON" / "OFF"
  pool/pump/output_percent/set  0–100
  pool/cell/on/set              "ON" / "OFF"
  pool/cell/output_percent/set  0–100
  pool/cell/cmd/polarity        "toggle"
  pool/sc/set                   "ON" / "OFF"
  pool/service_mode/set         "ON" / "OFF"
  pool/fault/reset              any payload
"""

import asyncio
import json
import logging
from typing import Callable, Optional

import paho.mqtt.client as mqtt

import config

logger = logging.getLogger(__name__)

T     = config.TOPIC_PREFIX            # "pool" (new canonical prefix)
OLD_T = config.MQTT_BRIDGE_OLD_PREFIX  # "jarvis/pool/TudorPool" during bridge window; "" = disabled
D     = config.HA_DISCOVERY_PREFIX

_DEVICE = {
    "identifiers": ["jarvis_pool"],
    "name": "Jarvis Pool Controller",
    "model": "Raspberry Pi 3B+",
    "manufacturer": "Jarvis Home Automation",
}

# Stale retained discovery entries to delete from the broker on connect.
_TOMBSTONES: list[tuple[str, str]] = [
    ("binary_sensor", "jarvis_pool_cell_allowed"),
    ("sensor",        "jarvis_pool_pump_watts"),
    ("number",        "jarvis_pool_pump_set_rpm"),
    ("sensor",        "jarvis_pool_spa_temp"),
]

# (component, unique_id, discovery_payload)
_DISCOVERY: list[tuple[str, str, dict]] = [
    # ---- Pump preload ----
    ("binary_sensor", "jarvis_pool_pump_preload_active", {
        "name": "Pump Preload Active",
        "unique_id": "jarvis_pool_pump_preload_active",
        "state_topic": f"{T}/pump/preload_active",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "running",
        "icon": "mdi:timer-sand",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_pump_preload_remaining_s", {
        "name": "Pump Preload Remaining",
        "unique_id": "jarvis_pool_pump_preload_remaining_s",
        "state_topic": f"{T}/pump/preload_remaining_s",
        "unit_of_measurement": "s",
        "device_class": "duration",
        "state_class": "measurement",
        "icon": "mdi:timer-sand",
        "device": _DEVICE,
    }),
    # ---- Pump stable ----
    ("binary_sensor", "jarvis_pool_pump_stable", {
        "name": "Pump Stable",
        "unique_id": "jarvis_pool_pump_stable",
        "state_topic": f"{T}/pump/stable",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "running",
        "icon": "mdi:check-circle",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_pump_stable_countdown", {
        "name": "Pump Stable Countdown",
        "unique_id": "jarvis_pool_pump_stable_countdown",
        "state_topic": f"{T}/pump/countdown",
        "unit_of_measurement": "s",
        "device_class": "duration",
        "state_class": "measurement",
        "icon": "mdi:timer",
        "device": _DEVICE,
    }),
    # ---- Pump controls ----
    ("switch", "jarvis_pool_pump_power_on", {
        "name": "Pump Power",
        "unique_id": "jarvis_pool_pump_power_on",
        "command_topic": f"{T}/pump/set",
        "state_topic": f"{T}/pump/state",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:power",
        "retain": True,
        "device": _DEVICE,
    }),
    ("number", "jarvis_pool_pump_speed", {
        "name": "Pool Pump Speed",
        "unique_id": "jarvis_pool_pump_speed",
        "command_topic": f"{T}/pump/output_percent/set",
        "state_topic": f"{T}/pump/output_percent",
        "min": 0, "max": 100, "step": 1,
        "unit_of_measurement": "%",
        "icon": "mdi:pump",
        "retain": True,
        "device": _DEVICE,
    }),
    # ---- Cell controls ----
    ("switch", "jarvis_pool_cell", {
        "name": "Pool Salt Cell",
        "unique_id": "jarvis_pool_cell",
        "command_topic": f"{T}/cell/on/set",
        "state_topic": f"{T}/cell/on",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:lightning-bolt-circle",
        "retain": True,
        "device": _DEVICE,
    }),
    ("binary_sensor", "jarvis_pool_cell_gate_state", {
        "name": "Cell Gate State",
        "unique_id": "jarvis_pool_cell_gate_state",
        "state_topic": f"{T}/cell/gate_state",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "running",
        "icon": "mdi:gate",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_cell_countdown", {
        "name": "Cell Boot Grace Countdown",
        "unique_id": "jarvis_pool_cell_countdown",
        "state_topic": f"{T}/cell/countdown",
        "unit_of_measurement": "s",
        "device_class": "duration",
        "state_class": "measurement",
        "icon": "mdi:timer",
        "device": _DEVICE,
    }),
    # ---- Cell output ----
    ("number", "jarvis_pool_cell_output", {
        "name": "Cell Output",
        "unique_id": "jarvis_pool_cell_output",
        "command_topic": f"{T}/cell/output_percent/set",
        "state_topic": f"{T}/cell/output_percent",
        "min": 0, "max": 100, "step": 1,
        "unit_of_measurement": "%",
        "icon": "mdi:brightness-percent",
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
    # ---- Pump telemetry ----
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
        "state_topic": f"{T}/cell/current_amps",
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
        "state_topic": f"{T}/sensors/ec",
        "unit_of_measurement": "µS/cm",
        "icon": "mdi:water-percent",
        "state_class": "measurement",
        "device": _DEVICE,
    }),
    # ---- System health / online ----
    ("binary_sensor", "jarvis_pool_controller_online", {
        "name": "Pool Controller Online",
        "unique_id": "jarvis_pool_controller_online",
        "state_topic": f"{T}/lwt",
        "payload_on": "online",
        "payload_off": "offline",
        "device_class": "connectivity",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_system_cpu_percent", {
        "name": "Pool Controller CPU",
        "unique_id": "jarvis_pool_system_cpu_percent",
        "state_topic": f"{T}/system/cpu",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "icon": "mdi:cpu-64-bit",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_system_cpu_temp", {
        "name": "Pool Controller CPU Temperature",
        "unique_id": "jarvis_pool_system_cpu_temp",
        "state_topic": f"{T}/system/temp",
        "unit_of_measurement": "°F",
        "device_class": "temperature",
        "state_class": "measurement",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_system_memory_percent", {
        "name": "Pool Controller Memory",
        "unique_id": "jarvis_pool_system_memory_percent",
        "state_topic": f"{T}/system/memory",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "icon": "mdi:memory",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_system_disk_percent", {
        "name": "Pool Controller Disk",
        "unique_id": "jarvis_pool_system_disk_percent",
        "state_topic": f"{T}/system/disk",
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
        "state_topic": f"{T}/system/uptime",
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
    # ---- Polarity ----
    ("sensor", "jarvis_pool_cell_polarity", {
        "name": "Salt Cell Polarity",
        "unique_id": "jarvis_pool_cell_polarity",
        "state_topic": f"{T}/cell/polarity_direction",
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
        "state_topic": f"{T}/cell/polarity_accumulator",
        "unit_of_measurement": "s",
        "device_class": "duration",
        "state_class": "measurement",
        "icon": "mdi:timer",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_polarity_accumulated", {
        "name": "Salt Cell Polarity Accumulated",
        "unique_id": "jarvis_pool_polarity_accumulated",
        "state_topic": f"{T}/cell/polarity_accumulated_s",
        "icon": "mdi:timer-play",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_polarity_remaining", {
        "name": "Salt Cell Polarity Remaining",
        "unique_id": "jarvis_pool_polarity_remaining",
        "state_topic": f"{T}/cell/polarity_remaining_s",
        "icon": "mdi:timer-sand",
        "device": _DEVICE,
    }),
    # ---- Cell actual duty ----
    ("sensor", "jarvis_pool_cell_actual_duty", {
        "name": "Cell Actual Duty",
        "unique_id": "jarvis_pool_cell_actual_duty",
        "state_topic": f"{T}/cell/runtime_percent",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "icon": "mdi:gauge",
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_cell_actual_confidence", {
        "name": "Cell Actual Confidence",
        "unique_id": "jarvis_pool_cell_actual_confidence",
        "state_topic": f"{T}/cell/actual_duty_confidence",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "icon": "mdi:shield-check",
        "device": _DEVICE,
    }),
    # ---- Super Chlorinate ----
    ("switch", "jarvis_pool_super_chlorinate", {
        "name": "Super Chlorinate",
        "unique_id": "jarvis_pool_super_chlorinate",
        "command_topic": f"{T}/sc/set",
        "state_topic": f"{T}/sc/active",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:water-plus",
        "retain": True,
        "device": _DEVICE,
    }),
    ("sensor", "jarvis_pool_super_chlorinate_remaining", {
        "name": "Super Chlorinate Remaining",
        "unique_id": "jarvis_pool_super_chlorinate_remaining",
        "state_topic": f"{T}/sc/remaining",
        "unit_of_measurement": "s",
        "device_class": "duration",
        "state_class": "measurement",
        "icon": "mdi:timer-outline",
        "device": _DEVICE,
    }),
    # ---- Service mode ----
    ("switch", "jarvis_pool_service_mode", {
        "name":          "Service Mode",
        "unique_id":     "jarvis_pool_service_mode",
        "command_topic": f"{T}/service_mode/set",
        "state_topic":   f"{T}/service_mode",
        "payload_on":    "ON",
        "payload_off":   "OFF",
        "icon":          "mdi:wrench",
        "retain":        True,
        "device":        _DEVICE,
    }),
    ("sensor", "jarvis_pool_system_mode", {
        "name":      "Pool System Mode",
        "unique_id": "jarvis_pool_system_mode",
        "state_topic": f"{T}/system/mode",
        "icon":      "mdi:pool",
        "device":    _DEVICE,
    }),
    # ---- Fault state + reset ----
    ("sensor", "jarvis_pool_fault_state", {
        "name":      "Pool Fault State",
        "unique_id": "jarvis_pool_fault_state",
        "state_topic": f"{T}/fault/state",
        "icon":      "mdi:alert-circle",
        "device":    _DEVICE,
    }),
    ("button", "jarvis_pool_fault_reset", {
        "name":          "Pool Fault Reset",
        "unique_id":     "jarvis_pool_fault_reset",
        "command_topic": f"{T}/fault/reset",
        "payload_press": "reset",
        "icon":          "mdi:restart",
        "device":        _DEVICE,
    }),
    # ---- Notifications ----
    ("sensor", "jarvis_pool_notifications", {
        "name":      "Pool Notifications",
        "unique_id": "jarvis_pool_notifications",
        "state_topic": f"{T}/notifications",
        "icon":      "mdi:bell",
        "device":    _DEVICE,
    }),
]

# Command subtopics in the new namespace (used for subscribe + dispatch)
_CMD_SUBTOPICS = [
    "pump/set",
    "pump/output_percent/set",
    "cell/on/set",
    "cell/cmd/polarity",
    "sc/set",
    "cell/output_percent/set",
    "service_mode/set",
    "fault/reset",
]


class MQTTClient:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()
        self._connected = False

        self._on_speed_set: Optional[Callable[[int], None]] = None
        self._on_pump_power_set: Optional[Callable[[bool], None]] = None
        self._on_cell_set: Optional[Callable[[bool], None]] = None
        self._on_polarity_toggle: Optional[Callable[[], None]] = None
        self._on_super_chlorinate_set: Optional[Callable[[bool], None]] = None
        self._on_output_set: Optional[Callable[[int], None]] = None
        self._on_service_mode_set: Optional[Callable[[bool], None]] = None
        self._on_fault_reset: Optional[Callable[[], None]] = None

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="jarvis-pool",
        )
        self._client.will_set(f"{T}/lwt", "offline", qos=1, retain=True)
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
        for sub in _CMD_SUBTOPICS:
            client.subscribe(f"{T}/{sub}")
        if OLD_T:
            for sub in _CMD_SUBTOPICS:
                client.subscribe(f"{OLD_T}/{sub}")
            logger.info("Bridge subscriptions active on old prefix %s", OLD_T)
        client.publish(f"{T}/lwt", "online", qos=1, retain=True)
        if OLD_T:
            client.publish(f"{OLD_T}/lwt", "online", qos=1, retain=True)
        self._publish_discovery(client)

    def _cb_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected = False
        logger.warning("MQTT disconnected: %s (auto-reconnect active)", reason_code)

    def _cb_message(self, client, userdata, msg):
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
            self._client.publish(f"{T}/lwt", "offline", qos=1, retain=True)
            if OLD_T:
                self._client.publish(f"{OLD_T}/lwt", "offline", qos=1, retain=True)
        except Exception:
            pass
        self._client.loop_stop()
        self._client.disconnect()

    def publish(self, subtopic: str, value, retain: bool = False) -> None:
        """Publish to the canonical prefix; also to old prefix when bridge is enabled."""
        if not self._connected:
            return
        try:
            self._client.publish(f"{T}/{subtopic}", str(value), retain=retain)
            if OLD_T:
                self._client.publish(f"{OLD_T}/{subtopic}", str(value), retain=retain)
        except Exception as exc:
            logger.debug("publish error: %s", exc)

    def is_connected(self) -> bool:
        return self._connected

    def _topic_match(self, topic: str, subtopic: str) -> bool:
        """Return True if topic matches the subtopic under the primary or bridge prefix."""
        return topic == f"{T}/{subtopic}" or (bool(OLD_T) and topic == f"{OLD_T}/{subtopic}")

    def register_speed_handler(self, fn: Callable[[int], None]) -> None:
        self._on_speed_set = fn

    def register_pump_power_handler(self, fn: Callable[[bool], None]) -> None:
        self._on_pump_power_set = fn

    def register_cell_handler(self, fn: Callable[[bool], None]) -> None:
        self._on_cell_set = fn

    def register_polarity_toggle_handler(self, fn: Callable[[], None]) -> None:
        self._on_polarity_toggle = fn

    def register_super_chlorinate_handler(self, fn: Callable[[bool], None]) -> None:
        self._on_super_chlorinate_set = fn

    def register_output_handler(self, fn: Callable[[int], None]) -> None:
        self._on_output_set = fn

    def register_service_mode_handler(self, fn: Callable[[bool], None]) -> None:
        self._on_service_mode_set = fn

    def register_fault_reset_handler(self, fn: Callable[[], None]) -> None:
        self._on_fault_reset = fn

    async def message_loop(self) -> None:
        """Dispatch inbound commands.  Run as an asyncio task."""
        try:
            while True:
                msg = await self._queue.get()
                topic   = msg.topic
                payload = msg.payload.decode("utf-8", errors="replace").strip()
                logger.debug("MQTT ← %s: %s", topic, payload)

                if self._topic_match(topic, "pump/set"):
                    if self._on_pump_power_set:
                        self._on_pump_power_set(payload.upper() == "ON")

                elif self._topic_match(topic, "pump/output_percent/set"):
                    try:
                        speed = int(float(payload))
                        if self._on_speed_set:
                            self._on_speed_set(speed)
                    except ValueError:
                        logger.warning("Bad speed payload: %r", payload)

                elif self._topic_match(topic, "cell/on/set"):
                    if self._on_cell_set:
                        self._on_cell_set(payload.upper() == "ON")

                elif self._topic_match(topic, "cell/cmd/polarity"):
                    if self._on_polarity_toggle:
                        self._on_polarity_toggle()

                elif self._topic_match(topic, "sc/set"):
                    if self._on_super_chlorinate_set:
                        self._on_super_chlorinate_set(payload.upper() == "ON")

                elif self._topic_match(topic, "cell/output_percent/set"):
                    try:
                        pct = int(float(payload))
                        if self._on_output_set:
                            self._on_output_set(pct)
                    except ValueError:
                        logger.warning("Bad cell output payload: %r", payload)

                elif self._topic_match(topic, "service_mode/set"):
                    if self._on_service_mode_set:
                        self._on_service_mode_set(payload.upper() == "ON")

                elif self._topic_match(topic, "fault/reset"):
                    if self._on_fault_reset:
                        self._on_fault_reset()

        except asyncio.CancelledError:
            pass
