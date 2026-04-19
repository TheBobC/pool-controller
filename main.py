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
import subprocess
import sys
import time

import psutil

import config  # noqa: F401 — loads .env before any other import
import log_setup

log_setup.setup()

import cell  # noqa: E402
import fans  # noqa: E402
import mqtt_client  # noqa: E402
import pump  # noqa: E402
import safety  # noqa: E402
import sensors  # noqa: E402
import state  # noqa: E402

logger = logging.getLogger("pool")

# ---------------------------------------------------------------------------
# Shared mutable state (touched only from asyncio tasks → no locking needed)
# ---------------------------------------------------------------------------
_cell_requested: bool = False
_mqtt: mqtt_client.MQTTClient | None = None
_main_loop: asyncio.AbstractEventLoop | None = None


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


async def _do_polarity_toggle() -> None:
    loop = asyncio.get_running_loop()
    # Blocks ~2 * POLARITY_SWITCH_DELAY_S — run in executor
    new_polarity = await loop.run_in_executor(None, cell.toggle_polarity)
    if _mqtt:
        _mqtt.publish("cell/polarity", new_polarity, retain=True)
        _mqtt.publish("cell/state",
                      "ON" if cell.get_cell_state() else "OFF", retain=True)


def handle_polarity_toggle() -> None:
    logger.info("← cell/cmd/polarity: toggle")
    if _main_loop is not None:
        asyncio.run_coroutine_threadsafe(_do_polarity_toggle(), _main_loop)


# ---------------------------------------------------------------------------
# System health helpers
# ---------------------------------------------------------------------------

