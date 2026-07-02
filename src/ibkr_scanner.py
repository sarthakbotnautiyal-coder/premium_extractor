"""
SPX 0DTE Option Scanner - Continuous Loop
- Runs every SCAN_INTERVAL seconds
- Caches option chain & contracts (huge speedup after first scan)
- Persists every scan to data/scanner.db
- Auto-reconnects on disconnect
- Clean single-line logging
- Tracks $10 and $20 wide credit spreads at the 0.03 delta strike

Requirements:
    pip install ib_async python-dateutil

Run TWS or IB Gateway with API enabled.
Stop with Ctrl+C.
"""

import math
import os
import signal
import sqlite3
import time
import logging
import threading
from datetime import datetime
from dateutil import tz

from ib_async import IB, Index, Option
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))


def _handle_sigterm(signum, frame):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, _handle_sigterm)
from config import CONFIG
from log_setup import get_scanner_logger
from tradingview_reader import get_tv_spot



# ---------- CONFIG ----------
HOST = "127.0.0.1"
PORT = CONFIG["ibkr"]["port"]
CLIENT_ID = CONFIG["ibkr"]["scanner_client_id"]
SPOT_CLIENT_ID = CONFIG["ibkr"].get("spot_client_id", CLIENT_ID + 1)  # dedicated SPX index feed
SCAN_INTERVAL = 60         # seconds between scans
TARGET_DELTA = 0.03
MKT_DATA_TYPE = 1          # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
SPREAD_WIDTHS = [10, 20]   # spread widths to compute (in $$)
EXPECTED_MOVE_MULT = 0.85
DB_PATH = CONFIG.get("premium_extractor", {}).get("db_path", "data/scanner.db")
SPOT_STALENESS_SECS = 120  # reject SPX spot if unchanged for this long (live-feed freeze guard)

# --- 0.03-delta anchoring (find the short strike infrequently, stream the legs) ---
# Instead of snapshotting hundreds of contracts every scan (which floods the
# Gateway output buffer and freezes the SPX feed), we locate the 0.03-delta
# short strike over a narrow band only occasionally, then keep a tiny set of
# persistent streaming subscriptions (short legs, long legs, ATM) for pricing.
ANCHOR_INTERVAL_SECS = 180        # re-find the 0.03-delta strike at least this often
ANCHOR_SPOT_MOVE_PCT = 0.003      # ...or sooner if spot moves this far since last anchor
ANCHOR_BAND_PCT = 0.015           # cold-start: one-sided OTM band (ATM→+1.5% calls, ATM→-1.5% puts)
ANCHOR_NEIGHBOURHOOD_STRIKES = 10 # warm re-anchor: search ±10 strikes (~±50pts) around last short

# --- TradingView fallback spot (when the IBKR SPX index feed freezes) ---
# tradingview.db carries an SPX price (~1 row/min). When the IBKR index feed
# freezes, we fall back to this so the anchor isn't stuck on a stale price, and
# we cross-check it against the index feed even when that feed *looks* fresh
# (a frozen feed can report a static value with a fresh timer).
TV_SPOT_STALENESS_SECS   = 90    # reject TV spot if its latest row is older than this
TV_CROSSCHECK_DIVERGENCE = 15.0  # pts: index vs TV disagreement that flags a frozen index feed
# Sticky-value guard: a live SPX index feed essentially never reprints the exact
# same price on two consecutive scans (~60s apart). Identical back-to-back
# readings mean the feed is stuck on a stale level while its snapshot timer stays
# fresh — a small (<TV_CROSSCHECK_DIVERGENCE) sticky error otherwise slips past
# both the staleness and divergence guards.
STICKY_SPOT_GUARD = True
# ----------------------------


# ---------- LOGGING SETUP ----------
_LOGS_DIR = Path(__file__).parent.parent / CONFIG["premium_extractor"]["log_path"]
log = get_scanner_logger("scanner", _LOGS_DIR)
# ----------------------------------


EST = tz.gettz("America/New_York")


def get_est_time() -> str:
    return datetime.now(EST).strftime("%Y-%m-%dT%H:%M:%S%z")


def init_db(db_path: str) -> None:
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON scan_results(timestamp_est)")
    conn.commit()
    conn.close()


