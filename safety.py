"""
safety.py — Salt cell interlock logic.

The cell is permitted only when ALL three conditions hold simultaneously:
  1. Pump speed  ≥ CELL_PUMP_MIN_SPEED  (default 1 %)
  2. Flow switch reports water flowing
  3. Conditions 1 & 2 have been continuously true for CELL_FLOW_DELAY_S (60 s)

Any violation immediately de-energises the cell regardless of user intent.
The 60-second timer resets whenever either condition is lost.

Phase 2 (not yet implemented) — current/polarity fault layer:
  - Overcurrent shutdown when cell current > 9 A
  - Undercurrent fault when gate is on and current < 0.5 A
  - Current-when-off fault when gate is off and current > 0.1 A
  - Pre-switch current must reach 0 before a polarity change is allowed
  - Post-switch current must return within 2 s of re-energising the gate
  - ADS1115 A2 polarity-verify reading must match commanded polarity after each switch
  - Any fault: immediate gate de-energise + MQTT alert + HA notification
"""

import logging
import time
from typing import Callable, Optional

import config

logger = logging.getLogger(__name__)

_flow_pump_since: Optional[float] = None  # monotonic timestamp when conditions first met
_cell_requested: bool = False
_prev_cell_on: bool = False
_trip_handler: Optional[Callable] = None


def register_trip_handler(fn: Callable) -> None:
    """Register a callback invoked on interlock-triggered cell shutdowns.

    Signature: fn(reason: str, pump_speed: int, flow_ok: bool)
    """
    global _trip_handler
    _trip_handler = fn


def update(
    pump_speed: int,
    flow_ok: bool,
    cell_requested: bool,
    set_cell_fn: Callable[[bool], None],
) -> bool:
    """
    Evaluate interlocks and drive the cell relay via *set_cell_fn*.

    Call every ≤1 second.  Returns True if cell is currently energised.
    """
    global _flow_pump_since, _cell_requested, _prev_cell_on
    _cell_requested = cell_requested

    conditions_met = (pump_speed >= config.CELL_PUMP_MIN_SPEED) and flow_ok

    if conditions_met:
        if _flow_pump_since is None:
            _flow_pump_since = time.monotonic()
            logger.info(
                "Safety: pump+flow conditions met — %ds warm-up timer started",
                int(config.CELL_FLOW_DELAY_S),
            )
        elapsed = time.monotonic() - _flow_pump_since
        timer_ok = elapsed >= config.CELL_FLOW_DELAY_S
    else:
        if _flow_pump_since is not None:
            logger.info("Safety: pump+flow conditions lost — timer reset")
        _flow_pump_since = None
        timer_ok = False

    allow = cell_requested and conditions_met and timer_ok

    if _prev_cell_on and not allow and cell_requested and _trip_handler is not None:
        reason = "flow_lost" if not flow_ok else "pump_stopped"
        logger.warning("Safety: cell trip — %s (pump=%d%% flow=%s)", reason, pump_speed, flow_ok)
        _trip_handler(reason=reason, pump_speed=pump_speed, flow_ok=flow_ok)

    _prev_cell_on = allow
    set_cell_fn(allow)
    return allow


def reset_timer() -> None:
    """Force the warm-up timer to restart (e.g. after pump speed drops to 0)."""
    global _flow_pump_since
    _flow_pump_since = None
    logger.debug("Safety: timer manually reset")


def timer_elapsed_s() -> Optional[float]:
    """Seconds since conditions were first met, or None if not running."""
    if _flow_pump_since is None:
        return None
    return time.monotonic() - _flow_pump_since


def is_interlock_ok() -> bool:
    """True when pump+flow conditions and warm-up timer are all satisfied."""
    if _flow_pump_since is None:
        return False
    return (time.monotonic() - _flow_pump_since) >= config.CELL_FLOW_DELAY_S


def is_cell_requested() -> bool:
    return _cell_requested
