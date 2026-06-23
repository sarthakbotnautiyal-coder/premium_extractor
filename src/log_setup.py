"""Logging setup for premium_extractor."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

# Trading timezone — log files rotate on the Eastern (EST/EDT) calendar day so
# filenames line up with the market session rather than UTC.
_ET = ZoneInfo("America/New_York")


def _et_date_str() -> str:
    """Return today's Eastern (EST/EDT) date as YYYY-MM-DD."""
    return datetime.now(_ET).strftime("%Y-%m-%d")


def _current_file_handler(logger: logging.Logger) -> logging.FileHandler | None:
    """Return the existing FileHandler attached to logger, if any."""
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler):
            return h
    return None


def _install_file_handler(logger: logging.Logger, log_dir: Path, name: str) -> None:
    """Install (or replace) a date-prefixed FileHandler on logger."""
    handler = logging.FileHandler(log_dir / f"{name}.{_et_date_str()}.log")
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)


def _ensure_current_day_handler(logger: logging.Logger, log_dir: Path, name: str) -> None:
    """Replace the FileHandler if the Eastern date has rolled over."""
    today = _et_date_str()
    existing = _current_file_handler(logger)
    if existing is None:
        _install_file_handler(logger, log_dir, name)
        return
    expected = log_dir / f"{name}.{today}.log"
    try:
        if Path(existing.baseFilename).resolve() != expected.resolve():
            logger.removeHandler(existing)
            existing.close()
            _install_file_handler(logger, log_dir, name)
    except Exception:
        # If baseFilename is unavailable for any reason, install fresh.
        logger.removeHandler(existing)
        existing.close()
        _install_file_handler(logger, log_dir, name)


def get_scanner_logger(name: str, log_dir: str = "logs") -> logging.Logger:
    """Get a configured logger with daily Eastern-rotated file handler.

    The file handler writes to ``logs/<name>.YYYY-MM-DD.log`` (Eastern date).
    On every call the current Eastern date is checked and, if it has changed
    since the last setup, the file handler is replaced so logs land in the
    new day's file.

    Console handler is always at INFO. File handler is at INFO so that
    successful writes (e.g. Supabase dual-write confirmations) are visible
    in the log file by default — not silenced at DEBUG.
    """
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True, parents=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if logger.handlers:
        # Already initialized — just make sure the file handler matches today.
        _ensure_current_day_handler(logger, log_path, name)
        return logger

    _install_file_handler(logger, log_path, name)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(logging.INFO)
    logger.addHandler(console)

    return logger