def save_scan(conn: sqlite3.Connection, ts: str, r: dict) -> int:
    """Insert a scan row. Returns the new local row id for Supabase dual-write."""
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
        ) VALUES (?, ?, ?,
                  ?, ?, ?,
                  ?, ?, ?,
                  ?, ?, ?,
                  ?, ?, ?,
                  ?, ?, ?,
                  ?, ?, ?,
                  ?, ?, ?)
    """, (
        ts, r["spx_spot"], r.get("expected_move"),
        r.get("atm_strike"), r.get("atm_call_mid"), r.get("atm_put_mid"),
        r.get("call_strike"), r.get("call_delta"), r.get("call_mid"),
        r.get("call_10_long_strike"), r.get("call_10_long_mid"), r.get("call_10_premium"),
        r.get("call_20_long_strike"), r.get("call_20_long_mid"), r.get("call_20_premium"),
        r.get("put_strike"), r.get("put_delta"), r.get("put_mid"),
        r.get("put_10_long_strike"), r.get("put_10_long_mid"), r.get("put_10_premium"),
        r.get("put_20_long_strike"), r.get("put_20_long_mid"), r.get("put_20_premium"),
    ))
    conn.commit()
    return int(cur.lastrowid)


def _dual_write_to_supabase(local_id: int, ts: str, r: dict) -> None:
    """Best-effort dual-write of a saved scan row to Supabase.

    Never raises — a cloud failure never crashes the scan loop.
    Failed rows are queued to ~/supabase_pending_writes_scanner.jsonl.
    """
    try:
        from supabase_writer import get_writer
        row = {
            "timestamp_est":      ts,
            "spx_spot":           r.get("spx_spot"),
            "expected_move":      r.get("expected_move"),
            "atm_strike":         r.get("atm_strike"),
            "atm_call_mid":       r.get("atm_call_mid"),
            "atm_put_mid":        r.get("atm_put_mid"),
            "call_strike_003":    r.get("call_strike"),
            "call_delta":         r.get("call_delta"),
            "call_mid":           r.get("call_mid"),
            "call_10_long_strike":r.get("call_10_long_strike"),
            "call_10_long_mid":   r.get("call_10_long_mid"),
            "call_10_premium":    r.get("call_10_premium"),
            "call_20_long_strike":r.get("call_20_long_strike"),
            "call_20_long_mid":   r.get("call_20_long_mid"),
            "call_20_premium":    r.get("call_20_premium"),
            "put_strike_003":     r.get("put_strike"),
            "put_delta":          r.get("put_delta"),
            "put_mid":            r.get("put_mid"),
            "put_10_long_strike": r.get("put_10_long_strike"),
            "put_10_long_mid":    r.get("put_10_long_mid"),
            "put_10_premium":     r.get("put_10_premium"),
            "put_20_long_strike": r.get("put_20_long_strike"),
            "put_20_long_mid":    r.get("put_20_long_mid"),
            "put_20_premium":     r.get("put_20_premium"),
        }
        get_writer().write_scan(local_id=local_id, row=row)
    except Exception as e:
        print(f"   ⚠ Supabase scanner dual-write skipped: {e}")


def mid(ticker):
    if ticker is None:
        return float("nan")
    bid, ask = ticker.bid, ticker.ask
    if bid is not None and ask is not None and bid > 0 and ask > 0 and not (math.isnan(bid) or math.isnan(ask)):
        return (bid + ask) / 2
    for px in (ticker.last, ticker.close, ticker.marketPrice()):
        if px and not math.isnan(px):
            return px
    return float("nan")


def safe_round(x, n=2):
    """Round, returning None for NaN/None."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(x, n)


# ---------- SPX LIVE SPOT FEED ----------
# Replaces the broken reqTickers(single SPX) snapshot approach.
# reqTickers for a single Index contract returns a one-time snapshot whose
# ticker.time is always the request timestamp (never stale), even when
# marketPrice() is frozen from a feed hiccup.
# Solution: use a persistent reqMktData subscription with a live ticker
# that updates on every market tick. Track the last PRICE CHANGE timestamp
# to detect true staleness (vs just ticker object age which is always fresh).