def _read_cpu_temp() -> float | None:
    """Read SoC temperature from the thermal zone; return °F or None."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            millideg = int(fh.read().strip())
        return round(millideg / 1000.0 * 9 / 5 + 32, 1)
    except Exception:
        return None


def _read_wifi_rssi() -> int | None:
    """Return wlan0 RSSI in dBm via iwconfig, or None if unavailable."""
    try:
        out = subprocess.check_output(
            ["iwconfig", "wlan0"], stderr=subprocess.DEVNULL, timeout=2
        ).decode()
        for token in out.split():
            if token.startswith("level="):
                return int(token.split("=", 1)[1])
    except Exception:
        pass
    return None


def _collect_system_health() -> dict:
    """Gather all system health metrics.  Blocking — run in executor."""
    return {
        "cpu_percent":    round(psutil.cpu_percent(interval=1), 1),
        "memory_percent": round(psutil.virtual_memory().percent, 1),
        "disk_percent":   round(psutil.disk_usage("/").percent, 1),
        "uptime_seconds": int(time.time() - psutil.boot_time()),
        "cpu_temp":       _read_cpu_temp(),
        "wifi_signal":    _read_wifi_rssi(),
    }


# ---------------------------------------------------------------------------
# Async task loops
# ---------------------------------------------------------------------------

async def pump_keepalive_loop(shutdown: asyncio.Event) -> None:
    """Send EcoStar RS-485 keep-alive every 500 ms and publish pump telemetry.

    send_keepalive() blocks for up to 100 ms waiting for the pump response, so
    it runs in a thread-pool executor to keep the asyncio event loop responsive.
    """
    loop = asyncio.get_running_loop()
    while not shutdown.is_set():
        await loop.run_in_executor(None, pump.send_keepalive)
        if _mqtt and _mqtt.is_connected():
            telemetry = pump.get_telemetry()
            if telemetry.get("rpm") is not None:
                _mqtt.publish("pump/rpm",   telemetry["rpm"])
            if telemetry.get("watts") is not None:
                _mqtt.publish("pump/power", telemetry["watts"])
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

        air_t_f = round(air_t * 9 / 5 + 32, 1) if air_t is not None else None

        # Fan logic: on if cell is active OR enclosure air exceeds threshold
        fan_on = cell.get_cell_state() or (
            air_t_f is not None and air_t_f > config.FAN_TEMP_THRESHOLD
        )
        fans.set_fans(fan_on)

        logger.info(
            "sensors: water=%s air=%s flow=%s current=%s ec=%s fans=%s",
            f"{round(water_t * 9/5 + 32, 1)}°F" if water_t is not None else "n/a",
            f"{air_t_f}°F"                        if air_t_f is not None else "n/a",
            "ON" if flow else "OFF",
            f"{current:.3f}A"                     if current is not None else "n/a",
            f"{ec:.0f}µS/cm"                      if ec is not None else "n/a",
            "ON" if fan_on else "OFF",
        )

        if _mqtt and _mqtt.is_connected():
            if water_t is not None:
                _mqtt.publish("sensors/water_temp",   round(water_t * 9 / 5 + 32, 1))
            if air_t_f is not None:
                _mqtt.publish("sensors/air_temp",     air_t_f)
            if current is not None:
                _mqtt.publish("sensors/current",      current)
            if ec is not None:
                _mqtt.publish("sensors/conductivity", ec)
            _mqtt.publish("sensors/flow", "ON" if flow else "OFF")
            _mqtt.publish("fans/state",  "ON" if fan_on else "OFF", retain=True)

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            pass


async def acs712_power_on_task() -> None:
    """Wait 5 s after startup, then energize ACS712 Vcc via CH4."""
    await asyncio.sleep(5.0)
    cell.set_acs712_power(True)
    sensors.set_acs712_powered(True)
    logger.info("Energizing ACS712 power (CH4) — current sensing coming online.")


async def state_publish_loop(shutdown: asyncio.Event) -> None:
    """Re-publish retained state every 60 s (handles HA restarts)."""
    while not shutdown.is_set():
        if _mqtt and _mqtt.is_connected():
            spd = pump.get_speed()
            _mqtt.publish("pump/speed",   spd,                         retain=True)
            _mqtt.publish("pump/running", "ON" if spd > 0 else "OFF",  retain=True)
            _mqtt.publish("cell/polarity", cell.get_polarity(),         retain=True)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass


async def system_health_loop(shutdown: asyncio.Event) -> None:
    """Collect and publish system health metrics every 30 s.

    _collect_system_health() blocks for ~1 s (psutil cpu_percent interval),
    so it runs in a thread-pool executor to keep the event loop responsive.
    """
    loop = asyncio.get_running_loop()
    while not shutdown.is_set():
        health = await loop.run_in_executor(None, _collect_system_health)
        if _mqtt and _mqtt.is_connected():
            _mqtt.publish("system/cpu_percent",    health["cpu_percent"])
            _mqtt.publish("system/memory_percent", health["memory_percent"])
            _mqtt.publish("system/disk_percent",   health["disk_percent"])
            _mqtt.publish("system/uptime_seconds", health["uptime_seconds"])
            if health["cpu_temp"] is not None:
                _mqtt.publish("system/cpu_temp",   health["cpu_temp"])
            if health["wifi_signal"] is not None:
                _mqtt.publish("system/wifi_signal", health["wifi_signal"])
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=30.0)
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
    fans.init()
    sensors.init()

    # --- MQTT ---
    global _main_loop
    loop = asyncio.get_running_loop()
    _main_loop = loop
    _mqtt = mqtt_client.MQTTClient(loop)
    _mqtt.register_speed_handler(handle_speed_set)
    _mqtt.register_cell_handler(handle_cell_set)
    _mqtt.register_polarity_toggle_handler(handle_polarity_toggle)
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
        asyncio.create_task(pump_keepalive_loop(shutdown),   name="pump-keepalive"),
        asyncio.create_task(safety_check_loop(shutdown),     name="safety"),
        asyncio.create_task(sensor_read_loop(shutdown),      name="sensors"),
        asyncio.create_task(state_publish_loop(shutdown),    name="state-pub"),
        asyncio.create_task(system_health_loop(shutdown),    name="system-health"),
        asyncio.create_task(_mqtt.message_loop(),             name="mqtt-rx"),
        asyncio.create_task(acs712_power_on_task(),          name="acs712-power-on"),
    ]

    await shutdown.wait()

    # --- Clean shutdown ---
    logger.info("Stopping tasks…")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    sensors.set_acs712_powered(False)
    pump.close()
    fans.close()
    cell.close()
    sensors.cleanup()
    _mqtt.disconnect()
    logger.info("Pool controller stopped")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "st":
        import selftest
        selftest.run()
    else:
        asyncio.run(main())
