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
import time

import config

logger = logging.getLogger(__name__)

_serial = None
_lock = threading.Lock()
_speed: int = 0           # speed actually commanded over RS-485 (100 during preload)
_connected: bool = False

# Last parsed pump telemetry; None until first valid response is received.
# Replaced atomically — safe to read from the asyncio thread without a lock.
_telemetry: dict = {"rpm": None, "watts": None, "reported_speed": None}

# ---------------------------------------------------------------------------
# Preload state machine
# ---------------------------------------------------------------------------
# Any 0→N transition runs at 100% for PRELOAD_DURATION_S before stepping to
# the requested speed.  Matches config.CELL_FLOW_DELAY_S so the cell interlock
# clears at the same moment the preload ends.
#
# _speed              : actual speed sent over RS-485 (100 during preload)
# _preload_active     : True while holding at 100% before target step-down
# _preload_start_time : monotonic timestamp when current preload began
# _preload_target     : what _speed will become after preload completes
# _last_startup_time  : monotonic timestamp of last 0→>0 transition
#
# Thread safety: all mutations in request_speed() and _tick() are protected
# by _lock.  get_* readers rely on CPython GIL for atomic primitive reads.
# ---------------------------------------------------------------------------
_PRELOAD_SPEED: int = 100

_preload_active: bool = False
_preload_start_time: float | None = None
_preload_target: int = 0
_last_startup_time: float | None = None

# pump_stable state machine (SPEC §2.11):
# False while off, in prime, or within PUMP_STABLE_POST_PRIME_S after prime ends.
# True only after that window.  Resets to False on speed→0.
_post_prime_start: float | None = None  # monotonic timestamp when preload last completed

# Response frame field offsets (0-indexed from frame byte 0 = DLE 0x10)
# Observed 13-byte format (single-byte CSUM, SRC=0x00):
#   [0][1] = DLE STX
#   [2]    = SRC = 0x00  (pump reports 0x00, not PUMP_ADDR 0x01)
#   [3]    = DST = CTRL_ADDR
#   [4][5] = FLAGS (0x00 0x00)
#   [6]    = speed %  (0x3d=61%, confirmed by live capture; 0x32 coincides with watts low at 50%)
#   [7][8] = watts BCD "read aloud" — each nibble is a decimal digit
#            e.g. 0x04 0x05 → 405W@61% (matches display), 0x02 0x32 → 232W@50%
#   [9]    = 0x00  (padding/unknown)
#   [10]   = CSUM  (sum(frame[0:10]) & 0xFF)
#   [11][12] = DLE ETX
# RPM derived: int(3450 * speed_pct / 100)  — no RPM field in frame
_OFF_SPEED    = 6
_OFF_WATTS_HI = 7
_OFF_WATTS_LO = 8
_MIN_RESP_LEN = 13


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
    """Validate checksum and extract telemetry from one complete DLE-STX…DLE-ETX frame.

    Handles two observed formats:
      13-byte: 10 data bytes + 1-byte CSUM + DLE + ETX  (SRC = 0x00)
      14-byte: 10 data bytes + 2-byte CSUM + DLE + ETX  (SRC = PUMP_ADDR)
    Data offsets (speed/rpm/watts) are the same for both.
    """
    if len(frame) < _MIN_RESP_LEN:
        return None

    # Accept pump→controller frames: SRC must be PUMP_ADDR or 0x00 (observed),
    # DST must be our CTRL_ADDR.
    if frame[3] != config.CTRL_ADDR:
        return None
    if frame[2] not in (config.PUMP_ADDR, 0x00):
        return None

    # Determine checksum format by frame length.
    # 13-byte: single-byte CSUM at frame[-3], covering frame[0:-3]
    # 14-byte: two-byte big-endian CSUM at frame[-4:-2], covering frame[0:-4]
    if len(frame) == 13:
        actual   = sum(frame[:-3]) & 0xFF
        expected = frame[-3]
    else:
        actual   = sum(frame[:-4]) & 0xFFFF
        expected = (frame[-4] << 8) | frame[-3]

    if actual != expected:
        logger.debug("Pump frame checksum mismatch (calc 0x%02X, frame 0x%02X)", actual, expected)
        return None

    speed_pct = frame[_OFF_SPEED]
    wh, wl = frame[_OFF_WATTS_HI], frame[_OFF_WATTS_LO]
    watts = ((wh >> 4) * 1000) + ((wh & 0x0F) * 100) + ((wl >> 4) * 10) + (wl & 0x0F)
    rpm = int(3450 * speed_pct / 100)
    return {
        "reported_speed": speed_pct,
        "rpm":            rpm,
        "watts":          watts,
    }