class SpxSpotFeed:
    """
    Persistent streaming SPX spot feed using reqMktData.
    Solves the frozen-snapshot problem by tracking when the feed last
    received data from IBKR, not just when the price changed.
    """
    def __init__(self, ib: IB):
        self.ib = ib
        self._contract = None
        self._ticker = None
        self._spot = float("nan")
        self._last_tick_time = None     # datetime of last tick received from IBKR
        self._last_change_time = None   # datetime of last price change (for debugging)
        self._last_full_reconnect = None  # throttle full reconnects
        self._lock = threading.Lock()

    def ensure_started(self) -> None:
        """Qualify the SPX index on our OWN (dedicated) connection and start
        streaming. Idempotent — safe to call every scan cycle. The qualified
        contract is cached and survives stop()/restart."""
        if self._contract is None:
            spx = Index("SPX", "CBOE", "USD")
            self.ib.qualifyContracts(spx)
            self._contract = spx
        if self._ticker is None:
            self._ticker = self.ib.reqMktData(self._contract, "", False, False)
            self._update_spot(self._ticker)

    def start(self, contract: Index) -> None:
        """Start streaming SPX spot from an externally-qualified contract.
        Retained for backward compatibility; ensure_started() is preferred."""
        if self._ticker is not None:
            return
        self._contract = contract
        self._ticker = self.ib.reqMktData(contract, "", False, False)
        self._update_spot(self._ticker)

    def stop(self) -> None:
        """Cancel the streaming subscription but KEEP the qualified contract so
        a restart can re-subscribe without re-qualifying."""
        if self._ticker is not None:
            try:
                self.ib.cancelMktData(self._contract)
            except Exception:
                pass
            self._ticker = None
            self._spot = float("nan")
            self._last_tick_time = None
            self._last_change_time = None

    def force_full_reconnect(self, min_interval_secs: float = 120.0) -> bool:
        """Tear down and rebuild the spot connection — the only reliable cure
        for a frozen SPX index data-farm subscription (a soft re-subscribe just
        gets the same frozen value back). Throttled to avoid hammering the
        Gateway; returns True only when a reconnect was actually issued."""
        now = datetime.now(EST)
        if (self._last_full_reconnect is not None
                and (now - self._last_full_reconnect).total_seconds() < min_interval_secs):
            return False
        self._last_full_reconnect = now
        self.stop()
        try:
            self.ib.disconnect()
        except Exception:
            pass
        self.ib.sleep(2)
        self._contract = None  # force re-qualify on the fresh connection
        self.ib.connect(HOST, PORT, clientId=SPOT_CLIENT_ID, timeout=10)
        self.ib.reqMarketDataType(MKT_DATA_TYPE)
        self.ensure_started()
        return True

    def _update_spot(self, ticker) -> None:
        """Update spot price and mark that we received a tick from the feed."""
        mp = ticker.marketPrice()
        new_spot = mp if (mp and not math.isnan(mp)) else ticker.close
        if new_spot and not math.isnan(new_spot):
            with self._lock:
                changed = math.isnan(self._spot) or abs(self._spot - new_spot) > 0.001
                self._spot = new_spot
                self._last_tick_time = datetime.now(EST)  # mark that feed is alive
                if changed:
                    self._last_change_time = datetime.now(EST)

    def get_spot(self) -> tuple[float, float]:
        """
        Returns (spot, seconds_since_last_tick).
        Staleness is based on when the feed last received ANY data from IBKR,
        not just when the price changed.
        """
        t = self._ticker
        if t is not None:
            self._update_spot(t)

        with self._lock:
            spot = self._spot
            last_tick = self._last_tick_time

        age = 0.0
        if last_tick is not None:
            age = (datetime.now(EST) - last_tick).total_seconds()

        return spot, age

    def get_snapshot(self) -> float:
        """Return current spot without staleness check (for debug logging)."""
        t = self._ticker
        if t is not None:
            self._update_spot(t)
        with self._lock:
            return self._spot


# ---------- LEG STREAMER ----------
class LegStreamer:
    """Persistent streaming subscriptions for the small working set of option
    legs we actually trade: the 0.03-delta short strikes, their long legs, and
    the ATM straddle. Replaces the per-scan reqTickers() over hundreds of
    contracts that was flooding the Gateway output buffer.

    The working set is refreshed only when the short strikes are re-anchored,
    so steady-state load is ~8 streaming tickers instead of ~240 snapshots.
    """
    def __init__(self, ib: IB):
        self.ib = ib
        self._tickers = {}   # (right, strike) -> ticker

    def set_working_set(self, contracts: list) -> None:
        """Subscribe to the given contracts; cancel any no longer needed."""
        wanted = {(c.right, c.strike): c for c in contracts}
        for key in list(self._tickers):
            if key not in wanted:
                try:
                    self.ib.cancelMktData(self._tickers[key].contract)
                except Exception:
                    pass
                del self._tickers[key]
        for key, c in wanted.items():
            if key not in self._tickers:
                self._tickers[key] = self.ib.reqMktData(c, "", False, False)

    def get(self, right: str, strike: float):
        return self._tickers.get((right, strike))

    def stop(self) -> None:
        for t in list(self._tickers.values()):
            try:
                self.ib.cancelMktData(t.contract)
            except Exception:
                pass
        self._tickers.clear()


