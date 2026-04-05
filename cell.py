"""
cell.py — Salt cell on/off via GeeekPi 4-channel relay HAT (I2C 0x10).

The HAT exposes relay state as a single byte; bit N-1 controls relay N
(1-based).  Writing a 1-bit energises the relay (active HIGH logic).
Set CELL_RELAY_INVERT=true in .env if your board uses active-LOW logic.

Safety interlocks live in safety.py — this module only drives hardware.
"""

import logging

import config

logger = logging.getLogger(__name__)

_bus = None
_hw_ok: bool = False
_cell_on: bool = False

# Pre-computed bit mask for the configured relay channel (1-based → bit index)
_BIT = 1 << (config.CELL_RELAY_CH - 1)


def init() -> bool:
    """Open I2C bus and verify HAT is present.  Non-fatal."""
    global _bus, _hw_ok
    try:
        import smbus2  # type: ignore
        b = smbus2.SMBus(1)
        b.read_byte(config.CELL_I2C_ADDR)   # presence check
        _bus = b
        _hw_ok = True
        logger.info("Cell relay HAT found at I2C 0x%02X, channel %d",
                    config.CELL_I2C_ADDR, config.CELL_RELAY_CH)
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


def set_cell(on: bool) -> bool:
    """Energise (on=True) or de-energise the relay.
    Tracks intent even when hardware is absent.
    Returns True if the hardware write succeeded."""
    global _cell_on
    _cell_on = on

    if not _hw_ok or _bus is None:
        logger.debug("Cell set to %s (no hardware)", "ON" if on else "OFF")
        return False

    # Apply active-LOW inversion if needed
    drive_on = (not on) if config.CELL_RELAY_INVERT else on

    try:
        current = _read()
        new = (current | _BIT) if drive_on else (current & ~_BIT)
        _write(new)
        logger.info("Cell relay → %s", "ON" if on else "OFF")
        return True
    except Exception as exc:
        logger.warning("Cell relay write failed: %s", exc)
        return False


def get_cell_state() -> bool:
    return _cell_on


def is_hw_ok() -> bool:
    return _hw_ok


def close() -> None:
    """De-energise relay on shutdown."""
    if _hw_ok:
        try:
            set_cell(False)
        except Exception:
            pass
    if _bus is not None:
        try:
            _bus.close()
        except Exception:
            pass
