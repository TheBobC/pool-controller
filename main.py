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
import datetime
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

import actual_duty  # noqa: E402
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

# Super chlorinate: forces cell on at 100% duty until remaining_s reaches zero.
# Countdown only ticks while the gate is physically energized (cell-on time, not wall clock).
# On restart, remaining time is restored from state.json; retained MQTT ON does not reset it.
_super_chlorinate_active: bool = False
_super_chlorinate_remaining_s: float = 0.0  # seconds of cell-on time remaining

# Duty cycle: user-set output level 0–100 %.
# cell_duty_cycle_loop drives the gate; safety_check_loop sets _interlocks_ok.
_cell_output_percent: int = 0
_interlocks_ok: bool = False  # True when safety permits the gate to be energised
_fault_state: str | None = None  # None = no fault; string = latched fault name (SPEC §7.1)

# Actual duty tracker: ACS712-based rolling 30-min measurement.
_actual_duty: actual_duty.ActualDutyTracker = actual_duty.ActualDutyTracker()

# Last successfully read cell current — shared between actual_duty_sample_loop (1 Hz)
# and fast_sensor_loop (15 s), avoiding a redundant ACS712 read.
_last_current_a: float | None = None

# Pump power: gates all speed commands.  Boots OFF — user must explicitly enable.
# When OFF, keepalive still runs but sends speed=0 so pump doesn't revert to panel.
_pump_power_on: bool = False

# Service mode: overrides schedule/SC for manual diagnostics; safety interlocks still active
_service_mode: bool = False
_service_mode_entered_at: float | None = None       # wall-clock time service mode was entered
_pre_service_mode_state: dict | None = None         # snapshot taken on service mode entry

# Power recovery: raw state.json snapshot captured before startup overrides
_startup_snapshot: dict = {}

# Boot grace (SPEC §1.6): hard lockout from service start.
# Pump commands rejected for 60s; cell/SC commands rejected for 150s.
_boot_start_time: float = time.monotonic()  # set once; never updated
_BOOT_PUMP_GRACE_S: float = 60.0
_BOOT_CELL_GRACE_S: float = 150.0


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
    state.save({"pump_output_percent": speed})
    if _mqtt:
        _mqtt.publish("pump/output_percent", pump.get_target_speed(),               retain=True)
        _mqtt.publish("pump/running",        "ON" if pump.get_speed() > 0 else "OFF", retain=True)


def handle_pump_power_set(on: bool) -> None:
    global _pump_power_on
    logger.info("← pump/power_on/set: %s", "ON" if on else "OFF")
    # SPEC §1.6: pump boot grace — reject ON commands for 60s after start
    if on:
        elapsed = time.monotonic() - _boot_start_time
        remaining = _BOOT_PUMP_GRACE_S - elapsed
        if remaining > 0:
            logger.warning("Pump enable rejected: boot grace (%ds remaining)", int(remaining))
            _publish_notification("error", f"Pump enable rejected: boot grace ({int(remaining)}s remaining)")
            if _mqtt:
                _mqtt.publish("pump/boot_grace_remaining_s", int(remaining))  # informational, not in discovery
            return
    _pump_power_on = on
    state.save({"pump_on": on})
    if on:
        spd = state.get("pump_output_percent", 0)
        pump.request_speed(spd)   # triggers preload if spd > 0
        if _mqtt:
            _mqtt.publish("pump/state",          "ON",                                    retain=True)
            _mqtt.publish("pump/output_percent", pump.get_target_speed(),                  retain=True)
            _mqtt.publish("pump/running",        "ON" if pump.get_speed() > 0 else "OFF",  retain=True)
    else:
        if pump.get_speed() > 0:
            safety.reset_timer()
        pump.request_speed(0)
        if _mqtt:
            _mqtt.publish("pump/state",          "OFF", retain=True)
            _mqtt.publish("pump/output_percent", 0,     retain=True)
            _mqtt.publish("pump/running",        "OFF", retain=True)


def _effective_cell_output() -> int:
    """Compute the published/displayed cell_output_percent per SPEC §3.7.

    If any trip condition is active → 0.
    If SC active → 100.
    Otherwise → stored _cell_output_percent (user/schedule set value).
    """
    if not _cell_requested or _fault_state is not None or not _interlocks_ok:
        return 0
    if _super_chlorinate_active:
        return 100
    return _cell_output_percent


def _publish_notification(severity: str, message: str) -> None:
    """Publish a human-readable notification to the notifications topic."""
    if _mqtt:
        _mqtt.publish("notifications", json.dumps({
            "severity": severity,
            "message":  message,
        }))