# ---------- CACHE ----------
class ScanCache:
    """Caches static-per-day data: SPX contract, chain, qualified option contracts."""
    def __init__(self):
        self.spx = None
        self.chain = None
        self.expiry = None
        self.contracts_by_strike_right = {}
        self.last_spot = None
        # Raw IBKR index spot from the previous scan (before any TV override),
        # used by the sticky-value guard to spot a feed stuck on a stale level.
        self.prev_primary_spot = None
        self._reset_anchor()

    def _reset_anchor(self):
        # Current 0.03-delta anchor (refreshed every ANCHOR_INTERVAL_SECS / on spot move)
        self.atm_strike = None
        self.call_short = None
        self.put_short = None
        self.call_delta = None   # delta at last anchor (fallback if live greeks absent)
        self.put_delta = None
        self.anchor_spot = None
        self.anchor_time = None

    def is_valid_for_today(self) -> bool:
        today = datetime.now(EST).strftime("%Y%m%d")
        return self.expiry == today and self.chain is not None

    def reset(self):
        self.spx = None
        self.chain = None
        self.expiry = None
        self.contracts_by_strike_right = {}
        self.prev_primary_spot = None
        self._reset_anchor()


def ensure_chain(ib: IB, cache: ScanCache, spot: float) -> None:
    if cache.is_valid_for_today():
        return

    log.info("Loading option chain (one-time per day)...")
    cache.reset()

    spx = Index("SPX", "CBOE", "USD")
    ib.qualifyContracts(spx)
    cache.spx = spx

    chains = ib.reqSecDefOptParams(spx.symbol, "", spx.secType, spx.conId)
    chain = next((c for c in chains if c.tradingClass == "SPXW" and c.exchange == "SMART"), None)
    if chain is None:
        chain = next((c for c in chains if c.exchange == "SMART"), chains[0])

    today = datetime.now(EST).strftime("%Y%m%d")
    if today not in chain.expirations:
        raise RuntimeError(f"No 0DTE expiry today ({today})")

    cache.chain = chain
    cache.expiry = today
    log.info(f"Chain loaded: {len(chain.strikes)} strikes, expiry {today}")


def get_or_qualify_contracts(
    ib: IB, cache: ScanCache, strikes: list[float], rights=("C", "P")
) -> list:
    needed = []
    for k in strikes:
        for right in rights:
            if (k, right) not in cache.contracts_by_strike_right:
                needed.append(Option("SPX", cache.expiry, k, right, "SMART", tradingClass="SPXW"))

    if needed:
        ib.qualifyContracts(*needed)
        for c in needed:
            if c.conId:
                cache.contracts_by_strike_right[(c.strike, c.right)] = c

    out = []
    for k in strikes:
        for right in rights:
            c = cache.contracts_by_strike_right.get((k, right))
            if c:
                out.append(c)
    return out


def _live_delta(ticker):
    """Return live model delta from a streaming ticker, or None if unavailable."""
    if ticker is None:
        return None
    mg = ticker.modelGreeks
    if mg is None or mg.delta is None or math.isnan(mg.delta):
        return None
    return round(mg.delta, 4)


