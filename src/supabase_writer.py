"""Supabase dual-write writer for scanner results."""

from __future__ import annotations

import json
import os
import socket
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# Trading timezone — sync metadata is stamped in Eastern time (DST-aware) so it
# reads consistently with timestamp_est in the cloud tables.
_ET = ZoneInfo("America/New_York")

try:
    from supabase import Client, create_client
except ImportError:
    Client = None
    create_client = None

from log_setup import get_scanner_logger

logger = get_scanner_logger("scanner")

CLOUD_SCHEMA = "trading"
CLOUD_TABLE = "scan_results"
PENDING_WRITES_PATH = Path.home() / "supabase_pending_writes_scanner.jsonl"

SCANNER_SOURCE = "scanner"
_init_lock = threading.Lock()
_writer_instance: "SupabaseScannerWriter | None" = None

# Cap how many dead-letter entries a single drain pass will attempt. A large
# backlog is drained over several passes instead of one unbounded burst, so the
# writer can never open a storm of cloud connections at once (which previously
# exhausted file descriptors and left hundreds of sockets in CLOSE_WAIT).
_MAX_DRAIN_PER_PASS = 50


def _is_dns_error(exc: BaseException) -> bool:
    """True if exc (or anything chained under it) is a DNS resolution failure.

    httpx wraps the underlying socket.gaierror, so we have to walk
    __cause__/__context__ rather than checking the top-level exception type.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return True
        current = current.__cause__ or current.__context__
    return False


def _to_cloud_row(local_id: int, row: dict[str, Any]) -> dict[str, Any]:
    """Map a local SQLite row to a cloud-ready dict."""
    cloud_row: dict[str, Any] = {
        "raw_id_local": int(local_id),
        "source": SCANNER_SOURCE,
        "timestamp_est": row.get("timestamp_est"),
        "received_at": datetime.now(_ET).isoformat(),
        "spx_spot": row.get("spx_spot"),
        "expected_move": row.get("expected_move"),
        "atm_strike": row.get("atm_strike"),
        "atm_call_mid": row.get("atm_call_mid"),
        "atm_put_mid": row.get("atm_put_mid"),
        "call_strike_003": row.get("call_strike_003"),
        "call_delta": row.get("call_delta"),
        "call_mid": row.get("call_mid"),
        "call_10_long_strike": row.get("call_10_long_strike"),
        "call_10_long_mid": row.get("call_10_long_mid"),
        "call_10_premium": row.get("call_10_premium"),
        "call_20_long_strike": row.get("call_20_long_strike"),
        "call_20_long_mid": row.get("call_20_long_mid"),
        "call_20_premium": row.get("call_20_premium"),
        "put_strike_003": row.get("put_strike_003"),
        "put_delta": row.get("put_delta"),
        "put_mid": row.get("put_mid"),
        "put_10_long_strike": row.get("put_10_long_strike"),
        "put_10_long_mid": row.get("put_10_long_mid"),
        "put_10_premium": row.get("put_10_premium"),
        "put_20_long_strike": row.get("put_20_long_strike"),
        "put_20_long_mid": row.get("put_20_long_mid"),
        "put_20_premium": row.get("put_20_premium"),
    }
    return cloud_row


class SupabaseScannerWriter:
    """Best-effort dual-write writer for scanner results."""

    def __init__(self) -> None:
        self._client: Optional[Client] = None
        # Reentrancy guard: ensures retry_pending_writes() can never run nested
        # (a drain calls write_scan, and a successful write_scan triggers a
        # drain — without this flag those recurse and storm cloud connections).
        self._draining = False

    def _get_client(self) -> Client:
        """Create the Supabase client on first use (thread-safe)."""
        if self._client is None:
            with _init_lock:
                if self._client is None:
                    self._client = _create_client()
        return self._client

    def _reset_client(self) -> None:
        """Drop the cached client so the next call builds a fresh one.

        A DNS failure on a long-lived client can mean the underlying
        httpx connection pool is holding a stale resolver/socket state
        (e.g. after the host's network config changed). A fresh client
        picks up the current resolver, so we discard the old one rather
        than reusing it on every subsequent attempt.
        """
        with _init_lock:
            self._client = None

    def write_scan(self, local_id: int, row: dict[str, Any], from_retry: bool = False) -> bool:
        """Dual-write a scan result row to Supabase.

        When called from retry_pending_writes, pass from_retry=True so that
        failures (e.g. duplicate-key constraint) are not re-enqueued into
        the dead-letter file — the cloud row is already there, retrying
        just creates an infinite write -> fail -> enqueue loop.
        """
        try:
            cloud_row = _to_cloud_row(local_id, row)
        except Exception as e:
            logger.warning("[scanner_writer] failed to map row (local_id=%s): %s", local_id, e)
            if not from_retry:
                self._enqueue(local_id, row, error=f"mapping_error: {e}")
            return False

        try:
            client = self._get_client()
            client.schema(CLOUD_SCHEMA).table(CLOUD_TABLE).insert(cloud_row).execute()
            logger.info("[scanner_writer] wrote scan local_id=%s ts=%s",
                        local_id, cloud_row.get("timestamp_est"))
            # Opportunistically drain any dead-letter entries now that writes
            # work again — but ONLY for live writes. Retries (from_retry=True)
            # are themselves issued from inside a drain, so draining here would
            # recurse and open an unbounded storm of cloud connections.
            if not from_retry:
                try:
                    succeeded, failed = self.retry_pending_writes()
                    if succeeded or failed:
                        logger.info("[scanner_writer] drained dead-letter: %d succeeded, %d failed",
                                    succeeded, failed)
                except Exception as e:
                    logger.debug("[scanner_writer] dead-letter drain skipped: %s", e)
            return True
        except Exception as e:
            err_str = str(e)
            is_duplicate = "duplicate key" in err_str.lower()
            logger.warning("[scanner_writer] cloud write failed (local_id=%s, duplicate=%s): %s",
                           local_id, is_duplicate, err_str)
            if _is_dns_error(e):
                logger.warning("[scanner_writer] DNS resolution failed — rebuilding Supabase client")
                self._reset_client()
            # Re-enqueue only if this wasn't a retry AND it isn't a duplicate
            # (duplicates mean the row is already in the cloud — no point retrying)
            if not from_retry and not is_duplicate:
                self._enqueue(local_id, row, error=err_str)
            return False

    def _enqueue(self, local_id: int, row: dict[str, Any], error: str) -> None:
        """Append a failed write to the JSONL retry file."""
        try:
            PENDING_WRITES_PATH.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now(_ET).isoformat(),
                "local_id": int(local_id),
                "row": dict(row),
                "error": error,
            }
            with PENDING_WRITES_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error("[scanner_writer] CRITICAL: failed to enqueue dead-letter (local_id=%s): %s", local_id, e)

    def _local_id_in_cloud(self, local_id: int) -> bool:
        """Check if a scan with the given local_id already exists in cloud."""
        try:
            client = self._get_client()
            resp = (client.schema(CLOUD_SCHEMA).table(CLOUD_TABLE)
                    .select("id")
                    .eq("raw_id_local", local_id)
                    .limit(1)
                    .execute())
            return bool(resp.data)
        except Exception as e:
            logger.debug("[scanner_writer] existence check failed (local_id=%s): %s",
                         local_id, e)
            if _is_dns_error(e):
                self._reset_client()
            return False

    def retry_pending_writes(self) -> tuple[int, int]:
        """Retry any writes that previously failed.

        For each dead-letter entry, first check whether the row is already
        in the cloud (by raw_id_local). If it is, the entry is stale and
        counts as resolved. Otherwise attempt the insert; failures are NOT
        re-enqueued (from_retry=True) so we never create infinite loops.
        The dead-letter file is removed when every entry is resolved.
        """
        if not PENDING_WRITES_PATH.exists():
            return (0, 0)
        # Hard reentrancy guard: never let a drain run nested inside another
        # drain. Belt-and-suspenders alongside the from_retry check in
        # write_scan — together they make a connection storm impossible.
        if self._draining:
            return (0, 0)
        self._draining = True
        try:
            return self._drain_pending_writes()
        finally:
            self._draining = False

    def _drain_pending_writes(self) -> tuple[int, int]:
        """Single bounded drain pass — see retry_pending_writes for semantics."""
        entries: list[dict[str, Any]] = []
        with PENDING_WRITES_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        succeeded = 0
        failed = 0
        already_in_cloud = 0
        # Bound work per pass so a large backlog drains gradually rather than in
        # one unbounded burst of cloud connections. Entries past the cap, and
        # any that fail this pass, are kept for the next pass.
        remaining: list[dict[str, Any]] = list(entries[_MAX_DRAIN_PER_PASS:])
        for entry in entries[:_MAX_DRAIN_PER_PASS]:
            local_id = entry.get("local_id")
            row = entry.get("row", {})
            if local_id is None:
                failed += 1  # malformed entry — drop it (no local_id to retry)
                continue
            # Pre-flight: skip if row is already in cloud
            if self._local_id_in_cloud(int(local_id)):
                already_in_cloud += 1
                continue
            try:
                if self.write_scan(local_id=local_id, row=row, from_retry=True):
                    succeeded += 1
                else:
                    failed += 1
                    remaining.append(entry)
            except Exception as e:
                failed += 1
                remaining.append(entry)
                logger.warning("[scanner_writer] retry error (local_id=%s): %s",
                               local_id, e)
        if already_in_cloud:
            logger.info("[scanner_writer] skipped %d dead-letter entries (already in cloud)",
                        already_in_cloud)
        # Persist exactly the unresolved entries: remove the file when nothing
        # remains, otherwise atomically rewrite it with the leftovers so we
        # neither lose un-drained rows nor re-attempt resolved ones.
        self._rewrite_pending(remaining)
        return (succeeded, failed)

    def _rewrite_pending(self, remaining: list[dict[str, Any]]) -> None:
        """Replace the dead-letter file with ``remaining`` (or delete if empty)."""
        try:
            if not remaining:
                PENDING_WRITES_PATH.unlink(missing_ok=True)
                return
            tmp = PENDING_WRITES_PATH.with_suffix(PENDING_WRITES_PATH.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for entry in remaining:
                    f.write(json.dumps(entry, default=str) + "\n")
            tmp.replace(PENDING_WRITES_PATH)
        except Exception as e:
            logger.error("[scanner_writer] CRITICAL: failed to rewrite dead-letter file: %s", e)


def get_writer() -> SupabaseScannerWriter:
    """Return the module-level singleton writer (thread-safe init)."""
    global _writer_instance
    if _writer_instance is None:
        with _init_lock:
            if _writer_instance is None:
                _writer_instance = SupabaseScannerWriter()
    return _writer_instance


def _create_client() -> Client:
    """Create the Supabase client."""
    if create_client is None:
        raise RuntimeError("supabase package is not installed")
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY must be set")
    return create_client(url, key)
