"""
cell.py — Salt cell control via GeeekPi 4-channel relay HAT (I2C 0x10).

Relay map (1-based channels → bit index on the HAT status byte):
  CH1  Gate      — cell mains power. Must be de-energised during any polarity
                    change; never switch polarity with the gate hot.
  CH2  Polarity A
  CH3  Polarity B
  CH4  Fans      — owned by fans.py (shares the I2C bus via read-modify-write)

Polarity convention:
  forward  = A energised, B de-energised
  reverse  = A de-energised, B energised

Cell-power switching sequence (set_cell):
  on   : set polarity relays to current polarity → energise gate
  off  : de-energise gate

Polarity switching sequence (toggle_polarity / set_polarity):
  1. de-energise gate (cell power off)
  2. wait POLARITY_SWITCH_DELAY_S
  3. flip A and B
  4. wait POLARITY_SWITCH_DELAY_S
  5. re-energise gate only if cell was on before the switch

Safety interlocks live in safety.py — this module only drives hardware.
"""

import logging
import time

import config

logger = logging.getLogger(__name__)

_bus = None
_hw_ok: bool = False
_cell_on: bool = False
_polarity: str = "forward"   # "forward" or "reverse"

_BIT_GATE = 1 << (config.CELL_RELAY_CH_GATE - 1)
_BIT_A    = 1 << (config.CELL_RELAY_CH_A - 1)
_BIT_B    = 1 << (config.CELL_RELAY_CH_B - 1)


def init() -> bool:
    """Open I2C bus, verify HAT is present, force a known-safe baseline:
    gate OFF, polarity = forward.  Non-fatal."""
    global _bus, _hw_ok, _cell_on, _polarity
    try:
        import smbus2  # type: ignore
        b = smbus2.SMBus(1)
        b.read_byte(config.CELL_I2C_ADDR)
        _bus = b
        _hw_ok = True
        _cell_on = False
        _polarity = "forward"
        _apply_polarity_relays(_polarity, gate_on=False)
        logger.info("Cell relay HAT at 0x%02X — gate=CH%d, A=CH%d, B=CH%d",
                    config.CELL_I2C_ADDR,
                    config.CELL_RELAY_CH_GATE,
                    config.CELL_RELAY_CH_A,
                    config.CELL_RELAY_CH_B)
        return True
    except Exception as exc:
        logger.warning("Cell relay HAT unavailable (0x%02X): %s",
                       config.CELL_I2C_ADDR, exc)
        _hw_ok = False
        return False


def _read() -> int:
    return _bus.read_byte(config.CELL_I2C_ADDR)


def _write(byte: int) -> None:
    _bus.write_byte(config.CELL_I2C_ADDR, byte & 0xFF)


def _apply_polarity_relays(polarity: str, gate_on: bool) -> None:
    """Write A/B to match polarity and set gate bit. Leaves other bits (fans) alone."""
    if not _hw_ok or _bus is None:
        return
    current = _read()
    new = current & ~(_BIT_A | _BIT_B | _BIT_GATE)
    if polarity == "forward":
        new |= _BIT_A
    else:
        new |= _BIT_B
    if gate_on:
        new |= _BIT_GATE
    if new != current:
        _write(new)


def _set_gate(on: bool) -> None:
    if not _hw_ok or _bus is None:
        return
    current = _read()
    new = (current | _BIT_GATE) if on else (current & ~_BIT_GATE)
    if new != current:
        _write(new)


def set_cell(on: bool) -> bool:
    """Energise or de-energise the cell gate.  Polarity relays are left at
    their current setting (they match _polarity).  Tracks intent even when
    hardware is absent.  Returns True on successful hardware write."""
    global _cell_on
    _cell_on = on

    if not _hw_ok or _bus is None:
        logger.debug("Cell set to %s (no hardware)", "ON" if on else "OFF")
        return False

    try:
        _apply_polarity_relays(_polarity, gate_on=on)
        logger.info("Cell gate (CH%d) → %s  [polarity=%s]",
                    config.CELL_RELAY_CH_GATE,
                    "ON" if on else "OFF", _polarity)
        return True
    except Exception as exc:
        logger.warning("Cell relay write failed: %s", exc)
        return False


def get_cell_state() -> bool:
    return _cell_on


def get_polarity() -> str:
    return _polarity


def is_hw_ok() -> bool:
    return _hw_ok


# ---------------------------------------------------------------------------
# Polarity switching
# NEVER switch polarity while the gate is energised.
# Blocks for ~2 * POLARITY_SWITCH_DELAY_S; call from an executor.
# ---------------------------------------------------------------------------

def set_polarity(polarity: str) -> str:
    """Switch to the requested polarity ('forward'|'reverse') using the
    mandated sequence.  Returns the polarity in effect after the call."""
    global _polarity, _cell_on
    if polarity not in ("forward", "reverse"):
        logger.warning("Bad polarity: %r", polarity)
        return _polarity
    if polarity == _polarity:
        return _polarity

    was_on = _cell_on

    # Phase 2 safety: pre-switch current must reach zero before polarity change;
    # post-switch current must return within 2 s; verify ADS1115 A2 afterwards.

    # 1. de-energise gate
    _cell_on = False
    try:
        _set_gate(False)
    except Exception as exc:
        logger.warning("Polarity switch: gate-off failed: %s", exc)
    logger.info("Polarity switch: gate OFF")

    # 2. wait
    time.sleep(config.POLARITY_SWITCH_DELAY_S)

    # 3. toggle A and B
    _polarity = polarity
    try:
        _apply_polarity_relays(_polarity, gate_on=False)
    except Exception as exc:
        logger.warning("Polarity switch: A/B flip failed: %s", exc)
    logger.info("Polarity switch: A/B → %s", _polarity)

    # 4. wait
    time.sleep(config.POLARITY_SWITCH_DELAY_S)

    # Phase 2 safety: verify ADS1115 A2 polarity-sense matches _polarity here.

    # 5. re-energise gate if cell was on
    if was_on:
        try:
            _set_gate(True)
            _cell_on = True
            logger.info("Polarity switch: gate ON (polarity=%s)", _polarity)
        except Exception as exc:
            logger.warning("Polarity switch: gate-on failed: %s", exc)
    else:
        logger.info("Polarity switch: cell was off, leaving gate OFF (polarity=%s)",
                    _polarity)

    return _polarity


def toggle_polarity() -> str:
    return set_polarity("reverse" if _polarity == "forward" else "forward")


def close() -> None:
    """De-energise gate on shutdown; leave fans alone."""
    if _hw_ok:
        try:
            _set_gate(False)
        except Exception:
            pass
    if _bus is not None:
        try:
            _bus.close()
        except Exception:
            pass
