"""log_setup.py — Rotating file + console logging for the pool controller."""

import logging
import logging.handlers
import os

LOG_DIR  = "/var/log/jarvis"
LOG_FILE = os.path.join(LOG_DIR, "pool.log")

_FMT    = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup() -> None:
    """Attach a daily-rotating file handler and a console handler to the root logger.

    Rotates at midnight; keeps 60 days of history (auto-purges older files).
    Safe to call once at startup before any other imports use logging.
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    fh = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE,
        when="midnight",
        backupCount=60,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(ch)