def anchor_strikes(ib: IB, cache: ScanCache, spot: float):
    """Locate the 0.03-delta short strikes via a one-sided narrow-band snapshot.

    Two key optimisations over the old ±2.5% both-sided approach:

    1. ONE-SIDED: only qualify calls ABOVE ATM and puts BELOW ATM.
       The old code qualified both rights for every strike (150 contracts);
       the 0.03-delta call is always OTM above ATM, and the 0.03-delta put is
       always OTM below ATM — the ITM half is useless and now skipped (~50% cut).

    2. WARM NEIGHBOURHOOD SEARCH: on a re-anchor (previous short strikes are
       known) search only ±ANCHOR_NEIGHBOURHOOD_STRIKES strikes around each
       prior short strike instead of ±ANCHOR_BAND_PCT of spot.  This cuts a
       re-anchor snapshot from ~75 contracts to ~20.  A cold-start (no prior
       short) falls back to a one-sided ANCHOR_BAND_PCT band (~40 contracts).

    Both changes keep contract counts well under the 100-ticker Gateway cap and
    eliminate the output-buffer overflow that was dropping SPX spot ticks.
    """
    all_strikes = sorted(cache.chain.strikes)
    atm_strike = min(all_strikes, key=lambda s: abs(s - spot))

    # ---- call side: strikes ABOVE ATM only ----
    warm_call = cache.call_short is not None
    if warm_call:
        # Neighbourhood around previous short strike
        lo_c = cache.call_short - ANCHOR_NEIGHBOURHOOD_STRIKES * 5
        hi_c = cache.call_short + ANCHOR_NEIGHBOURHOOD_STRIKES * 5
    else:
        # Cold start: ATM → +ANCHOR_BAND_PCT
        lo_c = atm_strike
        hi_c = spot * (1 + ANCHOR_BAND_PCT)
    call_strikes = [s for s in all_strikes if lo_c <= s <= hi_c and s > atm_strike]

    # ---- put side: strikes BELOW ATM only ----
    warm_put = cache.put_short is not None
    if warm_put:
        lo_p = cache.put_short - ANCHOR_NEIGHBOURHOOD_STRIKES * 5
        hi_p = cache.put_short + ANCHOR_NEIGHBOURHOOD_STRIKES * 5
    else:
        lo_p = spot * (1 - ANCHOR_BAND_PCT)
        hi_p = atm_strike
    put_strikes = [s for s in all_strikes if lo_p <= s <= hi_p and s < atm_strike]

    call_contracts = get_or_qualify_contracts(ib, cache, call_strikes, rights=("C",))
    put_contracts  = get_or_qualify_contracts(ib, cache, put_strikes,  rights=("P",))
    contracts = call_contracts + put_contracts

    log.info(
        f"anchor_strikes: {len(call_contracts)} call + {len(put_contracts)} put = "
        f"{len(contracts)} contracts (warm_call={warm_call}, warm_put={warm_put})"
    )

    if not contracts:
        log.warning("anchor_strikes: no contracts to snapshot — skipping")
        return

    # Snapshot the one-sided band to read real model Greeks. This runs on the
    # OPTION connection (ib), which is separate from the SPX spot feed's
    # dedicated connection — so even if this snapshot churns the option
    # connection, the SPX index ticker cannot be starved.
    tickers = ib.reqTickers(*contracts)

    # Warn if IB is delivering delayed/frozen data instead of live.
    non_live = [(t.contract.localSymbol, t.marketDataType) for t in tickers if t.marketDataType not in (0, 1, None)]
    if non_live:
        sample = non_live[:3]
        label = _MDT_NAMES.get(sample[0][1], f"type={sample[0][1]}")
        log.warning(f"DATA TYPE: {label} (not live!) — {len(non_live)}/{len(tickers)} contracts affected, e.g. {[s for s,_ in sample]}")

    def closest_to_delta(right: str):
        best, best_diff = None, float("inf")
        for t in tickers:
            if t.contract.right != right:
                continue
            mg = t.modelGreeks
            if mg is None or mg.delta is None or math.isnan(mg.delta):
                continue
            diff = abs(abs(mg.delta) - TARGET_DELTA)
            if diff < best_diff:
                best_diff, best = diff, t
        return best

    target_call = closest_to_delta("C")
    target_put  = closest_to_delta("P")

    cache.atm_strike = atm_strike
    cache.call_short = target_call.contract.strike if target_call else None
    cache.put_short  = target_put.contract.strike if target_put else None
    cache.call_delta = _live_delta(target_call)
    cache.put_delta  = _live_delta(target_put)
    cache.anchor_spot = spot
    cache.anchor_time = datetime.now(EST)


def _build_working_set(ib: IB, cache: ScanCache) -> list:
    """Qualified contracts for the legs we stream: ATM straddle + short/long legs."""
    wanted = []  # list of (strike, right)
    if cache.atm_strike is not None:
        wanted += [(cache.atm_strike, "C"), (cache.atm_strike, "P")]
    if cache.call_short is not None:
        wanted.append((cache.call_short, "C"))
        wanted += [(cache.call_short + w, "C") for w in SPREAD_WIDTHS]
    if cache.put_short is not None:
        wanted.append((cache.put_short, "P"))
        wanted += [(cache.put_short - w, "P") for w in SPREAD_WIDTHS]

    get_or_qualify_contracts(ib, cache, sorted({s for s, _ in wanted}))

    out = []
    for strike, right in wanted:
        c = cache.contracts_by_strike_right.get((strike, right))
        if c:
            out.append(c)
    return out


def _is_sticky_spot(primary_spot: float, prev_primary_spot) -> bool:
    """True when the index feed reprinted the exact same price two scans running.

    A live SPX index feed practically never returns the identical value on two
    consecutive scans (~60s apart); when it does, the feed is stuck on a stale
    level while its snapshot timer stays fresh. Returns False on the first scan
    (no prior reading) or any NaN reading.
    """
    if not STICKY_SPOT_GUARD:
        return False
    if math.isnan(primary_spot):
        return False
    if prev_primary_spot is None or math.isnan(prev_primary_spot):
        return False
    return primary_spot == prev_primary_spot


