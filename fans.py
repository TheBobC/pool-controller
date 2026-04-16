"""
fans.py — Enclosure fan control on GeeekPi 4-channel relay HAT (I2C 0x10 CH4).

Shares the relay HAT with cell.py via read-modify-write on the status byte;
touches only the CH4 bit so the cell gate/polarity relays are left alone.
"""

import logging

import config

logger = logging.getLogger(__name__)

_bus = None
_hw_ok: bool = False
_fans_on: bool = False

_BIT_FAN = 1 << (config.FAN_RELAY_CH - 1)


def init() -> bool:
    global _bus, _hw_ok, _fans_on
    try:
        import smbus2  # type: ignore
        b = smbus2.SMBus(1)
        b.read_byte(config.CELL_I2C_ADDR)
        _bus = b
        _hw_ok = True
        set_fans(False)
        logger.info("Fan relay on HAT 0x%02X CH%d",
                    config.CELL_I2C_ADDR, config.FAN_RELAY_CH)
        return True
    except Exception as exc:
        logger.warning("Fan relay unavailable: %s", exc)
        _hw_ok = False
        return False


def set_fans(on: bool) -> bool:
    global _fans_on
    _fans_on = on
    if not _hw_ok or _bus is None:
        return False
    try:
        current = _bus.read_byte(config.CELL_I2C_ADDR)
        new = (current | _BIT_FAN) if on else (current & ~_BIT_FAN)
        if new != current:
            _bus.write_byte(config.CELL_I2C_ADDR, new & 0xFF)
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