def handle_cell_set(on: bool) -> None:
    global _cell_requested
    logger.info("← cell/set: %s", "ON" if on else "OFF")
    # SPEC §1.7: cell boot grace — reject ON commands for 150s after start
    if on:
        elapsed = time.monotonic() - _boot_start_time
        remaining = _BOOT_CELL_GRACE_S - elapsed
        if remaining > 0:
            logger.warning("Cell enable rejected: boot grace (%ds remaining)", int(remaining))
            if _mqtt:
                _mqtt.publish("cell/cant_enable_reason", f"boot_grace:{int(remaining)}s", retain=True)
                _mqtt.publish("cell/countdown", int(remaining))
            _publish_notification("error", f"Cell enable rejected: boot grace ({int(remaining)}s remaining)")
            return
    if on and _fault_state is not None:
        logger.warning("Cell enable refused: fault latched (%s) — reset fault first", _fault_state)
        if _mqtt:
            _mqtt.publish("cell/cant_enable_reason", f"fault_latched:{_fault_state}", retain=True)
        _publish_notification("error", f"Cell enable rejected: fault latched ({_fault_state}) — reset fault first")
        return
    # SPEC §3.4 rejection gates
    if on:
        flow = sensors.read_flow()
        if not flow:
            logger.warning("Cell enable refused: no flow detected")
            if _mqtt:
                _mqtt.publish("cell/cant_enable_reason", "no_flow", retain=True)
            _publish_notification("error", "Cell enable rejected: no flow detected")
            return
        if not pump.is_stable():
            countdown = pump.get_stable_countdown_s()
            logger.warning("Cell enable refused: pump not stable (countdown=%ds)", countdown)
            if _mqtt:
                _mqtt.publish("cell/cant_enable_reason", f"pump_not_stable:{countdown}s", retain=True)
            _publish_notification("error", f"Cell enable rejected: pump not stable ({countdown}s remaining)")
            return
    if on and _cell_output_percent == 0:
        if _super_chlorinate_active:
            handle_output_set(100)
        else:
            logger.warning("Cell enable refused: output_percent is 0 — send cell/output/set first")
            if _mqtt:
                _mqtt.publish("cell/cant_enable_reason", "output_percent_not_set", retain=True)
            return
    if on and not _cell_requested:
        _actual_duty.reset()
    if on and _mqtt:
        _mqtt.publish("cell/cant_enable_reason", "", retain=True)
    # Service mode pre-flight: pump must be ≥ SERVICE_CELL_MIN_PUMP_SPEED before cell can run
    if on and _service_mode and pump.get_speed() < config.SERVICE_CELL_MIN_PUMP_SPEED:
        logger.info(
            "Service mode cell pre-flight: pump %d%% < %d%% — boosting to 100%%",
            pump.get_speed(), config.SERVICE_CELL_MIN_PUMP_SPEED,
        )
        if not _pump_power_on:
            handle_pump_power_set(True)
        pump.request_speed(100)
        if _mqtt:
            _mqtt.publish("pump/output_percent", pump.get_target_speed(),               retain=True)
            _mqtt.publish("pump/running",        "ON" if pump.get_speed() > 0 else "OFF", retain=True)
    _cell_requested = on
    if not on:
        _actual_duty.notify_cell_off()
    state.save({"cell_on": on})
    if _mqtt:
        _mqtt.publish("cell/on",           "ON" if _cell_requested else "OFF", retain=True)
        _mqtt.publish("cell/output_percent", _effective_cell_output(),          retain=True)


def handle_cell_trip(reason: str, pump_speed: int, flow_ok: bool) -> None:
    global _fault_state, _cell_requested, _cell_output_percent
    logger.warning(
        "Cell trip event: reason=%s pump_speed=%d flow=%s — latching fault",
        reason, pump_speed, flow_ok,
    )
    # SPEC §7.1: faults latch.  §7.2: gate de-energized (safety already did it),
    # cell_on forced False, cell_output_percent forced 0.  §7.11: individual topic.
    _fault_state = reason
    _cell_requested = False
    _cell_output_percent = 0
    _actual_duty.notify_cell_off()
    if _super_chlorinate_active:
        _cancel_super_chlorinate("safety trip")
    state.save({"fault_state": reason, "cell_on": False, "cell_output_percent": 0})
    _publish_notification("critical", f"Cell safety trip: {reason}")
    if _mqtt:
        _mqtt.publish("fault/state",      reason,                          retain=True)
        _mqtt.publish("cell/on",          "OFF",                           retain=True)
        _mqtt.publish("cell/output_percent", 0,                            retain=True)
        _mqtt.publish("events/cell_trip", json.dumps({
            "reason":     reason,
            "pump_speed": pump_speed,
            "flow_ok":    flow_ok,
        }))


def handle_fault_reset() -> None:
    global _fault_state
    if _fault_state is None:
        logger.info("← fault/reset: no fault latched — ignored")
        return
    logger.info("← fault/reset: clearing latched fault '%s'", _fault_state)
    _fault_state = None
    state.save({"fault_state": None})
    if _mqtt:
        _mqtt.publish("fault/state", "none", retain=True)
        _mqtt.publish("cell/cant_enable_reason", "", retain=True)
    logger.info("Fault cleared — user must re-enable cell manually (SPEC §7.10)")