def run_scan(ib: IB, cache: ScanCache, spot_feed: SpxSpotFeed, legs: LegStreamer) -> dict:
    """Run one scan cycle. Returns result dict."""
    # 1) SPX spot from the dedicated spot-feed connection (isolated from the
    #    option-data churn on this `ib` connection). ensure_started() qualifies
    #    the SPX index on the spot feed's own connection and streams it.
    spot_feed.ensure_started()

    spot, spot_age = spot_feed.get_spot()

    # Raw index reading, captured before any TV override below so the sticky
    # guard always compares index-to-index across scans.
    primary_spot = spot

    # True staleness check: has the price actually CHANGED recently? We track the
    # last PRICE CHANGE timestamp, since a frozen feed still reports a fresh
    # ticker.time on snapshot requests.
    primary_ok = (not math.isnan(spot)) and (spot_age <= SPOT_STALENESS_SECS)

    # Sticky-value guard: identical index readings on two consecutive scans mean
    # the feed is stuck on a stale level (see STICKY_SPOT_GUARD note above).
    sticky = _is_sticky_spot(primary_spot, cache.prev_primary_spot)
    if not math.isnan(primary_spot):
        cache.prev_primary_spot = primary_spot

    # TV fallback spot (~1 row/min, ≤~60s fresh) from tradingview.db. Used when
    # the IBKR index feed freezes, and as an independent cross-check even when
    # the index feed *looks* fresh (it can report a static value with a fresh
    # timer). Reject it if TV's own upstream process has stalled.
    tv_price, tv_age = get_tv_spot()
    tv_ok = (tv_price is not None) and (tv_age is not None) and (tv_age <= TV_SPOT_STALENESS_SECS)

    # Sticky index value: distrust the index feed and prefer TV. If TV isn't
    # available to substitute, force a reconnect (the proven cure for frozen feeds).
    if primary_ok and sticky:
        if tv_ok:
            log.warning(
                f"SPX index feed sticky: identical value {primary_spot:.2f} on two "
                f"consecutive scans — distrusting index feed, using TV={tv_price:.2f}."
            )
            primary_ok = False
        else:
            log.warning(
                f"SPX index feed sticky: identical value {primary_spot:.2f} on two "
                "consecutive scans — forcing SPX feed reconnect (TV cross-check unavailable)."
            )
            force_anchor = True
            primary_ok = False
            try:
                if spot_feed.force_full_reconnect():
                    log.info("SPX spot feed full reconnect issued.")
            except Exception as e:
                log.warning(f"SPX spot feed full reconnect failed: {e}")

    # Cross-check: a fresh-looking index feed that disagrees with TV by a wide
    # margin is the frozen-but-timer-fresh case — distrust the index feed.
    if primary_ok and tv_ok and abs(spot - tv_price) > TV_CROSSCHECK_DIVERGENCE:
        log.warning(
            f"SPX index feed diverges from TV by {abs(spot - tv_price):.1f}pts "
            f"(index={spot:.2f} tv={tv_price:.2f}) — distrusting index feed."
        )
        primary_ok = False

    force_anchor = False
    if primary_ok:
        pass  # use the IBKR index spot as-is
    elif tv_ok:
        log.warning(
            f"Primary SPX feed unusable (age={spot_age:.0f}s price={spot:.2f}) — "
            f"falling back to TV spot={tv_price:.2f} (age={tv_age:.0f}s); "
            "forcing re-anchor and rebuilding the IBKR spot feed."
        )
        spot = tv_price
        force_anchor = True
        # Escalate: only a full reconnect reliably revives a frozen SPX index
        # data-farm subscription. Throttled internally to avoid hammering.
        try:
            if spot_feed.force_full_reconnect():
                log.info("SPX spot feed full reconnect issued.")
        except Exception as e:
            log.warning(f"SPX spot feed full reconnect failed: {e}")
    else:
        raise RuntimeError(
            f"SPX spot unavailable: index stale (age={spot_age:.0f}s price={spot:.2f}), "
            f"TV stale/missing (age={tv_age} price={tv_price})"
        )

    cache.last_spot = spot

    # 2) Ensure chain loaded
    ensure_chain(ib, cache, spot)

    # 3) Re-anchor the 0.03-delta short strikes if stale or spot has drifted.
    #    force_anchor is set when we fell back to TV — the prior anchor was
    #    chosen from a now-distrusted index price, so re-anchor immediately.
    need_anchor = (
        force_anchor
        or cache.anchor_time is None
        or cache.anchor_spot is None
        or (datetime.now(EST) - cache.anchor_time).total_seconds() > ANCHOR_INTERVAL_SECS
        or abs(spot - cache.anchor_spot) / cache.anchor_spot > ANCHOR_SPOT_MOVE_PCT
    )
    if need_anchor:
        anchor_strikes(ib, cache, spot)
        legs.set_working_set(_build_working_set(ib, cache))
        # Let the freshly-subscribed streaming tickers receive their first ticks
        # before we read prices off them this cycle.
        ib.sleep(1.5)
        log.info(
            f"Re-anchored | ATM={cache.atm_strike} "
            f"CCS_short={cache.call_short} PCS_short={cache.put_short}"
        )

    # 4) Read live prices off the persistent streaming legs
    atm_call_t = legs.get("C", cache.atm_strike)
    atm_put_t  = legs.get("P", cache.atm_strike)

    atm_call_mid = mid(atm_call_t)
    atm_put_mid  = mid(atm_put_t)
    em = None
    if not math.isnan(atm_call_mid) and not math.isnan(atm_put_mid):
        em = round((atm_call_mid + atm_put_mid) * EXPECTED_MOVE_MULT, 2)

    r = {
        "spx_spot": round(spot, 2),
        "expected_move": em,
        "atm_strike": cache.atm_strike,
        "atm_call_mid": safe_round(atm_call_mid),
        "atm_put_mid":  safe_round(atm_put_mid),
    }

    # ----- CALL CREDIT SPREAD legs -----
    if cache.call_short is not None:
        short_t = legs.get("C", cache.call_short)
        cmid = mid(short_t)
        r["call_strike"] = cache.call_short
        r["call_delta"]  = _live_delta(short_t) or cache.call_delta
        r["call_mid"]    = safe_round(cmid)

        for width in SPREAD_WIDTHS:
            long_strike = cache.call_short + width
            long_t = legs.get("C", long_strike)
            long_mid = mid(long_t) if long_t else float("nan")
            credit = (cmid - long_mid) if (not math.isnan(cmid) and not math.isnan(long_mid)) else None
            r[f"call_{width}_long_strike"] = long_strike if long_t else None
            r[f"call_{width}_long_mid"]    = safe_round(long_mid)
            r[f"call_{width}_premium"]     = safe_round(credit)
    else:
        r.update(call_strike=None, call_delta=None, call_mid=None)
        for width in SPREAD_WIDTHS:
            r[f"call_{width}_long_strike"] = None
            r[f"call_{width}_long_mid"]    = None
            r[f"call_{width}_premium"]     = None

    # ----- PUT CREDIT SPREAD legs -----
    if cache.put_short is not None:
        short_t = legs.get("P", cache.put_short)
        pmid = mid(short_t)
        r["put_strike"] = cache.put_short
        r["put_delta"]  = _live_delta(short_t) or cache.put_delta
        r["put_mid"]    = safe_round(pmid)

        for width in SPREAD_WIDTHS:
            long_strike = cache.put_short - width
            long_t = legs.get("P", long_strike)
            long_mid = mid(long_t) if long_t else float("nan")
            credit = (pmid - long_mid) if (not math.isnan(pmid) and not math.isnan(long_mid)) else None
            r[f"put_{width}_long_strike"] = long_strike if long_t else None
            r[f"put_{width}_long_mid"]    = safe_round(long_mid)
            r[f"put_{width}_premium"]     = safe_round(credit)
    else:
        r.update(put_strike=None, put_delta=None, put_mid=None)
        for width in SPREAD_WIDTHS:
            r[f"put_{width}_long_strike"] = None
            r[f"put_{width}_long_mid"]    = None
            r[f"put_{width}_premium"]     = None

    return r


