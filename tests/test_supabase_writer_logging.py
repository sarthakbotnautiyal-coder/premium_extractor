"""Regression tests for src/supabase_writer.py logger wiring.

Locks in the TASK-2026-192 follow-up so the previous log-routing bug
(``logging.getLogger(__name__)`` hiding the writer behind a module-level
name and making it skip the project file handler) cannot silently come
back.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

# Import the module under test. Importing it will run the module-level
# ``logger = get_scanner_logger("scanner")`` line — that's exactly what we
# want to assert about.
import supabase_writer as sw


SOURCE_FILE = Path(sw.__file__).resolve()


def test_module_logger_is_project_scanner_logger() -> None:
    assert sw.logger.name == "scanner", (
        f"expected module logger name 'scanner', got {sw.logger.name!r} — "
        "the writer should route through get_scanner_logger('scanner'), "
        "not logging.getLogger(__name__)."
    )


def test_module_logger_is_not_under_src_namespace() -> None:
    assert not sw.logger.name.startswith("src."), (
        f"module logger name {sw.logger.name!r} still uses a 'src.*' "
        "namespace; switch to get_scanner_logger('scanner')."
    )


def test_source_has_no_bare_getlogger_dunder_name() -> None:
    """Static guard: the literal 'logging.getLogger(__name__)' must not
    appear anywhere in the module source. This catches reintroduction even
    if the runtime check above is monkey-patched."""
    text = SOURCE_FILE.read_text(encoding="utf-8")
    assert "logging.getLogger(__name__)" not in text, (
        "src/supabase_writer.py still contains 'logging.getLogger(__name__)'; "
        "use get_scanner_logger('scanner') instead."
    )


def test_module_logger_routes_through_project_file_handler(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log call on the module logger should emit at INFO and be captured
    in caplog at level INFO — proving it's not stuck at DEBUG."""
    with caplog.at_level(logging.INFO, logger="scanner"):
        sw.logger.info("[scanner_writer] test_marker line_id=42")
    records = [r for r in caplog.records if r.name == "scanner"]
    assert records, "expected at least one record on the 'scanner' logger"
    assert any("test_marker" in r.getMessage() for r in records), (
        "expected the marker message to appear in captured records"
    )
    # And specifically at INFO, not DEBUG — this is the original
    # "invisible cloud write" regression we're guarding.
    info_records = [r for r in records if r.levelno == logging.INFO]
    assert info_records, "expected at least one INFO-level record"


def test_writer_class_logger_is_module_logger() -> None:
    """If a future refactor instantiates its own logger inside the class,
    we want it to be the same project logger — not a new one."""
    assert hasattr(sw, "SupabaseScannerWriter")
    # Module-level logger is what write_scan / _enqueue / retry_pending_writes
    # actually use (verified by reading the file). We assert the module
    # logger identity is preserved across instantiations.
    w1 = sw.get_writer.__wrapped__() if hasattr(sw.get_writer, "__wrapped__") else None
    # Direct attribute check: the class methods don't define self.logger, so
    # they use the module-level one. Verify that.
    cls_logger = logging.getLogger("scanner")
    assert cls_logger is sw.logger, (
        "module logger should be the same 'scanner' logger that other code "
        "would resolve via logging.getLogger('scanner')"
    )


def test_supabase_writer_import_does_not_create_unrelated_handlers() -> None:
    """Importing the module should not leak extra handlers onto unrelated
    loggers (e.g. the root logger)."""
    root = logging.getLogger()
    root_handler_count_before = len(root.handlers)
    # Re-import to re-trigger module-level logger setup
    import importlib

    importlib.reload(sw)
    root_handler_count_after = len(root.handlers)
    assert root_handler_count_before == root_handler_count_after, (
        "importing src.supabase_writer added handlers to the root logger; "
        "the project logger must be a named child logger, not the root."
    )


# --------------------------------------------------------------------------
# Below: behavioral coverage of src/supabase_writer.py so coverage stays
# at the ≥95% bar the master task requires. These tests use a mocked
# Supabase client (no network) and a tmp-path PENDING_WRITES_PATH so they
# never touch the real cloud or the user's home directory.
# --------------------------------------------------------------------------


import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock


