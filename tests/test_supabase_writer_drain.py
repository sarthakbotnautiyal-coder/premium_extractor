"""Regression tests for the dead-letter drain in src/supabase_writer.py.

Locks in the fix for the file-descriptor exhaustion incident (2026-06-25):
a successful write triggered an unbounded, recursive ``retry_pending_writes``
drain that opened a storm of Supabase connections, leaking hundreds of sockets
in CLOSE_WAIT until the process ran out of fds — which then also broke the
sqlite TV-spot fallback ("unable to open database file").

These tests assert the two guarantees that make that impossible:
  1. Retries (from_retry=True) never trigger a nested drain (no recursion).
  2. A single drain pass is bounded (_MAX_DRAIN_PER_PASS) and never loses or
     re-attempts already-resolved entries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import supabase_writer as sw


@pytest.fixture
def pending_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "pending.jsonl"
    monkeypatch.setattr(sw, "PENDING_WRITES_PATH", p)
    return p


def _seed(path: Path, n: int, start: int = 1) -> None:
    with path.open("w", encoding="utf-8") as f:
        for i in range(start, start + n):
            f.write(json.dumps({"local_id": i, "row": {"timestamp_est": "t"}}) + "\n")


def _remaining_ids(path: Path) -> list[int]:
    if not path.exists():
        return []
    return [json.loads(line)["local_id"] for line in path.read_text().splitlines() if line.strip()]


def test_successful_write_does_not_recurse_on_retry(pending_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A from_retry=True write must NOT kick off another drain. We assert
    retry_pending_writes is entered at most once for a live write that drains
    a backlog — never re-entered from inside the retry writes."""
    _seed(pending_path, 3)

    writer = sw.SupabaseScannerWriter()
    monkeypatch.setattr(writer, "_get_client", lambda: object())
    monkeypatch.setattr(writer, "_local_id_in_cloud", lambda _id: False)
    # Make the actual insert a no-op success.
    monkeypatch.setattr(sw, "_to_cloud_row", lambda lid, row: {"raw_id_local": lid})

    inserts: list = []

    class _FakeClient:
        def schema(self, *_a, **_k): return self
        def table(self, *_a, **_k): return self
        def insert(self, payload): inserts.append(payload); return self
        def execute(self): return self
    monkeypatch.setattr(writer, "_get_client", lambda: _FakeClient())

    entered = {"count": 0, "max_depth": 0, "depth": 0}
    real_retry = writer.retry_pending_writes

    def _tracked() -> tuple[int, int]:
        entered["count"] += 1
        entered["depth"] += 1
        entered["max_depth"] = max(entered["max_depth"], entered["depth"])
        try:
            return real_retry()
        finally:
            entered["depth"] -= 1
    monkeypatch.setattr(writer, "retry_pending_writes", _tracked)

    assert writer.write_scan(local_id=100, row={"timestamp_est": "t"}) is True

    # The live write triggers exactly one drain; the retries inside it must
    # not re-enter the drain (depth never exceeds 1).
    assert entered["max_depth"] == 1, "dead-letter drain recursed — fd-leak guard is broken"
    # All 3 backlog rows + the live row were inserted; backlog file is gone.
    assert len(inserts) == 4
    assert _remaining_ids(pending_path) == []


def test_drain_is_bounded_per_pass(pending_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A backlog larger than the cap drains gradually; leftovers are kept."""
    n = sw._MAX_DRAIN_PER_PASS + 17
    _seed(pending_path, n)

    writer = sw.SupabaseScannerWriter()
    monkeypatch.setattr(writer, "_local_id_in_cloud", lambda _id: False)

    attempted: list[int] = []

    def _fake_write(local_id, row, from_retry=False):
        attempted.append(local_id)
        return True
    monkeypatch.setattr(writer, "write_scan", _fake_write)

    succeeded, failed = writer.retry_pending_writes()

    assert succeeded == sw._MAX_DRAIN_PER_PASS
    assert len(attempted) == sw._MAX_DRAIN_PER_PASS
    # The 17 over-cap entries survive for the next pass, in order.
    assert _remaining_ids(pending_path) == list(range(sw._MAX_DRAIN_PER_PASS + 1, n + 1))


def test_failed_retries_are_kept_not_lost(pending_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If cloud is still down, failed entries stay in the file (no data loss)."""
    _seed(pending_path, 5)

    writer = sw.SupabaseScannerWriter()
    monkeypatch.setattr(writer, "_local_id_in_cloud", lambda _id: False)
    monkeypatch.setattr(writer, "write_scan", lambda local_id, row, from_retry=False: False)

    succeeded, failed = writer.retry_pending_writes()

    assert succeeded == 0
    assert failed == 5
    assert _remaining_ids(pending_path) == [1, 2, 3, 4, 5]


def test_reentrancy_guard_blocks_nested_drain(pending_path: Path) -> None:
    """While a drain is in progress, a second call is a no-op (returns 0,0)."""
    _seed(pending_path, 2)
    writer = sw.SupabaseScannerWriter()
    writer._draining = True  # simulate "already draining"
    assert writer.retry_pending_writes() == (0, 0)
    # File untouched because we never entered the body.
    assert _remaining_ids(pending_path) == [1, 2]
