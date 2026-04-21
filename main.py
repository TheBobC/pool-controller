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
import json
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

# Polarity auto-reverse: accumulated gate-on time for the current polarity period.
# Persisted in state.json so a restart doesn't reset the clock.
_polarity_on_time_s: float = 0.0
_last_cell_on_tick: float | None = None   # monotonic timestamp of last tick cell was ON
_polarity_reversing: bool = False          # True while an auto-reverse is in flight

# Super chlorinate: forces cell on at 100% duty for 24 h.
# On restart, active state is preserved but cell does not auto-enable — user must
# explicitly send cell/set ON for super chlorinate to take effect.
_super_chlorinate_active: bool = False
_super_chlorinate_expires_at: float = 0.0  # unix timestamp

# Duty cycle: user-set output level 0–100 %.
# cell_duty_cycle_loop drives the gate; safety_check_loop sets _interlocks_ok.
_cell_output_percent: int = 0
_interlocks_ok: bool = False  # True when safety permits the gate to be energised

# Pump power: gates all speed commands.  Boots OFF — user must explicitly enable.
# When OFF, keepalive still runs but sends speed=0 so pump doesn't revert to panel.
_pump_power_on: bool = False


# ---------------------------------------------------------------------------
# MQTT command handlers
# Called from paho's thread via call_soon_threadsafe — keep them short
# ---------------------------------------------------------------------------

def handle_speed_set(speed: int) -> None:
    if not _pump_power_on:
        logger.debug("← speed/set: %d%% ignored — pump power is OFF", speed)
        return
    logger.info("← speed/set: %d%%", speed)
    if speed == 0 and pump.get_speed() > 0:
        safety.reset_timer()
    pump.request_speed(speed)
    state.save({"pump_speed": speed})
    if _mqtt:
        _mqtt.publish("pump/speed",   pump.get_target_speed(),               retain=True)
        _mqtt.publish("pump/running", "ON" if pump.get_speed() > 0 else "OFF", retain=True)


def handle_pump_power_set(on: bool) -> None:
    global _pump_power_on
    logger.info("← pump/power_on/set: %s", "ON" if on else "OFF")
    _pump_power_on = on
    state.save({"pump_power_on": on})
    if on:
        spd = state.get("pump_speed", 0)
        pump.request_speed(spd)   # triggers preload if spd > 0
        if _mqtt:
            _mqtt.publish("pump/power_on", "ON",                                   retain=True)
            _mqtt.publish("pump/speed",    pump.get_target_speed(),                 retain=True)
            _mqtt.publish("pump/running",  "ON" if pump.get_speed() > 0 else "OFF", retain=True)
    else:
        if pump.get_speed() > 0:
            safety.reset_timer()
        pump.request_speed(0)
        if _mqtt:
            _mqtt.publish("pump/power_on", "OFF", retain=True)
            _mqtt.publish("pump/speed",    0,     retain=True)
            _mqtt.publish("pump/running",  "OFF", retain=True)


def handle_cell_set(on: bool) -> None:
    global _cell_requested
    logger.info("← cell/set: %s", "ON" if on else "OFF")
    _cell_requested = on
    state.save({"cell_on": on})


def handle_cell_trip(reason: str, pump_speed: int, flow_ok: bool) -> None:
    logger.warning("Cell trip event: reason=%s pump_speed=%d flow=%s", reason, pump_speed, flow_ok)
    if _super_chlorinate_active:
        _cancel_super_chlorinate("safety trip")
    if _mqtt:
        _mqtt.publish("events/cell_trip", json.dumps({
            "reason": reason,
            "pump_speed": pump_speed,
            "flow_ok": flow_ok,
        }))


async def _do_polarity_toggle() -> None:
    loop = asyncio.get_running_loop()
    # Blocks ~2 * POLARITY_SWITCH_DELAY_S — run in executor
    new_polarity = await loop.run_in_executor(None, cell.toggle_polarity)
    if _mqtt:
        _mqtt.publish("cell/polarity", new_polarity, retain=True)
        _mqtt.publish("cell/state",
                      "ON" if cell.get_cell_state() else "OFF", retain=True)


