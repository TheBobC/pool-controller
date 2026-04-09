"""
pump.py — Hayward EcoStar SP3400VSP RS-485 control + telemetry.

Protocol (half-duplex, 19200 8N1 via Waveshare 2-CH RS485 HAT on /dev/ttySC0):

  Command packet (10 bytes, sent every ≤500 ms):
    0x10  0x02  CTRL  PUMP  0x00  SPEED  CSUM_H  CSUM_L  0x10  0x03
    where:
      CTRL  = controller address (0x0C)
      PUMP  = pump address       (0x01)
      SPEED = 0–100 % (0 = off, 100 = full)
      CSUM  = sum(frame[0:-4]) & 0xFFFF split into two bytes

  Response packet (≥14 bytes, pump replies after each command):
    0x10  0x02  PUMP  CTRL  0x00  SPEED  RPM_H  RPM_L  WATTS_H  WATTS_L  ...  CSUM_H  CSUM_L  0x10  0x03
    where:
      SPEED = reported speed %     (byte  5)
      RPM   = big-endian uint16   (bytes 6–7)
      WATTS = big-endian uint16   (bytes 8–9)
      CSUM  = sum(frame[0:-4]) & 0xFFFF split into two bytes

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
_speed: int = 0           # current target speed 0–100 %
_connected: bool = False

# Last parsed pump telemetry; None until first valid response is received.
# Replaced atomically — safe to read from the asyncio thread without a lock.
_telemetry: dict = {"rpm": None, "watts": None, "reported_speed": None}

# Response frame field offsets (0-indexed from frame byte 0 = DLE 0x10)
# Layout: DLE STX SRC DST 0x00 SPEED RPM_H RPM_L WATTS_H WATTS_L ... CSUM_H CSUM_L DLE ETX
_OFF_SPEED    = 5
_OFF_RPM      = 6   # big-endian uint16 at [6][7]
_OFF_WATTS    = 8   # big-endian uint16 at [8][9]
_MIN_RESP_LEN = 14  # 10 data bytes + CSUM_H + CSUM_L + DLE + ETX


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

def _build_packet(speed: int) -> bytes:
    spd = max(0, min(100, speed))
    header = [0x10, 0x02, config.CTRL_ADDR, config.PUMP_ADDR, 0x00, spd]
    csum = sum(header)
    return bytes(header + [(csum >> 8) & 0xFF, csum & 0xFF, 0x10, 0x03])


def _parse_response(data: bytes) -> dict | None:
    """
    Find the first valid pump status frame in *data* and return parsed telemetry.

    Scans for DLE-STX … DLE-ETX frame boundaries, validates checksum, then
    extracts speed %, RPM, and watts from known byte offsets.  Frames whose
    SRC/DST fields don't match the pump→controller direction are ignored (this
    also silently discards any echo of our own transmitted bytes).

    Returns dict {reported_speed, rpm, watts} or None if no valid frame found.
    """
    if not data:
        return None

    logger.debug("Pump RX (%d B): %s", len(data), data.hex())

    i = 0
    while i < len(data) - 1:
        if data[i] != 0x10 or data[i + 1] != 0x02:
            i += 1
            continue

        # Find the matching DLE ETX
        j = i + 2
        while j < len(data) - 1:
            if data[j] == 0x10 and data[j + 1] == 0x03:
                result = _try_parse_frame(data[i : j + 2])
                if result is not None:
                    return result
                break
            j += 1

        i += 1

    return None


def _try_parse_frame(frame: bytes) -> dict | None:
    """Validate checksum and extract telemetry from one complete DLE-STX…DLE-ETX frame."""
    if len(frame) < _MIN_RESP_LEN:
        return None

    # Accept only frames from the pump addressed to us
    if frame[2] != config.PUMP_ADDR or frame[3] != config.CTRL_ADDR:
        return None

    # Checksum covers frame[0:-4]; result stored as big-endian uint16 at frame[-4:-2]
    actual   = sum(frame[:-4]) & 0xFFFF
    expected = (frame[-4] << 8) | frame[-3]
    if actual != expected:
        logger.debug("Pump frame checksum mismatch (calc 0x%04X, frame 0x%04X)", actual, expected)
        return None

    return {
        "reported_speed": frame[_OFF_SPEED],
        "rpm":            (frame[_OFF_RPM]   << 8) | frame[_OFF_RPM   + 1],
        "watts":          (frame[_OFF_WATTS] << 8) | frame[_OFF_WATTS + 1],
    }


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
    """Transmit one speed-control packet and read the pump telemetry response.

    Called every 500 ms (blocking — run in a thread-pool executor from asyncio).
    Returns True on success, False if hardware is absent/faulted.

    After flushing the command the port read-timeout (100 ms) gives the pump
    enough time to reply (~20–50 ms at 19200 baud).  Any valid response frame
    is parsed and stored in _telemetry for retrieval via get_telemetry().
    """
    global _connected, _telemetry
    if _serial is None:
        return False
    packet = _build_packet(_speed)
    with _lock:
        try:
            _serial.write(packet)
            _serial.flush()
        except Exception as exc:
            logger.warning("Pump serial write failed: %s", exc)
            _connected = False
            return False

        # Read up to 64 bytes; port timeout=0.1 s covers the pump reply window
        try:
            raw = _serial.read(64)
            if raw:
                parsed = _parse_response(raw)
                if parsed is not None:
                    _telemetry = parsed   # atomic dict replacement (GIL-safe)
                    logger.debug("Pump telemetry: speed=%s%% rpm=%s W=%s",
                                 parsed.get("reported_speed"),
                                 parsed.get("rpm"),
                                 parsed.get("watts"))
        except Exception as exc:
            logger.debug("Pump response read error: %s", exc)

        return True


def get_telemetry() -> dict:
    """Return the last parsed pump telemetry.

    Keys: ``reported_speed`` (%), ``rpm`` (int), ``watts`` (int).
    Values are ``None`` until the first valid response frame is received.
    """
    return dict(_telemetry)


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
