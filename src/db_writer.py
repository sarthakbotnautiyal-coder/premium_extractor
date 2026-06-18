"""Database writer for scanner results."""

import sqlite3
import os


def init_db(db_path: str) -> None:
    """Initialize SQLite database for scanner results."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_results (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_est        TEXT    NOT NULL,
            spx_spot             REAL    NOT NULL,
            expected_move        REAL,
            atm_strike           REAL,
            atm_call_mid         REAL,
            atm_put_mid          REAL,
            call_strike_003      REAL,
            call_delta           REAL,
            call_mid             REAL,
            call_10_long_strike  REAL,
            call_10_long_mid     REAL,
            call_10_premium      REAL,
            call_20_long_strike  REAL,
            call_20_long_mid     REAL,
            call_20_premium      REAL,
            put_strike_003       REAL,
            put_delta            REAL,
            put_mid              REAL,
            put_10_long_strike   REAL,
            put_10_long_mid      REAL,
            put_10_premium       REAL,
            put_20_long_strike   REAL,
            put_20_long_mid      REAL,
            put_20_premium       REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_ts ON scan_results(timestamp_est)")
    conn.commit()
    conn.close()


def save_scan_result(conn: sqlite3.Connection, result: dict) -> int | None:
    """Insert a scan result. Returns the new row id, or None on error."""
    try:
        cur = conn.execute("""
            INSERT INTO scan_results (
                timestamp_est, spx_spot, expected_move,
                atm_strike, atm_call_mid, atm_put_mid,
                call_strike_003, call_delta, call_mid,
                call_10_long_strike, call_10_long_mid, call_10_premium,
                call_20_long_strike, call_20_long_mid, call_20_premium,
                put_strike_003, put_delta, put_mid,
                put_10_long_strike, put_10_long_mid, put_10_premium,
                put_20_long_strike, put_20_long_mid, put_20_premium
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            result.get("timestamp_est"),
            result.get("spx_spot"),
            result.get("expected_move"),
            result.get("atm_strike"),
            result.get("atm_call_mid"),
            result.get("atm_put_mid"),
            result.get("call_strike_003"),
            result.get("call_delta"),
            result.get("call_mid"),
            result.get("call_10_long_strike"),
            result.get("call_10_long_mid"),
            result.get("call_10_premium"),
            result.get("call_20_long_strike"),
            result.get("call_20_long_mid"),
            result.get("call_20_premium"),
            result.get("put_strike_003"),
            result.get("put_delta"),
            result.get("put_mid"),
            result.get("put_10_long_strike"),
            result.get("put_10_long_mid"),
            result.get("put_10_premium"),
            result.get("put_20_long_strike"),
            result.get("put_20_long_mid"),
            result.get("put_20_premium"),
        ))
        conn.commit()
        return int(cur.lastrowid)
    except Exception:
        return None