async def _auto_polarity_reverse() -> None:
    """Triggered by safety_check_loop when accumulated on-time reaches threshold."""
    global _polarity_on_time_s, _polarity_reversing
    loop = asyncio.get_running_loop()
    old_polarity = cell.get_polarity()
    new_polarity = await loop.run_in_executor(None, cell.toggle_polarity)
    _polarity_on_time_s = 0.0
    state.save({"polarity_on_time_s": 0.0})
    _polarity_reversing = False
    logger.info(
        "Polarity auto-reverse after %.0f s accumulated on-time: %s → %s",
        config.CELL_POLARITY_REVERSE_INTERVAL_S, old_polarity, new_polarity,
    )
    if _mqtt:
        _mqtt.publish("cell/polarity", new_polarity, retain=True)
        _mqtt.publish("cell/state",
                      "ON" if cell.get_cell_state() else "OFF", retain=True)
        _mqtt.publish("cell/polarity_on_time_s", 0)


def handle_polarity_toggle() -> None:
    logger.info("← cell/cmd/polarity: toggle")
    if _main_loop is not None:
        asyncio.run_coroutine_threadsafe(_do_polarity_toggle(), _main_loop)


def _duty_gate(allow: bool) -> None:
    """set_cell_fn passed to safety.update() — immediately kills gate on False.

    When allow=False, kills the gate right away.  When True, cell_duty_cycle_loop
    takes over gate control.  Never calls cell.set_cell(True) directly.
    """
    global _interlocks_ok
    _interlocks_ok = allow
    if not allow:
        cell.set_cell(False)


def handle_output_set(pct: int) -> None:
    global _cell_output_percent
    pct = max(0, min(100, pct))
    logger.info("← cell/output/set: %d%%", pct)
    _cell_output_percent = pct
    state.save({"cell_output_percent": pct})
    if _mqtt:
        _mqtt.publish("cell/output", pct, retain=True)


def _publish_super_chlorinate_state() -> None:
    if not _mqtt:
        return
    _mqtt.publish("cell/super_chlorinate", "ON" if _super_chlorinate_active else "OFF", retain=True)
    remaining = max(0, int(_super_chlorinate_expires_at - time.time())) if _super_chlorinate_active else 0
    _mqtt.publish("cell/super_chlorinate_remaining_s", remaining, retain=True)


def _cancel_super_chlorinate(reason: str) -> None:
    global _super_chlorinate_active, _super_chlorinate_expires_at
    _super_chlorinate_active = False
    _super_chlorinate_expires_at = 0.0
    state.save({"super_chlorinate_active": False, "super_chlorinate_expires_at": 0.0})
    logger.info("Super chlorinate cleared: %s", reason)
    _publish_super_chlorinate_state()


def handle_super_chlorinate_set(on: bool) -> None:
    global _super_chlorinate_active, _super_chlorinate_expires_at, _cell_requested
    logger.info("← cell/super_chlorinate/set: %s", "ON" if on else "OFF")
    if on:
        _super_chlorinate_active = True
        _super_chlorinate_expires_at = time.time() + config.SUPER_CHLORINATE_DURATION_S
        _cell_requested = True
        state.save({
            "super_chlorinate_active": True,
            "super_chlorinate_expires_at": _super_chlorinate_expires_at,
            "cell_on": True,
        })
        logger.info("Super chlorinate activated — expires at %.0f (%.1f h)",
                    _super_chlorinate_expires_at, config.SUPER_CHLORINATE_DURATION_S / 3600)
    else:
        _cancel_super_chlorinate("cancelled by user")
    _publish_super_chlorinate_state()


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
            _mqtt.publish("pump/preload_active",    "ON" if pump.is_preloading() else "OFF")
            _mqtt.publish("pump/preload_remaining_s", pump.get_preload_remaining_s())
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=config.PUMP_KEEPALIVE_S)
        except asyncio.TimeoutError:
            pass