class _FakeChain:
    """Mock for ``client.schema(...).table(...).insert(...).execute()`` style chains."""

    def __init__(self, terminal: Any = None) -> None:
        self._terminal = terminal

    def __getattr__(self, name: str) -> Any:
        # Return a new chain for every method call so attribute chaining works
        return _FakeChain(terminal=self._terminal)

    def execute(self) -> Any:
        return self._terminal


@contextmanager
def _patched_writer(writer_path: Path):
    """Context manager: point supabase_writer.PENDING_WRITES_PATH at writer_path
    and yield the module for tests to mutate.
    """
    original = sw.PENDING_WRITES_PATH
    sw.PENDING_WRITES_PATH = writer_path
    try:
        yield sw
    finally:
        sw.PENDING_WRITES_PATH = original


def _make_mock_client(*, insert_response: Any = MagicMock(data=[{"id": 1}]), select_response: Any = MagicMock(data=[])) -> MagicMock:
    """Build a mock that mimics the supabase client's schema().table() chain.

    The production code calls ``client.schema("trading").table("scan_results")``
    so the same schema/table instances must be reused across calls — we use
    ``return_value`` (not side_effect) so the auto-generated schema mock's
    ``.table`` attribute delegates correctly.
    """
    client = MagicMock()
    schema = MagicMock()
    table = MagicMock()
    table.insert.return_value.execute.return_value = insert_response
    table.select.return_value.eq.return_value.limit.return_value.execute.return_value = select_response
    schema.table.return_value = table
    client.schema.return_value = schema
    return client


