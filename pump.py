"""
pump.py — Hayward EcoStar SP3400VSP RS-485 control.

Protocol (half-duplex, 19200 8N1 via Waveshare 2-CH RS485 HAT on /dev/ttySC0):

  Packet (10 bytes):
    0x10  0x02  CTRL  PUMP  0x00  SPEED  CSUM_H  CSUM_L  0x10  0x03
    where:
      CTRL  = controller address (0x0C)
      PUMP  = pump address       (0x01)
      SPEED = 0–100 % (0 = off, 100 = full)
      CSUM  = sum of the 6 header bytes (before checksum bytes)

  Must be sent every 500 ms or the pump reverts to its local panel.

WARNING: The EcoStar display panel MUST be physically disconnected
         from the RS-485 bus before this controller takes over.
         Two masters on the same half-duplex bus will corrupt packets
         and may damage the pump drive board.
"""

import logging
import threading

import config

logger = logging.getLogger(__name__)

_serial = None
_lock = threading.Lock()
_speed: int = 0       # current target speed 0–100 %
_connected: bool = False


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

def _build_packet(speed: int) -> bytes:
    spd = max(0, min(100, speed))
    header = [0x10, 0x02, config.CTRL_ADDR, config.PUMP_ADDR, 0x00, spd]
    csum = sum(header)
    return bytes(header + [(csum >> 8) & 0xFF, csum & 0xFF, 0x10, 0x03])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init() -> bool:
    """Open the RS-485 serial port.  Non-fatal — returns True on success.

    serial.Serial() is run in a daemon thread with a 3 s timeout to guard
    against sc16is7xx driver hangs on Linux 6.x (open() blocks waiting for
    an interrupt that never fires on idle hardware).
    """
    global _serial, _connected

    result: list = [None]
    error:  list = [None]

    def _open() -> None:
        try:
            import serial  # type: ignore
            result[0] = serial.Serial(
                config.PUMP_PORT,
                baudrate=config.PUMP_BAUD,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
            )
        except Exception as exc:
            error[0] = exc

    t = threading.Thread(target=_open, daemon=True)
    t.start()
    t.join(timeout=3.0)

    if t.is_alive():
        logger.warning("Pump RS-485 port open timed out — sc16is7xx driver hang on %s", config.PUMP_PORT)
        _connected = False
        return False
    if error[0] is not None:
        logger.warning("Pump RS-485 unavailable (%s): %s", config.PUMP_PORT, error[0])
        _connected = False
        return False

    _serial = result[0]
    _connected = True
    logger.info("Pump serial opened: %s @ %d baud", config.PUMP_PORT, config.PUMP_BAUD)
    return True


def set_speed(speed: int) -> None:
    """Set target speed 0–100 %.  Sent on next keepalive tick."""
    global _speed
    _speed = max(0, min(100, speed))


def get_speed() -> int:
    return _speed


def is_connected() -> bool:
    return _connected


def send_keepalive() -> bool:
    """Transmit one speed-control packet.  Call every 500 ms.
    Returns True on success, False if hardware is absent/faulted."""
    global _connected
    if _serial is None:
        return False
    packet = _build_packet(_speed)
    with _lock:
        try:
            _serial.write(packet)
            _serial.flush()
            return True
        except Exception as exc:
            logger.warning("Pump serial write failed: %s", exc)
            _connected = False
            return False


def close() -> None:
    """Send a stop packet then close the port."""
    global _serial, _connected
    _connected = False
    if _serial is not None:
        with _lock:
            try:
                _serial.write(_build_packet(0))
                _serial.flush()
            except Exception:
                pass
            try:
                _serial.close()
            except Exception:
                pass
        _serial = None
        logger.info("Pump serial closed")