async def safety_check_loop(shutdown: asyncio.Event) -> None:
    """Evaluate cell interlocks every 1 s; track polarity on-time for auto-reverse.

    Passes _duty_gate as the set_cell_fn so safety kills the gate immediately on
    any trip while cell_duty_cycle_loop controls gate timing when conditions are met.
    Polarity on-time accumulates from the actual gate state (not the safety permit),
    so duty-cycle OFF periods don't inflate the polarity timer.
    """
    global _polarity_on_time_s, _last_cell_on_tick, _polarity_reversing
    while not shutdown.is_set():
        flow = sensors.read_flow()
        safety.update(
            pump_speed=pump.get_speed(),
            flow_ok=flow,
            cell_requested=_cell_requested,
            set_cell_fn=_duty_gate,
        )

        # Use actual hardware gate state; suppress during the ~6 s polarity switch
        actually_on = cell.get_cell_state()
        now = time.monotonic()
        if actually_on and not _polarity_reversing:
            if _last_cell_on_tick is not None:
                _polarity_on_time_s += now - _last_cell_on_tick
            _last_cell_on_tick = now
        else:
            _last_cell_on_tick = None

        # Trigger auto-reverse when threshold reached
        if (actually_on and not _polarity_reversing
                and _polarity_on_time_s >= config.CELL_POLARITY_REVERSE_INTERVAL_S):
            _polarity_reversing = True
            asyncio.create_task(_auto_polarity_reverse(), name="polarity-auto-reverse")

        if _mqtt:
            _mqtt.publish("cell/state",     "ON" if actually_on else "OFF",          retain=True)
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

        # Derive pump current from RS-485 watts (EcoStar exposes no current register)
        pump_watts = pump.get_telemetry().get("watts")
        pump_current = round(pump_watts / config.PUMP_VOLTAGE, 3) if pump_watts is not None else None

        logger.info(
            "sensors: water=%s air=%s flow=%s cell_current=%s pump_current=%s ec=%s fans=%s",
            f"{round(water_t * 9/5 + 32, 1)}°F" if water_t is not None else "n/a",
            f"{air_t_f}°F"                        if air_t_f is not None else "n/a",
            "ON" if flow else "OFF",
            f"{current:.3f}A"                     if current is not None else "n/a",
            f"{pump_current:.3f}A"                if pump_current is not None else "n/a",
            f"{ec:.0f}µS/cm"                      if ec is not None else "n/a",
            "ON" if fan_on else "OFF",
        )

        if _mqtt and _mqtt.is_connected():
            if water_t is not None:
                _mqtt.publish("sensors/water_temp",   round(water_t * 9 / 5 + 32, 1))
            if air_t_f is not None:
                _mqtt.publish("sensors/air_temp",     air_t_f)
            if current is not None:
                _mqtt.publish("sensors/cell_current", current)
            if pump_current is not None:
                _mqtt.publish("sensors/pump_current", pump_current)
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


async def cell_duty_cycle_loop(shutdown: asyncio.Event) -> None:
    """Drive cell gate ON/OFF within CELL_DUTY_WINDOW_S duty cycle windows.

    _interlocks_ok is set by _duty_gate (called from safety_check_loop every 1 s).
    When False, the gate is already off — this loop just waits.
    When True, this loop turns the gate on/off to honour _cell_output_percent.

    Mid-window output changes: recalculate remaining on-budget against already-used
    time; cut to OFF immediately if budget exhausted.

    Trip recovery: if the gate was continuously off for ≥ CELL_DUTY_WINDOW_S (trip
    duration), start a fresh window when conditions return.
    """
    TICK = 0.2  # s — poll interval

    window_start: float | None = None   # monotonic timestamp window began
    window_on_used: float = 0.0         # gate-on seconds used in current window
    gate_on_since: float | None = None  # monotonic timestamp gate last turned on
    last_went_off: float | None = None  # monotonic timestamp gate last turned off

    while not shutdown.is_set():
        now = time.monotonic()

        effective_pct = 100 if _super_chlorinate_active else _cell_output_percent

        if not _interlocks_ok:
            # Safety has already killed the gate.  Close out gate accumulator.
            if gate_on_since is not None:
                window_on_used += now - gate_on_since
                gate_on_since = None
            if last_went_off is None:
                last_went_off = now
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass
            continue

        # Conditions met + cell requested.

        # Determine if we need a fresh window:
        if window_start is None:
            # First time conditions are met
            window_start = now
            window_on_used = 0.0
        elif (last_went_off is not None
              and (now - last_went_off) >= config.CELL_DUTY_WINDOW_S):
            # Off for a full window (e.g. extended trip) — start fresh
            window_start = now
            window_on_used = 0.0
        elif (now - window_start) >= config.CELL_DUTY_WINDOW_S:
            # Natural window expiry — roll to next window
            window_start = now
            window_on_used = 0.0

        last_went_off = None

        # Advance on-time accumulator for gate-on duration since last tick
        if gate_on_since is not None:
            window_on_used += now - gate_on_since
            gate_on_since = now  # re-anchor to avoid double-counting

        on_budget = (effective_pct / 100.0) * config.CELL_DUTY_WINDOW_S

        # Decide target gate state for this tick
        if effective_pct == 0:
            target_on = False
        elif effective_pct == 100:
            target_on = True
        else:
            target_on = (window_on_used < on_budget)

        current = cell.get_cell_state()
        if target_on and not current:
            cell.set_cell(True)
            gate_on_since = now
        elif not target_on and current:
            cell.set_cell(False)
            gate_on_since = None
            last_went_off = now
        elif target_on and current and gate_on_since is None:
            gate_on_since = now  # gate already on (e.g. first tick after conditions met)

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=TICK)
        except asyncio.TimeoutError:
            pass


