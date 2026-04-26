"""
state.py — Atomic JSON state persistence.

Writes to a sibling temp file then os.replace() so the on-disk file
is never half-written.  Safe across hard power cuts.
"""

import datetime
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "pump_speed": 0,
    "pump_power_on": False,
    "cell_on": False,
    "cell_output_percent": 0,
    "polarity_on_time_s": 0.0,
    "polarity_direction": "forward",
    "super_chlorinate_active": False,
    "super_chlorinate_remaining_s": 0.0,
    "service_mode": False,
    "service_mode_entered_at": None,
    "pre_service_mode_state": None,
    "fault_state": None,
    "last_state_write": None,
}

_state: dict[str, Any] = {}


def load() -> dict[str, Any]:
    """Load state from disk.  Returns defaults if file is missing or corrupt."""
    global _state
    path = Path(config.STATE_FILE)
    if path.exists():
        try:
            _state = json.loads(path.read_text())
            logger.info("State loaded from %s: %s", path, _state)
        except Exception as exc:
            logger.warning("State file unreadable, using defaults: %s", exc)
            _state = dict(_DEFAULTS)
    else:
        logger.info("No state file found, using defaults")
        _state = dict(_DEFAULTS)
    return dict(_state)


def save(updates: dict[str, Any]) -> None:
    """Merge *updates* into current state and atomically write to disk."""
    global _state
    _state.update(updates)
    _state["last_state_write"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path = Path(config.STATE_FILE)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state_tmp_")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(_state, f, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.error("Failed to persist state: %s", exc)


def get(key: str, default: Any = None) -> Any:
    return _state.get(key, default)
