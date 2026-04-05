"""
pump.py — Pool pump relay control via GPIO.

All hardware initialisation is non-fatal: if GPIO or the relay pin
cannot be set up the module still loads and every public method
degrades gracefully (logs a warning, returns a safe value).
"""

import logging

logger = logging.getLogger(__name__)

# GPIO BCM pin connected to the relay IN line
PUMP_RELAY_PIN = 17

_gpio_ok = False
_pump_state = False  # last known logical state

try:
    import RPi.GPIO as GPIO  # type: ignore
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(PUMP_RELAY_PIN, GPIO.OUT, initial=GPIO.LOW)
    _gpio_ok = True
    logger.info("Pump GPIO initialised on pin %d", PUMP_RELAY_PIN)
except Exception as exc:
    logger.warning("Pump GPIO unavailable (hardware missing?): %s", exc)


def set_pump(state: bool) -> bool:
    """Turn the pump relay on (True) or off (False).

    Returns the new logical state.  If GPIO is unavailable the call is
    a no-op and the requested state is returned so callers can track
    intent even without hardware.
    """
    global _pump_state
    _pump_state = state
    if _gpio_ok:
        try:
            GPIO.output(PUMP_RELAY_PIN, GPIO.HIGH if state else GPIO.LOW)
        except Exception as exc:
            logger.warning("GPIO write failed: %s", exc)
    else:
        logger.debug("pump.set_pump(%s) skipped — no GPIO", state)
    return _pump_state


def get_pump_state() -> bool:
    """Return the last requested pump state."""
    return _pump_state


def cleanup():
    """Release GPIO resources if they were acquired."""
    if _gpio_ok:
        try:
            GPIO.cleanup()
        except Exception:
            pass