# ---------------------------------------------------------------------------
# Preload helpers (called under _lock)
# ---------------------------------------------------------------------------

def _tick() -> None:
    """Advance preload state machine.  Called from send_keepalive() while _lock held."""
    global _speed, _preload_active, _preload_start_time, _preload_target, _post_prime_start
    if not _preload_active or _preload_start_time is None:
        return
    if time.monotonic() - _preload_start_time >= config.CELL_FLOW_DELAY_S:
        _speed = _preload_target
        _preload_active = False
        _preload_start_time = None
        _post_prime_start = time.monotonic()  # begin post-prime stability window
        logger.info("Pump preload complete, stepping to %d%%", _preload_target)


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


def request_speed(target: int) -> None:
    """Request a target speed 0–100 %.  The single chokepoint for all speed changes.

    target = 0  → stop immediately, clear any preload in progress.
    target > 0  → if pump is stopped OR has been running < CELL_FLOW_DELAY_S since
                  last startup, preload at 100% for CELL_FLOW_DELAY_S then step down.
                  If already preloading, update the target without restarting the timer.
                  If running steadily for ≥ CELL_FLOW_DELAY_S, change speed directly.
    """
    global _speed, _preload_active, _preload_start_time, _preload_target, _last_startup_time
    target = max(0, min(100, target))
    now = time.monotonic()

    with _lock:
        if target == 0:
            _speed = 0
            _preload_active = False
            _preload_start_time = None
            _preload_target = 0
            _last_startup_time = None
            _post_prime_start = None  # pump_stable → False on stop (SPEC §2.11)
            return

        if _preload_active:
            # Already preloading — update destination, keep timer running
            if _preload_target != target:
                logger.debug("Pump preload target updated → %d%%", target)
                _preload_target = target
            return

        # Decide whether preload is needed
        needs_preload = (
            _speed == 0
            or (_last_startup_time is not None
                and now - _last_startup_time < config.CELL_FLOW_DELAY_S)
        )

        if needs_preload:
            was_zero = (_speed == 0)
            _preload_active = True
            _preload_start_time = now
            _preload_target = target
            _speed = _PRELOAD_SPEED
            if was_zero:
                _last_startup_time = now
            logger.info(
                "Pump preload: %d%%→%d%% for %.0fs, then target=%d%%",
                0 if was_zero else _speed, _PRELOAD_SPEED,
                config.CELL_FLOW_DELAY_S, target,
            )
        else:
            _speed = target


def get_speed() -> int:
    """Return the speed currently being commanded over RS-485 (100 during preload)."""
    return _speed


def get_target_speed() -> int:
    """Return the user's intended speed (preload_target during preload, _speed otherwise).

    Use this for MQTT state publishing so the HA slider reflects the user's setting,
    not the transient 100% preload value.
    """
    if _preload_active:
        return _preload_target
    return _speed


def is_preloading() -> bool:
    return _preload_active


def get_preload_remaining_s() -> int:
    if not _preload_active or _preload_start_time is None:
        return 0
    return max(0, int(config.CELL_FLOW_DELAY_S - (time.monotonic() - _preload_start_time)))


def is_stable() -> bool:
    """True when pump has been past prime for ≥ PUMP_STABLE_POST_PRIME_S (SPEC §2.11).

    False when: pump off, in prime, or within PUMP_STABLE_POST_PRIME_S after prime ends.
    Resets to False only on speed→0; non-zero speed changes do NOT reset it.
    """
    if _speed == 0 or _preload_active:
        return False
    if _post_prime_start is None:
        return False
    return time.monotonic() - _post_prime_start >= config.PUMP_STABLE_POST_PRIME_S


def get_stable_countdown_s() -> int:
    """Seconds remaining until pump_stable becomes True.  0 when already stable."""
    if is_stable():
        return 0
    if _speed == 0:
        return int(config.CELL_FLOW_DELAY_S + config.PUMP_STABLE_POST_PRIME_S)
    if _preload_active and _preload_start_time is not None:
        preload_remaining = max(0.0, config.CELL_FLOW_DELAY_S - (time.monotonic() - _preload_start_time))
        return int(preload_remaining + config.PUMP_STABLE_POST_PRIME_S)
    if _post_prime_start is not None:
        return max(0, int(config.PUMP_STABLE_POST_PRIME_S - (time.monotonic() - _post_prime_start)))
    return int(config.PUMP_STABLE_POST_PRIME_S)


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
    with _lock:
        _tick()  # advance preload state before building packet
        packet = _build_packet(_speed)
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