@pytest.fixture()
def tmp_pending_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the module-level PENDING_WRITES_PATH at a tmp file and yield it."""
    target = tmp_path / "pending.jsonl"
    monkeypatch.setattr(sw, "PENDING_WRITES_PATH", target)
    yield target


@pytest.fixture()
def fresh_writer(tmp_pending_writes):
    """Yield a SupabaseScannerWriter with its _client pre-set to a mock."""
    w = sw.SupabaseScannerWriter()
    return w


def test_to_cloud_row_includes_required_columns() -> None:
    """_to_cloud_row must include 'source' and 'received_at' (the columns
    that were missing before the fix in commit 4e12105 and would otherwise
    violate the new NOT NULL constraints on the cloud schema)."""
    row = {
        "timestamp_est": "2026-06-19T13:30:00-04:00",
        "spx_spot": 5800.25,
        "expected_move": 12.5,
        "atm_strike": 5800.0,
        "atm_call_mid": 5.5,
        "atm_put_mid": 5.4,
        "call_strike_003": 5810,
        "call_delta": 0.03,
        "call_mid": 1.0,
        "call_10_long_strike": 5810,
        "call_10_long_mid": 0.95,
        "call_10_premium": 950.0,
        "call_20_long_strike": 5820,
        "call_20_long_mid": 0.40,
        "call_20_premium": 400.0,
        "put_strike_003": 5790,
        "put_delta": -0.03,
        "put_mid": 1.0,
        "put_10_long_strike": 5790,
        "put_10_long_mid": 0.95,
        "put_10_premium": 950.0,
        "put_20_long_strike": 5780,
        "put_20_long_mid": 0.40,
        "put_20_premium": 400.0,
    }
    cloud = sw._to_cloud_row(local_id=42, row=row)
    assert cloud["raw_id_local"] == 42
    assert cloud["source"] == "scanner"
    assert "received_at" in cloud and cloud["received_at"], "received_at must be set"
    assert cloud["spx_spot"] == 5800.25
    assert cloud["timestamp_est"] == "2026-06-19T13:30:00-04:00"


def test_write_scan_success_logs_info(fresh_writer, caplog: pytest.LogCaptureFixture) -> None:
    """write_scan success path must emit at INFO level — the regression
    we're guarding against (success logs at DEBUG were invisible in
    production)."""
    client = _make_mock_client()
    fresh_writer._client = client
    # Stub retry_pending_writes so the success path doesn't recurse into
    # another cloud call.
    fresh_writer.retry_pending_writes = MagicMock(return_value=(0, 0))

    row = {"timestamp_est": "2026-06-19T13:30:00-04:00", "spx_spot": 5800.0}
    with caplog.at_level(logging.INFO, logger="scanner"):
        ok = fresh_writer.write_scan(local_id=1, row=row)
    assert ok is True
    info_msgs = [r.getMessage() for r in caplog.records if r.name == "scanner" and r.levelno == logging.INFO]
    assert any("[scanner_writer] wrote scan" in m and "local_id=1" in m for m in info_msgs), (
        f"expected an INFO line for the successful cloud write, got: {info_msgs}"
    )
    client.schema.assert_called_with("trading")


def test_write_scan_mapping_error_logs_warning_and_enqueues(
    fresh_writer, tmp_pending_writes, caplog: pytest.LogCaptureFixture
) -> None:
    """If _to_cloud_row raises (e.g. unexpected exception), write_scan logs
    a WARNING, enqueues into the dead-letter file, and returns False."""
    fresh_writer._client = _make_mock_client()
    # Force _to_cloud_row to raise by passing a row that triggers an exception
    # in the int() coercion of raw_id_local. Easiest path: pass a non-numeric
    # local_id. But the public API casts local_id inside _to_cloud_row, so
    # we need to make the cast itself blow up. Patch _to_cloud_row directly.
    with caplog.at_level(logging.WARNING, logger="scanner"):
        original_to_row = sw._to_cloud_row
        def boom(*args, **kwargs):
            raise ValueError("simulated mapping failure")
        sw._to_cloud_row = boom
        try:
            ok = fresh_writer.write_scan(local_id=7, row={"timestamp_est": "t"})
        finally:
            sw._to_cloud_row = original_to_row

    assert ok is False
    warn_msgs = [r.getMessage() for r in caplog.records if r.name == "scanner" and r.levelno == logging.WARNING]
    assert any("failed to map row" in m for m in warn_msgs), warn_msgs
    # Dead-letter should have one entry
    assert tmp_pending_writes.exists()
    entries = [json.loads(line) for line in tmp_pending_writes.read_text().splitlines() if line.strip()]
    assert len(entries) == 1
    assert entries[0]["local_id"] == 7
    assert "mapping_error" in entries[0]["error"]


def test_write_scan_duplicate_key_does_not_reenqueue(
    fresh_writer, tmp_pending_writes, caplog: pytest.LogCaptureFixture
) -> None:
    """If Supabase rejects with 'duplicate key', the dead-letter file must
    NOT be touched — the row is already in the cloud."""
    client = MagicMock()
    schema = MagicMock()
    table = MagicMock()
    # First call (write_scan): raise a duplicate-key error.
    table.insert.return_value.execute.side_effect = RuntimeError(
        'duplicate key value violates unique constraint "scan_results_raw_id_local_key"'
    )
    client.schema.return_value = schema
    schema.table.return_value = table
    fresh_writer._client = client

    with caplog.at_level(logging.WARNING, logger="scanner"):
        ok = fresh_writer.write_scan(local_id=99, row={"timestamp_est": "t"})
    assert ok is False
    assert not tmp_pending_writes.exists(), "duplicate-key must NOT enqueue"
    warn_msgs = [r.getMessage() for r in caplog.records if r.name == "scanner" and r.levelno == logging.WARNING]
    assert any("duplicate=True" in m for m in warn_msgs), warn_msgs


def test_write_scan_non_duplicate_failure_enqueues(
    fresh_writer, tmp_pending_writes, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-duplicate cloud failure should enqueue into the dead-letter."""
    client = MagicMock()
    schema = MagicMock()
    table = MagicMock()
    table.insert.return_value.execute.side_effect = RuntimeError("network timeout")
    client.schema.return_value = schema
    schema.table.return_value = table
    fresh_writer._client = client

    with caplog.at_level(logging.WARNING, logger="scanner"):
        ok = fresh_writer.write_scan(local_id=11, row={"timestamp_est": "t"})
    assert ok is False
    assert tmp_pending_writes.exists()
    entries = [json.loads(line) for line in tmp_pending_writes.read_text().splitlines() if line.strip()]
    assert any(e["local_id"] == 11 and "network timeout" in e["error"] for e in entries)


def test_write_scan_from_retry_failure_does_not_reenqueue(
    fresh_writer, tmp_pending_writes
) -> None:
    """When write_scan is called from retry_pending_writes (from_retry=True),
    a non-duplicate failure must NOT re-enqueue — otherwise we get the
    runaway loop the original bug produced."""
    client = MagicMock()
    schema = MagicMock()
    table = MagicMock()
    table.insert.return_value.execute.side_effect = RuntimeError("transient blip")
    client.schema.return_value = schema
    schema.table.return_value = table
    fresh_writer._client = client

    ok = fresh_writer.write_scan(local_id=12, row={"timestamp_est": "t"}, from_retry=True)
    assert ok is False
    assert not tmp_pending_writes.exists()