async def _do_polarity_toggle() -> None:
    loop = asyncio.get_running_loop()
    # Blocks 2 * POLARITY_SWITCH_DELAY_S (10s each = 20s total per SPEC §7.6)
    _verify_mismatch: list = []

    def _pre_regate(new_pol: str) -> bool:
        """SPEC §7.6 step 4: verify AIN0 before gate re-energization."""
        if not (config.POLARITY_FORWARD_MAX_V > 0 and config.POLARITY_REVERSE_MIN_V > 0):
            return True  # thresholds not calibrated — skip, allow gate-on
        pol_v = sensors.read_polarity_voltage()
        if pol_v is None:
            return True  # ADS1115 unavailable — skip, allow gate-on
        mismatch = (
            (new_pol == "forward" and pol_v > config.POLARITY_FORWARD_MAX_V)
            or (new_pol == "reverse" and pol_v < config.POLARITY_REVERSE_MIN_V)
        )
        if mismatch:
            _verify_mismatch.append((new_pol, pol_v))
        return not mismatch

    new_polarity = await loop.run_in_executor(
        None, lambda: cell.toggle_polarity(pre_regate_fn=_pre_regate)
    )
    state.save({"polarity_direction": new_polarity})
    if _verify_mismatch:
        new_pol, pol_v = _verify_mismatch[0]
        logger.error(
            "Post-switch polarity mismatch: expected=%s AIN0=%.3fV — latching fault",
            new_pol, pol_v,
        )
        handle_cell_trip("polarity_mismatch", pump.get_speed(), bool(sensors.read_flow()))
    if _mqtt:
        _mqtt.publish("cell/polarity_direction", new_polarity,                                retain=True)
        _mqtt.publish("cell/gate_state",         "ON" if cell.get_cell_state() else "OFF",    retain=True)
        _mqtt.publish("cell/on",                 "ON" if _cell_requested else "OFF",          retain=True)


async def _auto_polarity_reverse() -> None:
    """Triggered by safety_check_loop when accumulated on-time reaches threshold."""
    global _polarity_on_time_s, _polarity_reversing
    loop = asyncio.get_running_loop()
    old_polarity = cell.get_polarity()
    _verify_mismatch: list = []

    def _pre_regate(new_pol: str) -> bool:
        """SPEC §7.6 step 4: verify AIN0 before gate re-energization."""
        if not (config.POLARITY_FORWARD_MAX_V > 0 and config.POLARITY_REVERSE_MIN_V > 0):
            return True
        pol_v = sensors.read_polarity_voltage()
        if pol_v is None:
            return True
        mismatch = (
            (new_pol == "forward" and pol_v > config.POLARITY_FORWARD_MAX_V)
            or (new_pol == "reverse" and pol_v < config.POLARITY_REVERSE_MIN_V)
        )
        if mismatch:
            _verify_mismatch.append((new_pol, pol_v))
        return not mismatch

    new_polarity = await loop.run_in_executor(
        None, lambda: cell.toggle_polarity(pre_regate_fn=_pre_regate)
    )
    _polarity_on_time_s = 0.0
    state.save({"polarity_on_time_accumulator": 0.0, "polarity_direction": new_polarity})
    _polarity_reversing = False
    if _verify_mismatch:
        new_pol, pol_v = _verify_mismatch[0]
        logger.error(
            "Auto-reverse polarity mismatch: expected=%s AIN0=%.3fV — latching fault",
            new_pol, pol_v,
        )
        handle_cell_trip("polarity_mismatch", pump.get_speed(), bool(sensors.read_flow()))
    logger.info(
        "Polarity auto-reverse after %.0f s accumulated on-time: %s → %s",
        config.CELL_POLARITY_REVERSE_INTERVAL_S, old_polarity, new_polarity,
    )
    if _mqtt:
        _mqtt.publish("cell/polarity_direction",  new_polarity,                              retain=True)
        _mqtt.publish("cell/gate_state",          "ON" if cell.get_cell_state() else "OFF",  retain=True)
        _mqtt.publish("cell/on",                  "ON" if _cell_requested else "OFF",        retain=True)
        _mqtt.publish("cell/polarity_accumulator", 0)


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
    # SPEC §7.1: latched fault blocks re-energization
    if allow and _fault_state is not None:
        allow = False
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
        _mqtt.publish("cell/output_percent", _effective_cell_output(), retain=True)


def _publish_super_chlorinate_state() -> None:
    if not _mqtt:
        return
    _mqtt.publish("sc/active",    "ON" if _super_chlorinate_active else "OFF", retain=True)
    remaining = max(0, int(_super_chlorinate_remaining_s)) if _super_chlorinate_active else 0
    _mqtt.publish("sc/remaining", remaining, retain=True)


