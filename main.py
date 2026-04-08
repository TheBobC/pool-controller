"""
main.py — Jarvis Pool Controller  (asyncio entry point)

Startup sequence:
  1. Load persisted state from state.json
  2. Init all hardware — non-fatal, service always starts
  3. Connect to MQTT broker
  4. Run four async loops:
       pump_keepalive   — sends EcoStar RS-485 packet every 500 ms
       safety_check     — evaluates cell interlocks every 1 s
       sensor_read      — reads all sensors, publishes every 30 s
       state_publish    — re-publishes retained topics every 60 s
     plus mqtt.message_loop() for inbound commands
  5. Clean shutdown on SIGTERM / SIGINT

All hardware failure modes are non-fatal:  missing /dev nodes, absent
I2C devices, and GPIO errors are caught and logged as warnings so the
service keeps running and MQTT stays connected.
"""

import asyncio
import logging
import signal

import config  # noqa: F401 — loads .env before any other import
import cell
import mqtt_client
import pump
import safety
import sensors
import state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pool")

# ---------------------------------------------------------------------------
# Shared mutable state (touched only from asyncio tasks → no locking needed)
# ---------------------------------------------------------------------------
_cell_requested: bool = False
_mqtt: mqtt_client.MQTTClient | None = None


# ---------------------------------------------------------------------------
# MQTT command handlers
# Called from paho's thread via call_soon_threadsafe — keep them short
# ---------------------------------------------------------------------------

def handle_speed_set(speed: int) -> None:
    logger.info("← speed/set: %d%%", speed)
    if speed == 0 and pump.get_speed() > 0:
        safety.reset_timer()
    pump.set_speed(speed)
    state.save({"pump_speed": speed})
    if _mqtt:
        _mqtt.publish("pump/speed",   speed,                        retain=True)
        _mqtt.publish("pump/running", "ON" if speed > 0 else "OFF", retain=True)


def handle_cell_set(on: bool) -> None:
    global _cell_requested
    logger.info("← cell/set: %s", "ON" if on else "OFF")
    _cell_requested = on
    state.save({"cell_on": on})


# ---------------------------------------------------------------------------
# Async task loops
# ---------------------------------------------------------------------------

async def pump_keepalive_loop(shutdown: asyncio.Event) -> None:
    """Send EcoStar RS-485 keep-alive every 500 ms.  Must not miss ticks."""
    while not shutdown.is_set():
        pump.send_keepalive()
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=config.PUMP_KEEPALIVE_S)
        except asyncio.TimeoutError:
            pass


async def safety_check_loop(shutdown: asyncio.Event) -> None:
    """Evaluate cell interlocks every 1 s."""
    while not shutdown.is_set():
        flow = sensors.read_flow()
        cell_on = safety.update(
            pump_speed=pump.get_speed(),
            flow_ok=flow,
            cell_requested=_cell_requested,
            set_cell_fn=cell.set_cell,
        )
        if _mqtt:
            _mqtt.publish("cell/state",     "ON" if cell_on else "OFF",              retain=True)
            _mqtt.publish("cell/interlock", "ON" if safety.is_interlock_ok() else "OFF")
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def sensor_read_loop(shutdown: asyncio.Event) -> None:
    """Read sensors and publish every 30 s.  EC probe blocks ~650 ms — run in executor."""
    loop = asyncio.get_running_loop()
    while not shutdown.is_set():
        water_t  = await loop.run_in_executor(None, sensors.read_water_temp)
        air_t    = await loop.run_in_executor(None, sensors.read_air_temp)
        current  = await loop.run_in_executor(None, sensors.read_current)
        ec       = await loop.run_in_executor(None, sensors.read_conductivity)
        flow     = sensors.read_flow()

        if _mqtt and _mqtt.is_connected():
            if water_t is not None:
                _mqtt.publish("sensors/water_temp",   round(water_t * 9 / 5 + 32, 1))
            if air_t is not None:
                _mqtt.publish("sensors/air_temp",     round(air_t * 9 / 5 + 32, 1))
            if current is not None:
                _mqtt.publish("sensors/current",      current)
            if ec is not None:
                _mqtt.publish("sensors/conductivity", ec)
            _mqtt.publish("sensors/flow", "ON" if flow else "OFF")

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            pass


async def state_publish_loop(shutdown: asyncio.Event) -> None:
    """Re-publish retained state every 60 s (handles HA restarts)."""
    while not shutdown.is_set():
        if _mqtt and _mqtt.is_connected():
            spd = pump.get_speed()
            _mqtt.publish("pump/speed",   spd,                         retain=True)
            _mqtt.publish("pump/running", "ON" if spd > 0 else "OFF",  retain=True)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    global _mqtt, _cell_requested

    # --- Restore persisted state ---
    saved = state.load()
    _cell_requested = bool(saved.get("cell_on", False))
    pump.set_speed(int(saved.get("pump_speed", 0)))

    # --- Hardware init (all non-fatal) ---
    pump.init()
    cell.init()
    sensors.init()

    # --- MQTT ---
    loop = asyncio.get_running_loop()
    _mqtt = mqtt_client.MQTTClient(loop)
    _mqtt.register_speed_handler(handle_speed_set)
    _mqtt.register_cell_handler(handle_cell_set)
    _mqtt.connect()

    # --- Shutdown signal ---
    shutdown = asyncio.Event()

    def _request_shutdown(sig_name: str) -> None:
        logger.info("Received %s — shutting down", sig_name)
        shutdown.set()

    loop.add_signal_handler(signal.SIGINT,  lambda: _request_shutdown("SIGINT"))
    loop.add_signal_handler(signal.SIGTERM, lambda: _request_shutdown("SIGTERM"))

    logger.info("Pool controller running  (pump=%d%%, cell_req=%s)",
                pump.get_speed(), _cell_requested)

    # --- Run all tasks concurrently ---
    tasks = [
        asyncio.create_task(pump_keepalive_loop(shutdown),  name="pump-keepalive"),
        asyncio.create_task(safety_check_loop(shutdown),    name="safety"),
        asyncio.create_task(sensor_read_loop(shutdown),     name="sensors"),
        asyncio.create_task(state_publish_loop(shutdown),   name="state-pub"),
        asyncio.create_task(_mqtt.message_loop(),            name="mqtt-rx"),
    ]

    await shutdown.wait()

    # --- Clean shutdown ---
    logger.info("Stopping tasks…")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    pump.close()
    cell.close()
    sensors.cleanup()
    _mqtt.disconnect()
    logger.info("Pool controller stopped")


if __name__ == "__main__":
    asyncio.run(main())
