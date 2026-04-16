"""
cell.py — Salt cell control via GeeekPi 4-channel relay HAT (I2C 0x10).

Relay map (1-based channel = register number on the HAT):
  CH1  Gate      — cell mains power. Must be de-energised during any polarity
                    change; never switch polarity with the gate hot.
  CH2  Polarity  — single channel drives both A+B coils (tied in parallel);
                    toggling CH2 flips the cell's applied polarity.
  CH3  Fans      — owned by fans.py.
  CH4  unused.

INVERTED HAT logic (confirmed on the bench):
  write 0x00 to register N → channel N ENERGISED (relay ON / contact active)
  write 0xFF to register N → channel N DE-ENERGISED (relay OFF)

Polarity convention:
  forward = CH2 de-energised (0xFF)
  reverse = CH2 energised    (0x00)

Cell-power switching (set_cell):
  on   : energise gate (CH1 = 0x00)
  off  : de-energise gate (CH1 = 0xFF)

Polarity switching (toggle_polarity / set_polarity):
  1. de-energise gate (cell power off)
  2. wait POLARITY_SWITCH_DELAY_S
  3. flip CH2 to the target polarity
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

RELAY_ON  = 0x00   # energised  (inverted HAT)
RELAY_OFF = 0xFF   # de-energised

_ALL_CHANNELS = (1, 2, 3, 4)

_POLARITY_VAL = {
    "forward": RELAY_OFF,
    "reverse": RELAY_ON,
}


def _write_channel(ch: int, val: int) -> None:
    _bus.write_byte_data(config.CELL_I2C_ADDR, ch, val & 0xFF)


def init() -> bool:
    """Open I2C bus and FIRST drive all four channels to 0xFF (all
    de-energised) before any other I2C traffic.  Non-fatal — returns False
    if the HAT is absent."""
    global _bus, _hw_ok, _cell_on, _polarity
    try:
        import smbus2  # type: ignore
        b = smbus2.SMBus(1)
        # First I2C operations on the HAT: force every channel OFF.
        # Also doubles as a presence probe — a missing HAT raises here.
        for ch in _ALL_CHANNELS:
            b.write_byte_data(config.CELL_I2C_ADDR, ch, RELAY_OFF)
        _bus = b
        _hw_ok = True
        _cell_on = False
        _polarity = "forward"
        logger.info("Cell relay HAT at 0x%02X — gate=CH%d, polarity=CH%d (all OFF)",
                    config.CELL_I2C_ADDR,
                    config.CELL_RELAY_CH_GATE,
                    config.CELL_RELAY_CH_POLARITY)
        return True
    except Exception as exc:
        logger.warning("Cell relay HAT unavailable (0x%02X): %s",
                       config.CELL_I2C_ADDR, exc)
        _hw_ok = False
        return False


def _set_gate(on: bool) -> None:
    if not _hw_ok or _bus is None:
        return
    _write_channel(config.CELL_RELAY_CH_GATE, RELAY_ON if on else RELAY_OFF)


def _set_polarity_relay(polarity: str) -> None:
    if not _hw_ok or _bus is None:
        return
    _write_channel(config.CELL_RELAY_CH_POLARITY, _POLARITY_VAL[polarity])


def set_cell(on: bool) -> bool:
    """Energise or de-energise the cell gate (CH1) only.  Polarity relay is
    left as-is.  Tracks intent even when hardware is absent.  Returns True
    on successful hardware write."""
    global _cell_on
    _cell_on = on

    if not _hw_ok or _bus is None:
        logger.debug("Cell set to %s (no hardware)", "ON" if on else "OFF")
        return False

    try:
        _set_gate(on)
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
    mandated gate-cut / CH2-toggle / gate-restore sequence.  Returns the
    polarity in effect after the call."""
    global _polarity, _cell_on
    if polarity not in ("forward", "reverse"):
        logger.warning("Bad polarity: %r", polarity)
        return _polarity
    if polarity == _polarity:
        return _polarity

    was_on = _cell_on

    # Phase 2 safety: pre-switch current must reach zero before polarity change;
    # post-switch current must return within 2 s; verify ADS1115 A2 afterwards.

    # 1. de-energise gate (CH1 OFF)
    _cell_on = False
    try:
        _set_gate(False)
    except Exception as exc:
        logger.warning("Polarity switch: gate-off failed: %s", exc)
    logger.info("Polarity switch: gate OFF")

    # 2. wait
    time.sleep(config.POLARITY_SWITCH_DELAY_S)

    # 3. flip CH2 to target polarity (drives A+B coils simultaneously)
    _polarity = polarity
    try:
        _set_polarity_relay(_polarity)
    except Exception as exc:
        logger.warning("Polarity switch: CH2 flip failed: %s", exc)
    logger.info("Polarity switch: CH%d → %s",
                config.CELL_RELAY_CH_POLARITY, _polarity)

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