def _publish_system_mode() -> None:
    """Publish system/mode and system/service_mode retained topics."""
    if not _mqtt:
        return
    if _service_mode:
        mode = "service"
    elif _pump_power_on:
        mode = "on"
    else:
        mode = "off"
    _mqtt.publish("system/mode",  mode,                               retain=True)
    _mqtt.publish("service_mode", "ON" if _service_mode else "OFF",  retain=True)


def _publish_power_recovery(resumed_mode: str, outage_s: int) -> None:
    """Publish a retained power_recovery event for HA logging."""
    if not _mqtt:
        return
    payload = json.dumps({
        "outage_duration_s": outage_s,
        "resumed_mode":      resumed_mode,
        "timestamp":         datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    _mqtt.publish("system/power_recovery", payload, retain=True)
    logger.info("Power recovery event: mode=%s outage=%ds", resumed_mode, outage_s)


def _cancel_super_chlorinate(reason: str) -> None:
    global _super_chlorinate_active, _super_chlorinate_remaining_s
    _super_chlorinate_active = False
    _super_chlorinate_remaining_s = 0.0
    state.save({"super_chlorinate_active": False, "super_chlorinate_remaining": 0.0})
    logger.info("Super chlorinate cleared: %s", reason)
    _publish_super_chlorinate_state()
    if _mqtt:
        _mqtt.publish("cell/output_percent", _effective_cell_output(), retain=True)


def handle_super_chlorinate_set(on: bool) -> None:
    global _super_chlorinate_active, _super_chlorinate_remaining_s, _cell_requested
    logger.info("← cell/super_chlorinate/set: %s", "ON" if on else "OFF")
    # SPEC §1.7: cell boot grace applies to SC as well
    if on:
        elapsed = time.monotonic() - _boot_start_time
        remaining = _BOOT_CELL_GRACE_S - elapsed
        if remaining > 0:
            logger.warning("SC rejected: boot grace (%ds remaining)", int(remaining))
            if _mqtt:
                _mqtt.publish("cell/cant_enable_reason", f"boot_grace:{int(remaining)}s", retain=True)
                _mqtt.publish("cell/countdown", int(remaining))
            _publish_notification("error", f"Super Chlorinate rejected: boot grace ({int(remaining)}s remaining)")
            return
    if on and _fault_state is not None:
        logger.warning("SC refused: fault latched (%s) — reset fault first", _fault_state)
        if _mqtt:
            _mqtt.publish("cell/cant_enable_reason", f"fault_latched:{_fault_state}", retain=True)
        _publish_notification("error", f"Super Chlorinate rejected: fault latched ({_fault_state}) — reset fault first")
        return
    # SPEC §4.8 rejection gates (same as §3.4)
    if on:
        flow = sensors.read_flow()
        if not flow:
            logger.warning("SC refused: no flow detected")
            if _mqtt:
                _mqtt.publish("cell/cant_enable_reason", "no_flow", retain=True)
            _publish_notification("error", "Super Chlorinate rejected: no flow detected")
            return
        if not pump.is_stable():
            countdown = pump.get_stable_countdown_s()
            logger.warning("SC refused: pump not stable (countdown=%ds)", countdown)
            if _mqtt:
                _mqtt.publish("cell/cant_enable_reason", f"pump_not_stable:{countdown}s", retain=True)
            _publish_notification("error", f"Super Chlorinate rejected: pump not stable ({countdown}s remaining)")
            return
    if on:
        is_fresh = not _super_chlorinate_active
        if is_fresh:
            _super_chlorinate_remaining_s = float(config.SUPER_CHLORINATE_DURATION_S)
        _super_chlorinate_active = True
        _cell_requested = True
        state.save({
            "super_chlorinate_active": True,
            "super_chlorinate_remaining": _super_chlorinate_remaining_s,
            "cell_on": True,
        })
        logger.info("Super chlorinate %s — %.2f h cell-on time remaining",
                    "activated" if is_fresh else "resumed",
                    _super_chlorinate_remaining_s / 3600)
    else:
        _cancel_super_chlorinate("cancelled by user")
    _publish_super_chlorinate_state()


def handle_service_mode_set(on: bool) -> None:
    global _service_mode, _service_mode_entered_at, _pre_service_mode_state
    logger.info("← system/service_mode/set: %s", "ON" if on else "OFF")
    if on and not _service_mode:
        _pre_service_mode_state = {
            "super_chlorinate_active":    _super_chlorinate_active,
            "super_chlorinate_remaining_s": _super_chlorinate_remaining_s,
            "cell_requested":             _cell_requested,
            "pump_power_on":              _pump_power_on,
            "cell_output_percent":        _cell_output_percent,
            "pump_speed":                 pump.get_target_speed(),
        }
        _service_mode = True
        _service_mode_entered_at = time.time()
        logger.info("Service mode entered; pre-state: %s", _pre_service_mode_state)
    elif not on and _service_mode:
        _service_mode = False
        _service_mode_entered_at = None
        _pre_service_mode_state = None
        logger.info("Service mode exited")
    state.save({
        "service_mode":             _service_mode,
        "service_mode_entered_at":  _service_mode_entered_at,
        "pre_service_mode_state":   _pre_service_mode_state,
    })
    _publish_system_mode()


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
    _last_stable: bool | None = None
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
            # Publish pump_stable on change (SPEC §2.11)
            stable = pump.is_stable()
            if stable != _last_stable:
                _mqtt.publish("pump/stable",    "ON" if stable else "OFF",      retain=True)
                _mqtt.publish("pump/countdown", pump.get_stable_countdown_s())
                _last_stable = stable
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
    global _super_chlorinate_remaining_s
    loop = asyncio.get_running_loop()
    _polarity_persist_ticks: int = 0  # count 1s ticks; write accumulator every 10s when gate on
    _last_gate_state: bool | None = None  # track on-change publish for cell/gate_state
    while not shutdown.is_set():
        flow_raw = sensors.read_flow()  # None = sensor error (SPEC §7.3)

        # Flow sensor failure fault: gate energized + sensor read error
        actually_on = cell.get_cell_state()
        if flow_raw is None and actually_on and _fault_state is None:
            logger.error("Flow sensor read failure while gate energized — latching fault")
            handle_cell_trip("flow_sensor_failure", pump.get_speed(), False)

        flow_ok = bool(flow_raw) if flow_raw is not None else False
        safety.update(
            pump_speed=pump.get_speed(),
            flow_ok=flow_ok,
            cell_requested=_cell_requested,
            set_cell_fn=_duty_gate,
        )

        # Polarity mismatch fault (SPEC §7.3) — enabled only when thresholds are configured
        actually_on = cell.get_cell_state()
        if (actually_on and _fault_state is None
                and config.POLARITY_FORWARD_MAX_V > 0 and config.POLARITY_REVERSE_MIN_V > 0):
            pol_v = sensors.read_polarity_voltage()
            if pol_v is not None:
                direction = cell.get_polarity()
                mismatch = (
                    (direction == "forward" and pol_v > config.POLARITY_FORWARD_MAX_V)
                    or (direction == "reverse" and pol_v < config.POLARITY_REVERSE_MIN_V)
                )
                if mismatch:
                    logger.error(
                        "Polarity mismatch: commanded=%s AIN0=%.3fV — latching fault",
                        direction, pol_v,
                    )
                    handle_cell_trip("polarity_mismatch", pump.get_speed(), flow_ok)

        now = time.monotonic()
        if actually_on and not _polarity_reversing:
            if _last_cell_on_tick is not None:
                delta = now - _last_cell_on_tick
                _polarity_on_time_s += delta
                if _super_chlorinate_active:
                    _super_chlorinate_remaining_s -= delta
                    if _super_chlorinate_remaining_s <= 0:
                        _super_chlorinate_remaining_s = 0.0
                        _cancel_super_chlorinate("auto-expired")
            _last_cell_on_tick = now
        else:
            _last_cell_on_tick = None

        # Trigger auto-reverse when threshold reached
        if (actually_on and not _polarity_reversing
                and _polarity_on_time_s >= config.CELL_POLARITY_REVERSE_INTERVAL_S):
            _polarity_reversing = True
            asyncio.create_task(_auto_polarity_reverse(), name="polarity-auto-reverse")

        # Write polarity accumulator every 10s while gate energized (SPEC §9.3)
        if actually_on:
            _polarity_persist_ticks += 1
            if _polarity_persist_ticks >= 10:
                state.save({"polarity_on_time_accumulator": round(_polarity_on_time_s, 1)})
                _polarity_persist_ticks = 0
        else:
            _polarity_persist_ticks = 0

        if _mqtt:
            # cell/gate_state: on-change only (SPEC §10.4); state_publish_loop handles 60s re-pub
            if actually_on != _last_gate_state:
                _mqtt.publish("cell/gate_state", "ON" if actually_on else "OFF", retain=True)
                _last_gate_state = actually_on
            _mqtt.publish("cell/interlock",  "ON" if safety.is_interlock_ok() else "OFF")
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
        try:
            current = await loop.run_in_executor(None, sensors.read_current)
        except sensors.SensorReadError:
            current = None  # actual_duty_sample_loop owns the retry/fault logic
        ec       = await loop.run_in_executor(None, sensors.read_conductivity)
        flow_raw = sensors.read_flow()
        flow     = bool(flow_raw) if flow_raw is not None else False

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
            "ON" if flow else ("ERR" if flow_raw is None else "OFF"),
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
            if pump_current is not None:
                _mqtt.publish("sensors/pump_current", pump_current)
            if ec is not None:
                _mqtt.publish("sensors/ec",           ec)
            _mqtt.publish("sensors/flow", "ON" if flow else "OFF")
            _mqtt.publish("fans/state",  "ON" if fan_on else "OFF", retain=True)

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=60.0)
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

        effective_pct = _effective_cell_output()  # SPEC §3.7 single source of truth

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


async def actual_duty_sample_loop(shutdown: asyncio.Event) -> None:
    """Sample ACS712 current at 1 Hz; check overcurrent; feed actual duty tracker.
    Updates _last_current_a so fast_sensor_loop can publish without re-reading hardware."""
    loop = asyncio.get_running_loop()
    _acs712_fail_count: int = 0
    _acs712_last_fail: float | None = None
    while not shutdown.is_set():
        try:
            current = await loop.run_in_executor(None, sensors.read_current)
        except sensors.SensorReadError as exc:
            # ADS1115 read failure — auto-retry per SPEC §7.4
            now = time.monotonic()
            if _acs712_last_fail is None or (now - _acs712_last_fail) >= 30.0:
                _acs712_fail_count += 1
                _acs712_last_fail = now
                logger.warning("ACS712/ADS1115 read failure (%s) — retry %d/3 in 30s", exc, _acs712_fail_count)
                if _acs712_fail_count >= 3:
                    logger.error("ACS712/ADS1115 failed 3 retries — latching critical fault")
                    _publish_notification("critical", "ACS712/ADS1115 failed after 3 retries — cell disabled")
                    handle_cell_trip("acs712_failure", pump.get_speed(), bool(sensors.read_flow()))
                    _acs712_fail_count = 0
                    _acs712_last_fail = None
        else:
            if _acs712_fail_count > 0:
                logger.info("ACS712 recovered after %d retries", _acs712_fail_count)
                _publish_notification("warning", f"Transient sensor fault — ACS712 recovered (retry {_acs712_fail_count})")
                _acs712_fail_count = 0
                _acs712_last_fail = None
            if current is not None:
                global _last_current_a
                _last_current_a = current
                _actual_duty.sample(current)
                # Overcurrent fault (SPEC §7.3): >9A while gate energized
                if cell.get_cell_state() and current > config.CELL_OVERCURRENT_A and _fault_state is None:
                    logger.error("Overcurrent: %.3fA > %.1fA — latching fault", current, config.CELL_OVERCURRENT_A)
                    handle_cell_trip("overcurrent", pump.get_speed(), bool(sensors.read_flow()))
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def state_publish_loop(shutdown: asyncio.Event) -> None:
    """Re-publish retained state every 60 s (handles HA restarts); persist polarity timer."""
    _corrupt_notified = False
    while not shutdown.is_set():
        # Safety-net expiry check (primary check is in safety_check_loop at 1 Hz)
        if _super_chlorinate_active and _super_chlorinate_remaining_s <= 0:
            _cancel_super_chlorinate("auto-expired")

        # One-shot: notify HA if state.json was corrupt on startup (SPEC §9.5)
        if not _corrupt_notified and _mqtt and _mqtt.is_connected() and state.was_load_failed():
            _publish_notification("critical",
                "state.json missing or corrupt — loaded defaults, awaiting HA schedule")
            _corrupt_notified = True

        if _mqtt and _mqtt.is_connected():
            _mqtt.publish("pump/state",          "ON" if _pump_power_on else "OFF",         retain=True)
            _mqtt.publish("pump/output_percent", pump.get_target_speed(),                    retain=True)
            _mqtt.publish("pump/running",        "ON" if pump.get_speed() > 0 else "OFF",   retain=True)
            _mqtt.publish("pump/preload_active", "ON" if pump.is_preloading() else "OFF",   retain=True)
            _mqtt.publish("cell/on",             "ON" if _cell_requested else "OFF",        retain=True)
            _mqtt.publish("cell/gate_state",     "ON" if cell.get_cell_state() else "OFF",  retain=True)
            _mqtt.publish("cell/polarity_direction", cell.get_polarity(),                   retain=True)
            accumulated = round(_polarity_on_time_s)
            remaining = max(0, round(config.CELL_POLARITY_REVERSE_INTERVAL_S - _polarity_on_time_s))
            def _fmt_hm(s: int) -> str:
                h, m = divmod(s // 60, 60)
                return f"{h}:{m:02d}"
            _mqtt.publish("cell/polarity_accumulator",  accumulated)
            _mqtt.publish("cell/polarity_accumulated_s", _fmt_hm(accumulated))
            _mqtt.publish("cell/polarity_remaining_s",   _fmt_hm(remaining))
            _mqtt.publish("cell/output_percent",     _effective_cell_output(),              retain=True)
            _mqtt.publish("cell/runtime_percent",    _actual_duty.actual_duty_pct(),        retain=True)
            _mqtt.publish("cell/actual_duty_confidence", _actual_duty.confidence_pct(),     retain=True)
            _publish_super_chlorinate_state()
            _mqtt.publish("service_mode", "ON" if _service_mode else "OFF", retain=True)
            _mqtt.publish("system/mode",
                          "service" if _service_mode else ("on" if _pump_power_on else "off"),
                          retain=True)
            # Boot grace countdowns (SPEC §1.8, §1.9)
            elapsed = time.monotonic() - _boot_start_time
            pump_grace = max(0, int(_BOOT_PUMP_GRACE_S - elapsed))
            cell_grace = max(0, int(_BOOT_CELL_GRACE_S - elapsed))
            _mqtt.publish("pump/boot_grace_remaining_s", pump_grace)
            _mqtt.publish("cell/countdown",              cell_grace)
            _mqtt.publish("pump/countdown",              pump.get_stable_countdown_s())
        state.save({
            "polarity_on_time_accumulator": round(_polarity_on_time_s, 1),
            "super_chlorinate_remaining": round(_super_chlorinate_remaining_s, 1),
        })
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
            _mqtt.publish("system/cpu",    health["cpu_percent"])
            _mqtt.publish("system/memory", health["memory_percent"])
            _mqtt.publish("system/disk",   health["disk_percent"])
            _mqtt.publish("system/uptime", health["uptime_seconds"])
            if health["cpu_temp"] is not None:
                _mqtt.publish("system/temp",        health["cpu_temp"])
            if health["wifi_signal"] is not None:
                _mqtt.publish("system/wifi_signal", health["wifi_signal"])
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass


async def fast_sensor_loop(shutdown: asyncio.Event) -> None:
    """Publish 15-second group: cell/current_amps, cell/gate_state, cell/polarity_direction (SPEC §10.4)."""
    while not shutdown.is_set():
        if _mqtt and _mqtt.is_connected():
            if _last_current_a is not None:
                _mqtt.publish("cell/current_amps", _last_current_a)
            _mqtt.publish("cell/gate_state",         "ON" if cell.get_cell_state() else "OFF", retain=True)
            _mqtt.publish("cell/polarity_direction", cell.get_polarity(),                       retain=True)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Power recovery
# ---------------------------------------------------------------------------

async def power_recovery_task(shutdown: asyncio.Event) -> None:
    """Wait until boot grace expires, then auto-resume pre-restart state.

    Waits for the cell grace period (_BOOT_CELL_GRACE_S=150s) so auto-resume
    doesn't fire inside the command lockout window.  Reads _startup_snapshot (the
    raw state.json values captured before startup overrides) to determine what
    to resume.
    """
    global _pump_power_on, _cell_requested
    # Wait for the full cell grace window (150s) so auto-resume doesn't race with the lockout
    grace_wait = _BOOT_CELL_GRACE_S
    logger.info("Power recovery: waiting %.0fs (cell boot grace) before auto-resume", grace_wait)
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=grace_wait)
        logger.info("Power recovery: shutdown during grace period — skipping resume")
        return
    except asyncio.TimeoutError:
        pass

    if not _mqtt or not _mqtt.is_connected():
        logger.warning("Power recovery: MQTT not connected after grace period — skipping auto-resume")
        return

    # Compute outage duration from last successful state write
    last_write = state.get("last_state_write")
    outage_s = 0
    if last_write:
        try:
            last_dt = datetime.datetime.fromisoformat(last_write)
            now_dt  = datetime.datetime.now(datetime.timezone.utc)
            outage_s = max(0, int((now_dt - last_dt).total_seconds()))
        except Exception:
            pass

    logger.info("Power recovery: grace elapsed (outage ~%ds) — evaluating resume", outage_s)

    if _service_mode:
        logger.info("Power recovery: service mode persisted — no auto-resume")
        _publish_power_recovery("service_mode", outage_s)
        return

    snap = _startup_snapshot

    if snap.get("super_chlorinate_active") and _super_chlorinate_remaining_s > 0:
        # SC was active and still has cell-on time remaining
        saved_speed  = snap.get("pump_output_percent", snap.get("pump_speed", 0))
        resume_speed = max(saved_speed, config.SC_PUMP_SPEED_DEFAULT)
        logger.info(
            "Power recovery: resuming SC (%.0fs remaining, pump→%d%%)",
            _super_chlorinate_remaining_s, resume_speed,
        )
        pump.request_speed(resume_speed)
        _pump_power_on  = True
        _cell_requested = True
        state.save({"pump_on": True, "pump_output_percent": resume_speed, "cell_on": True})
        if _mqtt:
            _mqtt.publish("pump/state",          "ON",                                    retain=True)
            _mqtt.publish("pump/output_percent", pump.get_target_speed(),                  retain=True)
            _mqtt.publish("pump/running",        "ON" if pump.get_speed() > 0 else "OFF", retain=True)
        _publish_power_recovery("super_chlorinate", outage_s)

    elif snap.get("super_chlorinate_active") and _super_chlorinate_remaining_s <= 0:
        # SC was active but ran out during outage; main() already cleared it
        logger.info("Power recovery: SC expired during outage — transitioning to normal")
        _publish_power_recovery("super_chlorinate_expired", outage_s)

    else:
        # Normal schedule resume: restore last known pump/cell state
        logger.info("Power recovery: resuming normal schedule state")
        if snap.get("pump_on", snap.get("pump_power_on")):
            saved_speed = snap.get("pump_output_percent", snap.get("pump_speed", 0))
            pump.request_speed(saved_speed)
            _pump_power_on = True
            state.save({"pump_on": True, "pump_output_percent": saved_speed})
            if _mqtt:
                _mqtt.publish("pump/state",          "ON",                                    retain=True)
                _mqtt.publish("pump/output_percent", pump.get_target_speed(),                  retain=True)
                _mqtt.publish("pump/running",        "ON" if pump.get_speed() > 0 else "OFF", retain=True)
        if snap.get("cell_on"):
            _cell_requested = True
            state.save({"cell_on": True})
        _publish_power_recovery("schedule", outage_s)

    _publish_system_mode()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    global _mqtt, _cell_requested, _polarity_on_time_s
    global _super_chlorinate_active, _super_chlorinate_remaining_s
    global _cell_output_percent, _pump_power_on, _fault_state
    global _startup_snapshot, _service_mode, _service_mode_entered_at, _pre_service_mode_state

    # --- Restore persisted state ---
    saved = state.load()
    _startup_snapshot = dict(saved)
    # Publish critical notification if state.json was missing or corrupt (SPEC §9.5)
    # (MQTT not yet connected here; notification queued and sent after connect below)
    _service_mode = bool(saved.get("service_mode", False))
    _service_mode_entered_at = saved.get("service_mode_entered_at")
    _pre_service_mode_state = saved.get("pre_service_mode_state")
    _cell_requested = False   # never auto-enable cell on restart
    _pump_power_on  = False   # never auto-enable pump on restart
    pump.request_speed(0)     # keepalive sends 0 until user explicitly enables pump power
    # polarity_on_time_accumulator (new name); fall back to old name for one-boot migration
    _polarity_on_time_s = float(saved.get("polarity_on_time_accumulator",
                                          saved.get("polarity_on_time_s", 0.0)))
    _super_chlorinate_active = bool(saved.get("super_chlorinate_active", False))
    # SC remaining: new key "super_chlorinate_remaining", old "super_chlorinate_remaining_s",
    # older still "super_chlorinate_expires_at" (wall-clock epoch — obsolete)
    if "super_chlorinate_remaining" in saved:
        _super_chlorinate_remaining_s = float(saved["super_chlorinate_remaining"])
    elif "super_chlorinate_remaining_s" in saved:
        _super_chlorinate_remaining_s = float(saved["super_chlorinate_remaining_s"])
    elif "super_chlorinate_expires_at" in saved:
        _super_chlorinate_remaining_s = max(0.0, float(saved["super_chlorinate_expires_at"]) - time.time())
    else:
        _super_chlorinate_remaining_s = 0.0
    _raw_output = saved.get("cell_output_percent", config.CELL_OUTPUT_DEFAULT)
    _cell_output_percent = int(_raw_output) if _raw_output is not None else 0
    _fault_state = saved.get("fault_state")  # None = no latched fault; string = fault name
    if _fault_state:
        logger.warning("Restoring latched fault from state.json: %s — cell disabled until reset", _fault_state)
    # Clear super chlorinate if it already ran out
    if _super_chlorinate_active and _super_chlorinate_remaining_s <= 0:
        _super_chlorinate_active = False
        _super_chlorinate_remaining_s = 0.0
        state.save({"super_chlorinate_active": False, "super_chlorinate_remaining": 0.0})
        logger.info("Super chlorinate had no remaining cell-on time — cleared")

    # --- Hardware init (all non-fatal) ---
    pump.init()
    cell.init()
    # Restore polarity direction from state.json; gate is off so direct CH2 write is safe
    _saved_polarity = saved.get("polarity_direction", "forward")
    cell.restore_polarity_at_boot(_saved_polarity)
    logger.info("Polarity direction restored: %s (accumulator=%.0fs)", _saved_polarity, _polarity_on_time_s)
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
    _mqtt.register_service_mode_handler(handle_service_mode_set)
    _mqtt.register_fault_reset_handler(handle_fault_reset)
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
        asyncio.create_task(pump_keepalive_loop(shutdown),      name="pump-keepalive"),
        asyncio.create_task(safety_check_loop(shutdown),        name="safety"),
        asyncio.create_task(cell_duty_cycle_loop(shutdown),     name="cell-duty"),
        asyncio.create_task(actual_duty_sample_loop(shutdown),  name="actual-duty-sample"),
        asyncio.create_task(fast_sensor_loop(shutdown),         name="fast-sensors"),
        asyncio.create_task(sensor_read_loop(shutdown),         name="sensors"),
        asyncio.create_task(state_publish_loop(shutdown),       name="state-pub"),
        asyncio.create_task(system_health_loop(shutdown),       name="system-health"),
        asyncio.create_task(_mqtt.message_loop(),               name="mqtt-rx"),
        asyncio.create_task(acs712_power_on_task(),             name="acs712-power-on"),
        asyncio.create_task(power_recovery_task(shutdown),      name="power-recovery"),
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