def format_summary(r: dict) -> str:
    """Single clean line summarizing the scan."""
    em = f"EM=${r['expected_move']:.2f}" if r.get("expected_move") else "EM=n/a"
    parts = [f"SPX={r['spx_spot']:.2f}", em]
    if r.get("atm_call_mid") is not None and r.get("atm_put_mid") is not None:
        parts.append(f"ATM(C/P)=${r['atm_call_mid']:.2f}/${r['atm_put_mid']:.2f}")

    if r.get("call_strike"):
        c10 = r.get("call_10_premium")
        c20 = r.get("call_20_premium")
        c10s = f"${c10:.2f}" if c10 is not None else "n/a"
        c20s = f"${c20:.2f}" if c20 is not None else "n/a"
        cd = f"{r['call_delta']:+.3f}" if r.get("call_delta") is not None else "n/a"
        parts.append(f"CCS {r['call_strike']:.0f}@{cd} 10w={c10s} 20w={c20s}")

    if r.get("put_strike"):
        p10 = r.get("put_10_premium")
        p20 = r.get("put_20_premium")
        p10s = f"${p10:.2f}" if p10 is not None else "n/a"
        p20s = f"${p20:.2f}" if p20 is not None else "n/a"
        pd = f"{r['put_delta']:+.3f}" if r.get("put_delta") is not None else "n/a"
        parts.append(f"PCS {r['put_strike']:.0f}@{pd} 10w={p10s} 20w={p20s}")

    return " | ".join(parts)


