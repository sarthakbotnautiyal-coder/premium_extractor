"""
tradingview_reader.py — read SPX spot from tradingview.db for fallback.

Used by the scanner when the IBKR index feed freezes. Provides a lightweight,
read-only, never-raises helper to get the latest SPX price and its age.
"""
from datetime import datetime
from typing import Optional
import logging
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import CONFIG

# Read path from config
TV_DB_PATH = CONFIG.get("data_sources", {}).get("tradingview_db", "../../tradingView_signal_generator/data/tradingview.db")
TV_DB = Path(TV_DB_PATH).resolve() if Path(TV_DB_PATH).exists() or Path(TV_DB_PATH).is_absolute() else Path(__file__).parent.parent.parent / TV_DB_PATH

_LOG = logging.getLogger("tradingview_reader")


def get_tv_spot() -> tuple[Optional[float], Optional[float]]:
    """Return ``(spx_price, age_seconds)`` from the most recent TradingView
    ``fundamentals`` row, or ``(None, None)`` if unavailable.

    Lightweight, read-only, and never raises — intended as a fallback SPX spot
    source for the scanner when the IBKR index feed freezes. ``age_seconds`` is
    how old that row is (TV writes ~1 row/min), so callers can reject it if the
    upstream tradingView_signal_generator process has itself stalled.
    """
    try:
        conn = sqlite3.connect(f"file:{TV_DB}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                """
                SELECT price, received_at
                FROM spx_standardized
                WHERE alert_type = 'fundamentals' AND price IS NOT NULL
                ORDER BY received_at DESC
                LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()
    except Exception as e:
        _LOG.warning("get_tv_spot: DB read failed: %s", e)
        return None, None

    if not row or row[0] is None or row[1] is None:
        return None, None

    price, received_at = row
    try:
        ts = datetime.fromisoformat(received_at)
    except (ValueError, TypeError):
        return float(price), None
    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
    age = (now - ts).total_seconds()
    return float(price), age
