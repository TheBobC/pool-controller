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
    "pump_on": False,
    "pump_output_percent": 0,
    "cell_on": False,
    "cell_output_percent": 0,
    "polarity_on_time_accumulator": 0.0,
    "polarity_direction": "forward",
    "super_chlorinate_active": False,
    "super_chlorinate_remaining": 0.0,
    "service_mode": False,
    "service_mode_entered_at": None,
    "pre_service_mode_state": None,
    "fault_state": None,
    "last_user_override_pump": None,
    "last_user_override_cell": None,
    "last_state_write": None,
}

# One-time migration: old key → new key (SPEC §9.2 rename, Item 18)
_MIGRATIONS: list[tuple[str, str]] = [
    ("pump_power_on",             "pump_on"),
    ("pump_speed",                "pump_output_percent"),
    ("polarity_on_time_s",        "polarity_on_time_accumulator"),
    ("super_chlorinate_remaining_s", "super_chlorinate_remaining"),
]

_state: dict[str, Any] = {}


def _apply_migrations(data: dict[str, Any]) -> dict[str, Any]:
    """Rename legacy keys to spec-compliant names.  Runs once on load."""
    for old, new in _MIGRATIONS:
        if old in data and new not in data:
            data[new] = data.pop(old)
            logger.info("State migration: %s → %s", old, new)
    return data


_load_failed: bool = False  # set if load() used defaults due to missing/corrupt file


def load() -> dict[str, Any]:
    """Load state from disk.  Returns defaults if file is missing or corrupt.

    Sets _load_failed=True if defaults were used so callers can publish a
    critical notification (SPEC §9.5).
    """
    global _state, _load_failed
    path = Path(config.STATE_FILE)
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            _state = _apply_migrations(raw)
            _load_failed = False
            logger.info("State loaded from %s: %s", path, _state)
        except Exception as exc:
            logger.error("State file corrupt or unreadable (%s) — using defaults (SPEC §9.5)", exc)
            _state = dict(_DEFAULTS)
            _load_failed = True
    else:
        logger.info("No state file found, using defaults")
        _state = dict(_DEFAULTS)
        _load_failed = False  # missing file is normal on first boot, not an error
    return dict(_state)


def was_load_failed() -> bool:
    """True if last load() fell back to defaults due to a corrupt/unreadable file."""
    return _load_failed


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
                f.flush()
                os.fsync(fd)   # SPEC §9.4 — power-safe write
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