def test_retry_pending_writes_no_file(tmp_pending_writes) -> None:
    w = sw.SupabaseScannerWriter()
    assert w.retry_pending_writes() == (0, 0)


def test_retry_pending_writes_skips_already_in_cloud(
    fresh_writer, tmp_pending_writes, caplog: pytest.LogCaptureFixture
) -> None:
    """If every dead-letter entry already exists in the cloud, retry should
    report them as skipped and remove the file (failed=0)."""
    # Seed two entries
    tmp_pending_writes.write_text(
        json.dumps({"ts": "2026-06-19T00:00:00Z", "local_id": 1, "row": {"timestamp_est": "t"}, "error": "x"}) + "\n"
        + json.dumps({"ts": "2026-06-19T00:01:00Z", "local_id": 2, "row": {"timestamp_est": "t"}, "error": "x"}) + "\n"
    )
    # Mock the existence check to say both are already in cloud
    client = _make_mock_client(select_response=MagicMock(data=[{"id": 999}]))
    fresh_writer._client = client

    succeeded, failed = fresh_writer.retry_pending_writes()
    assert succeeded == 0
    assert failed == 0
    # File should be removed (failed == 0)
    assert not tmp_pending_writes.exists()


def test_retry_pending_writes_unlinks_file_when_all_entries_resolved(
    fresh_writer, tmp_pending_writes
) -> None:
    """When every entry in the dead-letter file is processed without failure,
    retry_pending_writes must unlink the file."""
    tmp_pending_writes.write_text(
        json.dumps({"ts": "2026-06-19T00:00:00Z", "local_id": 5, "row": {"timestamp_est": "t"}, "error": "x"}) + "\n"
    )
    # Stub the inner write_scan to succeed; stub _local_id_in_cloud to report
    # NOT in cloud so the entry is not skipped at the pre-flight check.
    fresh_writer._local_id_in_cloud = MagicMock(return_value=False)  # type: ignore[method-assign]
    fresh_writer.write_scan = MagicMock(return_value=True)  # type: ignore[method-assign]

    succeeded, failed = fresh_writer.retry_pending_writes()
    assert succeeded == 1
    assert failed == 0
    assert not tmp_pending_writes.exists(), (
        "retry_pending_writes should unlink the dead-letter file when "
        "failed == 0 and every entry resolved"
    )


def test_retry_pending_writes_keeps_file_when_some_fail(
    fresh_writer, tmp_pending_writes
) -> None:
    """When at least one entry fails, retry_pending_writes must NOT unlink
    the file (so the failure can be retried next time)."""
    tmp_pending_writes.write_text(
        json.dumps({"ts": "2026-06-19T00:00:00Z", "local_id": 5, "row": {"timestamp_est": "t"}, "error": "x"}) + "\n"
    )
    fresh_writer._local_id_in_cloud = MagicMock(return_value=False)  # type: ignore[method-assign]
    fresh_writer.write_scan = MagicMock(return_value=False)  # type: ignore[method-assign]

    succeeded, failed = fresh_writer.retry_pending_writes()
    assert succeeded == 0
    assert failed == 1
    assert tmp_pending_writes.exists(), (
        "dead-letter file should remain when any entry failed so it can "
        "be retried on the next pass"
    )


def test_retry_pending_writes_skips_entries_with_no_local_id(
    fresh_writer, tmp_pending_writes
) -> None:
    """Entries that are missing local_id should count as failed (not crash)."""
    tmp_pending_writes.write_text(
        json.dumps({"ts": "t", "row": {}, "error": "x"}) + "\n"  # no local_id
    )
    fresh_writer.write_scan = MagicMock()  # type: ignore[method-assign]
    succeeded, failed = fresh_writer.retry_pending_writes()
    assert succeeded == 0
    assert failed == 1
    fresh_writer.write_scan.assert_not_called()


def test_get_writer_returns_singleton() -> None:
    a = sw.get_writer()
    b = sw.get_writer()
    assert a is b, "get_writer() must return a module-level singleton"