async def state_publish_loop(shutdown: asyncio.Event) -> None:
    """Re-publish retained state every 60 s (handles HA restarts); persist polarity timer."""
    while not shutdown.is_set():
        # Check super chlorinate expiry
        if _super_chlorinate_active and time.time() >= _super_chlorinate_expires_at:
            logger.info("Super chlorinate expired after %.1f h", config.SUPER_CHLORINATE_DURATION_S / 3600)
            _cancel_super_chlorinate("auto-expired")

        if _mqtt and _mqtt.is_connected():
            _mqtt.publish("pump/power_on", "ON" if _pump_power_on else "OFF",         retain=True)
            _mqtt.publish("pump/speed",    pump.get_target_speed(),                    retain=True)
            _mqtt.publish("pump/running",  "ON" if pump.get_speed() > 0 else "OFF",   retain=True)
            _mqtt.publish("pump/preload_active", "ON" if pump.is_preloading() else "OFF", retain=True)
            _mqtt.publish("cell/polarity", cell.get_polarity(),         retain=True)
            accumulated = round(_polarity_on_time_s)
            remaining = max(0, round(config.CELL_POLARITY_REVERSE_INTERVAL_S - _polarity_on_time_s))
            def _fmt_hm(s: int) -> str:
                h, m = divmod(s // 60, 60)
                return f"{h}:{m:02d}"
            _mqtt.publish("cell/polarity_on_time_s",    accumulated)
            _mqtt.publish("cell/polarity_accumulated_s", _fmt_hm(accumulated))
            _mqtt.publish("cell/polarity_remaining_s",   _fmt_hm(remaining))
            _mqtt.publish("cell/output", _cell_output_percent, retain=True)
            _publish_super_chlorinate_state()
        state.save({"polarity_on_time_s": round(_polarity_on_time_s, 1)})
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
    global _mqtt, _cell_requested, _polarity_on_time_s
    global _super_chlorinate_active, _super_chlorinate_expires_at
    global _cell_output_percent, _pump_power_on

    # --- Restore persisted state ---
    saved = state.load()
    _cell_requested = False   # never auto-enable cell on restart
    _pump_power_on  = False   # never auto-enable pump on restart
    pump.request_speed(0)     # keepalive sends 0 until user explicitly enables pump power
    _polarity_on_time_s = float(saved.get("polarity_on_time_s", 0.0))
    _super_chlorinate_active = bool(saved.get("super_chlorinate_active", False))
    _super_chlorinate_expires_at = float(saved.get("super_chlorinate_expires_at", 0.0))
    _cell_output_percent = int(saved.get("cell_output_percent", config.CELL_OUTPUT_DEFAULT))
    # Clear super chlorinate if it already expired while service was down
    if _super_chlorinate_active and time.time() >= _super_chlorinate_expires_at:
        _super_chlorinate_active = False
        _super_chlorinate_expires_at = 0.0
        state.save({"super_chlorinate_active": False, "super_chlorinate_expires_at": 0.0})
        logger.info("Super chlorinate expired while service was stopped — cleared")

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
    _mqtt.register_pump_power_handler(handle_pump_power_set)
    _mqtt.register_cell_handler(handle_cell_set)
    _mqtt.register_polarity_toggle_handler(handle_polarity_toggle)
    _mqtt.register_super_chlorinate_handler(handle_super_chlorinate_set)
    _mqtt.register_output_handler(handle_output_set)
    _mqtt.connect()
    safety.register_trip_handler(handle_cell_trip)

    # --- Shutdown signal ---
    shutdown = asyncio.Event()

    def _request_shutdown(sig_name: str) -> None:
        logger.info("Received %s — shutting down", sig_name)
        shutdown.set()

    loop.add_signal_handler(signal.SIGINT,  lambda: _request_shutdown("SIGINT"))
    loop.add_signal_handler(signal.SIGTERM, lambda: _request_shutdown("SIGTERM"))

    logger.info("Pool controller running  (pump_power=%s pump=%d%% cell_req=%s)",
                _pump_power_on, pump.get_speed(), _cell_requested)

    # --- Run all tasks concurrently ---
    tasks = [
        asyncio.create_task(pump_keepalive_loop(shutdown),   name="pump-keepalive"),
        asyncio.create_task(safety_check_loop(shutdown),     name="safety"),
        asyncio.create_task(cell_duty_cycle_loop(shutdown),  name="cell-duty"),
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
