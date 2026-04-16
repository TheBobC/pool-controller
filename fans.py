"""
fans.py — Enclosure fan control on GeeekPi 4-channel relay HAT (I2C 0x10 CH3).

Uses the HAT's per-channel register (register N = channel N) with the
inverted logic confirmed on the bench: 0x00 = energised (fans ON),
0xFF = de-energised (fans OFF).

Shares the HAT with cell.py; writes only the fan channel register so the
cell gate / polarity relays are untouched.  cell.init() is expected to run
first and clear every channel to OFF as the very first I2C operation.
"""

import logging

import config

logger = logging.getLogger(__name__)

_bus = None
_hw_ok: bool = False
_fans_on: bool = False

RELAY_ON  = 0x00   # energised  (inverted HAT)
RELAY_OFF = 0xFF   # de-energised


def init() -> bool:
    global _bus, _hw_ok, _fans_on
    try:
        import smbus2  # type: ignore
        b = smbus2.SMBus(1)
        # Redundant-but-safe: force fan channel OFF on entry.
        b.write_byte_data(config.CELL_I2C_ADDR, config.FAN_RELAY_CH, RELAY_OFF)
        _bus = b
        _hw_ok = True
        _fans_on = False
        logger.info("Fan relay on HAT 0x%02X CH%d (OFF)",
                    config.CELL_I2C_ADDR, config.FAN_RELAY_CH)
        return True
    except Exception as exc:
        logger.warning("Fan relay unavailable: %s", exc)
        _hw_ok = False
        return False


def set_fans(on: bool) -> bool:
    global _fans_on
    changed = (_fans_on != on)
    _fans_on = on
    if not _hw_ok or _bus is None:
        return False
    try:
        _bus.write_byte_data(
            config.CELL_I2C_ADDR,
            config.FAN_RELAY_CH,
            RELAY_ON if on else RELAY_OFF,
        )
        if changed:
            logger.info("Fans (CH%d) → %s", config.FAN_RELAY_CH, "ON" if on else "OFF")
        return True
    except Exception as exc:
        logger.warning("Fan relay write failed: %s", exc)
        return False


def get_state() -> bool:
    return _fans_on


def is_hw_ok() -> bool:
    return _hw_ok


def close() -> None:
    if _hw_ok:
        try:
            set_fans(False)
        except Exception:
            pass
    if _bus is not None:
        try:
            _bus.close()
        except Exception:
            pass