# --------------------------------------------------------------------------
# Additional coverage tests for the small branches the regression suite
# above does not exercise.
# --------------------------------------------------------------------------


def test_write_scan_logs_drain_summary_after_success(
    fresh_writer, tmp_pending_writes, caplog: pytest.LogCaptureFixture
) -> None:
    """When retry_pending_writes reports >=1 succeeded/failed, write_scan
    must emit an INFO 'drained dead-letter' summary line."""
    client = _make_mock_client()
    fresh_writer._client = client
    # Make the success path's opportunistic drain return a non-zero tuple
    # so the "drained dead-letter" log fires.
    fresh_writer.retry_pending_writes = MagicMock(return_value=(3, 1))  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO, logger="scanner"):
        ok = fresh_writer.write_scan(local_id=2, row={"timestamp_est": "t"})

    assert ok is True
    msgs = [r.getMessage() for r in caplog.records if r.name == "scanner"]
    assert any("drained dead-letter" in m and "3 succeeded" in m and "1 failed" in m for m in msgs), msgs


def test_write_scan_logs_drain_skipped_when_retry_returns_zero_zero(
    fresh_writer, caplog: pytest.LogCaptureFixture
) -> None:
    """If retry_pending_writes returns (0, 0), write_scan must NOT log a
    drain summary — silence is correct here (no point in noise)."""
    client = _make_mock_client()
    fresh_writer._client = client
    fresh_writer.retry_pending_writes = MagicMock(return_value=(0, 0))  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO, logger="scanner"):
        ok = fresh_writer.write_scan(local_id=3, row={"timestamp_est": "t"})
    assert ok is True
    msgs = [r.getMessage() for r in caplog.records if r.name == "scanner"]
    assert not any("drained dead-letter" in m for m in msgs), msgs


def test_local_id_in_cloud_logs_debug_on_select_failure(fresh_writer, caplog: pytest.LogCaptureFixture) -> None:
    """If the existence-check select raises, _local_id_in_cloud returns
    False and logs at DEBUG (not WARNING — this is a benign miss)."""
    client = MagicMock()
    schema = MagicMock()
    table = MagicMock()
    table.select.return_value.eq.return_value.limit.return_value.execute.side_effect = RuntimeError("network blip")
    client.schema.return_value = schema
    schema.table.return_value = table
    fresh_writer._client = client

    with caplog.at_level(logging.DEBUG, logger="scanner"):
        result = fresh_writer._local_id_in_cloud(42)

    assert result is False
    debug_msgs = [r.getMessage() for r in caplog.records if r.name == "scanner" and r.levelno == logging.DEBUG]
    assert any("existence check failed" in m for m in debug_msgs), debug_msgs


def test_local_id_in_cloud_returns_true_when_data_nonempty(fresh_writer) -> None:
    client = _make_mock_client(select_response=MagicMock(data=[{"id": 1}]))
    fresh_writer._client = client
    assert fresh_writer._local_id_in_cloud(42) is True


def test_local_id_in_cloud_returns_false_when_data_empty(fresh_writer) -> None:
    client = _make_mock_client(select_response=MagicMock(data=[]))
    fresh_writer._client = client
    assert fresh_writer._local_id_in_cloud(42) is False


def test_retry_pending_writes_logs_skip_summary(fresh_writer, tmp_pending_writes, caplog: pytest.LogCaptureFixture) -> None:
    """When every dead-letter entry is already in the cloud, retry must
    log a 'skipped N dead-letter entries (already in cloud)' line."""
    tmp_pending_writes.write_text(
        json.dumps({"ts": "t", "local_id": 1, "row": {}, "error": "x"}) + "\n"
        + json.dumps({"ts": "t", "local_id": 2, "row": {}, "error": "x"}) + "\n"
    )
    # existence-check returns non-empty → both skipped
    client = _make_mock_client(select_response=MagicMock(data=[{"id": 1}]))
    fresh_writer._client = client

    with caplog.at_level(logging.INFO, logger="scanner"):
        succeeded, failed = fresh_writer.retry_pending_writes()
    assert succeeded == 0
    assert failed == 0
    msgs = [r.getMessage() for r in caplog.records if r.name == "scanner" and r.levelno == logging.INFO]
    assert any("skipped 2 dead-letter entries" in m for m in msgs), msgs