_MDT_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}


def connect(ib: IB) -> None:
    log.info(f"Connecting to {HOST}:{PORT} (clientId={CLIENT_ID})...")
    ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=10)
    ib.reqMarketDataType(MKT_DATA_TYPE)
    log.info("Connected.")
    # Don't verify data type here — SPX verification happens on the spot connection only


def connect_spot(ib_spot: IB) -> None:
    """Connect the dedicated SPX index spot feed on its own client_id, kept
    separate from the option-data connection so option churn can't starve it."""
    log.info(f"Connecting SPOT feed to {HOST}:{PORT} (clientId={SPOT_CLIENT_ID})...")
    ib_spot.connect(HOST, PORT, clientId=SPOT_CLIENT_ID, timeout=10)
    ib_spot.reqMarketDataType(MKT_DATA_TYPE)
    log.info("Spot feed connected.")
    _verify_data_type(ib_spot)  # Verify SPX data type on the spot connection only


def _verify_data_type(ib: IB) -> None:
    """Probe SPX spot to confirm the actual market data type IB is delivering."""
    try:
        spx = Index("SPX", "CBOE", "USD")
        ib.qualifyContracts(spx)
        (t,) = ib.reqTickers(spx)
        mdt = t.marketDataType
        label = _MDT_NAMES.get(mdt, f"unknown({mdt})")
        if mdt in (1, None, 0):
            log.info(f"Market data type confirmed: LIVE (type={mdt})")
        else:
            log.warning(
                f"Market data type: {label.upper()} (type={mdt}) — "
                "you are NOT receiving live data. Check TWS market data subscriptions."
            )
    except Exception as e:
        log.warning(f"Could not verify market data type: {e}")


def main():
    init_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)

    ib = IB()            # option-data connection (legs + anchor snapshots)
    ib_spot = IB()       # dedicated SPX index spot feed (isolated)
    cache = ScanCache()
    spot_feed = SpxSpotFeed(ib_spot)
    legs = LegStreamer(ib)
    connect(ib)
    connect_spot(ib_spot)

    log.info(f"Loop interval: {SCAN_INTERVAL}s. Press Ctrl+C to stop.")
    scan_count = 0
    stale_count = 0  # consecutive stale cycles; full reconnect after threshold

    try:
        while True:
            cycle_start = time.time()
            ts = get_est_time()

            try:
                if not ib.isConnected():
                    log.warning("Option connection lost. Reconnecting...")
                    cache.reset()
                    legs.stop()
                    connect(ib)

                if not ib_spot.isConnected():
                    log.warning("Spot connection lost. Reconnecting...")
                    spot_feed.stop()
                    connect_spot(ib_spot)

                r = run_scan(ib, cache, spot_feed, legs)
                local_id = save_scan(conn, ts, r)
                _dual_write_to_supabase(local_id, ts, r)
                scan_count += 1
                stale_count = 0
                log.info(f"#{scan_count} | {format_summary(r)}")

            except Exception as e:
                log.error(f"Scan failed: {e}")
                if "stale" in str(e).lower():
                    stale_count += 1
                    if stale_count >= 2:
                        # Feed is persistently frozen — reqMktData restart isn't enough.
                        # Full reconnect of the SPOT connection (not the option one)
                        # forces a fresh SPX index data-farm subscription.
                        log.warning(f"SPX feed frozen for {stale_count} cycles — full spot reconnect.")
                        spot_feed.stop()
                        try:
                            ib_spot.disconnect()
                        except Exception:
                            pass
                        ib_spot.sleep(2)
                        connect_spot(ib_spot)
                        spot_feed._contract = None  # force re-qualify on the fresh connection
                        stale_count = 0
                    else:
                        log.warning("Restarting SPX spot feed to recover from stale data.")
                        spot_feed.stop()
                elif "not connected" in str(e).lower() or "timeout" in str(e).lower():
                    cache.reset()
                    spot_feed.stop()
                    legs.stop()
                    stale_count = 0

            elapsed = time.time() - cycle_start
            sleep_for = max(0, SCAN_INTERVAL - elapsed)
            ib.sleep(sleep_for)

    except KeyboardInterrupt:
        log.info("Shutdown requested (Ctrl+C).")
    finally:
        log.info(f"Total scans: {scan_count}")
        spot_feed.stop()
        legs.stop()
        if ib.isConnected():
            ib.disconnect()
        if ib_spot.isConnected():
            ib_spot.disconnect()
        conn.close()
        log.info("Disconnected. Bye.")


if __name__ == "__main__":

    main()