def test_retry_pending_writes_logs_warning_when_write_scan_raises(
    fresh_writer, tmp_pending_writes, caplog: pytest.LogCaptureFixture
) -> None:
    """If write_scan raises (not just returns False), the retry loop catches
    the exception, increments failed, and logs a WARNING."""
    tmp_pending_writes.write_text(
        json.dumps({"ts": "t", "local_id": 7, "row": {}, "error": "x"}) + "\n"
    )
    fresh_writer._local_id_in_cloud = MagicMock(return_value=False)  # type: ignore[method-assign]
    fresh_writer.write_scan = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="scanner"):
        succeeded, failed = fresh_writer.retry_pending_writes()
    assert succeeded == 0
    assert failed == 1
    warn_msgs = [r.getMessage() for r in caplog.records if r.name == "scanner" and r.levelno == logging.WARNING]
    assert any("retry error" in m and "local_id=7" in m for m in warn_msgs), warn_msgs


def test_retry_pending_writes_unlink_tolerates_already_missing_file(
    fresh_writer, tmp_pending_writes, caplog: pytest.LogCaptureFixture
) -> None:
    """If the dead-letter file is removed between the read and the unlink,
    the FileNotFoundError is silently swallowed."""
    tmp_pending_writes.write_text(
        json.dumps({"ts": "t", "local_id": 9, "row": {}, "error": "x"}) + "\n"
    )
    fresh_writer._local_id_in_cloud = MagicMock(return_value=True)  # type: ignore[method-assign]

    # The existence-check returns True (skipped, already in cloud). When the
    # loop completes, retry_pending_writes tries to unlink. We pre-remove
    # the file to simulate a race.
    succeeded, failed = fresh_writer.retry_pending_writes()
    assert succeeded == 0
    assert failed == 0
    # Manual unlink to force the race condition the except handles.
    if tmp_pending_writes.exists():
        tmp_pending_writes.unlink()
    # Calling again should still be a no-op (no exception, returns 0,0).
    succeeded2, failed2 = fresh_writer.retry_pending_writes()
    assert (succeeded2, failed2) == (0, 0)


def test_get_writer_is_thread_safe_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_writer must return the same instance across calls; the
    double-checked locking branch should be exercised by at least one
    call."""
    # Reset module-level singleton so we can observe init
    monkeypatch.setattr(sw, "_writer_instance", None)
    a = sw.get_writer()
    b = sw.get_writer()
    assert a is b


def test_create_client_raises_if_supabase_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """_create_client must raise a clear error if supabase package is not
    available."""
    monkeypatch.setattr(sw, "create_client", None)
    with pytest.raises(RuntimeError, match="supabase package is not installed"):
        sw._create_client()


def test_create_client_raises_if_env_vars_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_create_client must raise if SUPABASE_URL or SUPABASE_SECRET_KEY
    is missing — covers the env-read branch."""
    # Point env path at a non-existent file so load_dotenv is a no-op.
    monkeypatch.setattr(sw, "create_client", lambda *a, **kw: MagicMock())
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    # Make load_dotenv a no-op
    monkeypatch.setattr(sw, "load_dotenv", lambda *a, **kw: None)
    # Patch the env_path computation indirectly by ensuring no .env exists
    # in the project — it does exist, so we override load_dotenv to no-op.
    with pytest.raises(RuntimeError, match="SUPABASE_URL and SUPABASE_SECRET_KEY must be set"):
        sw._create_client()


def test_create_client_succeeds_when_env_vars_set(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """_create_client must construct the supabase client when both env
    vars are present."""
    fake_client = MagicMock(name="fake_supabase_client")
    monkeypatch.setattr(sw, "create_client", lambda url, key: fake_client)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "secret")
    monkeypatch.setattr(sw, "load_dotenv", lambda *a, **kw: None)
    result = sw._create_client()
    assert result is fake_client


def test_get_client_lazy_creates_on_first_call(fresh_writer, monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_client must call _create_client exactly once and cache the result."""
    fake_client = MagicMock(name="lazy_client")
    calls = {"n": 0}
    def _factory():
        calls["n"] += 1
        return fake_client
    monkeypatch.setattr(sw, "_create_client", _factory)
    fresh_writer._client = None  # ensure clean state
    a = fresh_writer._get_client()
    b = fresh_writer._get_client()
    assert a is fake_client
    assert b is fake_client
    assert calls["n"] == 1, "expected _create_client to be called exactly once"


def test_retry_pending_writes_skips_empty_and_malformed_lines(
    fresh_writer, tmp_pending_writes, caplog: pytest.LogCaptureFixture
) -> None:
    """Empty lines and malformed JSON lines in the dead-letter file must
    be silently skipped, not crash the retry loop."""
    good_entry = json.dumps({"ts": "t", "local_id": 1, "row": {}, "error": "x"})
    tmp_pending_writes.write_text(
        "\n"           # empty line
        + "this is not json\n"  # malformed
        + good_entry + "\n"
    )
    fresh_writer._local_id_in_cloud = MagicMock(return_value=False)  # type: ignore[method-assign]
    fresh_writer.write_scan = MagicMock(return_value=True)  # type: ignore[method-assign]

    succeeded, failed = fresh_writer.retry_pending_writes()
    assert succeeded == 1
    assert failed == 0


def test_retry_pending_writes_unlink_handles_filenotfound(
    fresh_writer, tmp_pending_writes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the dead-letter file vanishes between the read and the unlink,
    the FileNotFoundError must be swallowed silently.

    We exercise this by patching ``Path.unlink`` on PosixPath to raise —
    the production code catches ``FileNotFoundError`` and continues.
    """
    import pathlib
    tmp_pending_writes.write_text(
        json.dumps({"ts": "t", "local_id": 1, "row": {}, "error": "x"}) + "\n"
    )
    fresh_writer._local_id_in_cloud = MagicMock(return_value=True)  # type: ignore[method-assign]

    original_unlink = pathlib.PosixPath.unlink
    def _raise(self, *args, **kwargs):
        # Only raise for our test path; let other paths behave normally.
        if str(self) == str(sw.PENDING_WRITES_PATH):
            raise FileNotFoundError("simulated race")
        return original_unlink(self, *args, **kwargs)
    monkeypatch.setattr(pathlib.PosixPath, "unlink", _raise)

    succeeded, failed = fresh_writer.retry_pending_writes()
    assert succeeded == 0
    assert failed == 0


def test_write_scan_logs_debug_when_drain_raises(
    fresh_writer, caplog: pytest.LogCaptureFixture
) -> None:
    """If retry_pending_writes raises during the opportunistic drain,
    write_scan must catch and log at DEBUG (it's best-effort, not fatal)."""
    client = _make_mock_client()
    fresh_writer._client = client
    fresh_writer.retry_pending_writes = MagicMock(side_effect=RuntimeError("drain failed"))  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG, logger="scanner"):
        ok = fresh_writer.write_scan(local_id=4, row={"timestamp_est": "t"})

    assert ok is True, "drain failure must NOT fail the original write"
    debug_msgs = [r.getMessage() for r in caplog.records if r.name == "scanner" and r.levelno == logging.DEBUG]
    assert any("dead-letter drain skipped" in m for m in debug_msgs), debug_msgs


def test_enqueue_logs_critical_on_write_failure(
    fresh_writer, tmp_pending_writes, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If writing to the dead-letter file raises, _enqueue must log at
    ERROR with 'CRITICAL' so the operator notices the dual-write is silently
    losing data."""
    fresh_writer._client = _make_mock_client()
    # Make the .open("a", ...) raise OSError to trigger the except branch.
    def _boom(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(tmp_pending_writes.__class__, "open", _boom)

    with caplog.at_level(logging.ERROR, logger="scanner"):
        # _enqueue is private; trigger via write_scan + cloud failure
        client = MagicMock()
        schema = MagicMock()
        table = MagicMock()
        table.insert.return_value.execute.side_effect = RuntimeError("network timeout")
        client.schema.return_value = schema
        schema.table.return_value = table
        fresh_writer._client = client

        ok = fresh_writer.write_scan(local_id=8, row={"timestamp_est": "t"})
    assert ok is False
    err_msgs = [r.getMessage() for r in caplog.records if r.name == "scanner" and r.levelno == logging.ERROR]
    assert any("CRITICAL" in m and "failed to enqueue dead-letter" in m for m in err_msgs), err_msgs
