"""Regression tests for src/log_setup.py.

Locks in:
- date-prefixed log filename pattern (logs/<name>.YYYY-MM-DD.log)
- file handler at INFO (not DEBUG, not WARNING) so Supabase success lines
  are visible by default
- console handler at INFO
- two calls with the same name return the same logger instance with no
  duplicate handlers
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
from pathlib import Path

import pytest

from log_setup import get_scanner_logger


DATE_PATTERN = re.compile(r"^.*[\\/].+\.\d{4}-\d{2}-\d{2}\.log$")


def _file_handlers(logger: logging.Logger) -> list[logging.FileHandler]:
    return [h for h in logger.handlers if isinstance(h, logging.FileHandler)]


def _console_handlers(logger: logging.Logger) -> list[logging.StreamHandler]:
    return [h for h in logger.handlers if isinstance(h, logging.StreamHandler)]


@pytest.fixture()
def fresh_logger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Return a logger backed by tmp_path as its log_dir.

    Cleans up any handlers it added to avoid cross-test leakage (the module
    uses a module-level ``logging.getLogger`` so handlers are cached on the
    global registry by name).
    """
    # Use a name unique to this test invocation to avoid colliding with the
    # default "scanner" logger that src/supabase_writer.py imports at module
    # load time.
    name = f"test_observer_{id(fresh_logger)}"
    logger = get_scanner_logger(name, log_dir=str(tmp_path))
    yield logger, name, tmp_path

    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def test_creates_date_prefixed_log_file(fresh_logger) -> None:
    logger, name, tmp_path = fresh_logger
    # Trigger at least one log line so the file is flushed
    logger.info("hello")

    expected = tmp_path / f"{name}.{datetime.now(_ET).strftime('%Y-%m-%d')}.log"
    assert expected.exists(), f"expected log file {expected} to exist"
    assert DATE_PATTERN.match(str(expected)), (
        f"log filename {expected!s} does not match YYYY-MM-DD pattern"
    )


def test_no_bare_unprefixed_log_file(fresh_logger) -> None:
    logger, name, tmp_path = fresh_logger
    logger.info("hello")
    bare = tmp_path / f"{name}.log"
    assert not bare.exists(), (
        f"unexpected bare log file {bare} — date-prefix rotation should "
        "have produced a .YYYY-MM-DD.log file instead"
    )


def test_file_handler_is_at_info(fresh_logger) -> None:
    logger, _name, _tmp_path = fresh_logger
    handlers = _file_handlers(logger)
    assert handlers, "expected at least one FileHandler"
    for h in handlers:
        assert h.level == logging.INFO, (
            f"file handler level should be INFO (was {logging.getLevelName(h.level)})"
        )


def test_console_handler_is_at_info(fresh_logger) -> None:
    logger, _name, _tmp_path = fresh_logger
    handlers = _console_handlers(logger)
    assert handlers, "expected at least one StreamHandler (console)"
    for h in handlers:
        assert h.level == logging.INFO, (
            f"console handler level should be INFO (was {logging.getLevelName(h.level)})"
        )


def test_same_name_returns_same_logger_with_no_duplicate_handlers(tmp_path: Path) -> None:
    name = f"test_dedup_{id(test_same_name_returns_same_logger_with_no_duplicate_handlers)}"
    a = get_scanner_logger(name, log_dir=str(tmp_path))
    b = get_scanner_logger(name, log_dir=str(tmp_path))
    assert a is b, "get_scanner_logger must return the same Logger instance for the same name"
    file_handlers = _file_handlers(a)
    assert len(file_handlers) == 1, (
        f"expected exactly one FileHandler after repeated calls, got {len(file_handlers)}"
    )

    # cleanup
    for h in list(a.handlers):
        a.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def test_logger_level_is_info(fresh_logger) -> None:
    logger, _name, _tmp_path = fresh_logger
    assert logger.level == logging.INFO


def test_handles_rollover_when_date_changes(tmp_path: Path) -> None:
    """If a stale FileHandler is left over for a previous date, get_scanner_logger
    must swap it out for one matching today's Eastern date on the next call.
    """
    name = f"test_rollover_{id(test_handles_rollover_when_date_changes)}"
    # First call — installs today's handler
    logger = get_scanner_logger(name, log_dir=str(tmp_path))
    today_file = _file_handlers(logger)[0].baseFilename
    today_str = datetime.now(_ET).strftime("%Y-%m-%d")
    assert today_str in today_file

    # Simulate a date rollover by replacing the FileHandler with one pointing
    # at a previous day's filename (mimicking what would happen if the
    # process stayed up across Eastern midnight without our swap logic).
    import log_setup as ls

    stale_date = "2000-01-01"
    stale_path = tmp_path / f"{name}.{stale_date}.log"
    old_handler = _file_handlers(logger)[0]
    logger.removeHandler(old_handler)
    old_handler.close()
    new_stale = logging.FileHandler(stale_path)
    new_stale.setLevel(logging.INFO)
    logger.addHandler(new_stale)

    # Next call must detect the mismatch and replace the handler.
    logger2 = get_scanner_logger(name, log_dir=str(tmp_path))
    assert logger2 is logger
    after_file = _file_handlers(logger2)[0].baseFilename
    assert today_str in after_file, (
        f"file handler should have been rotated back to today's date; got {after_file}"
    )

    # cleanup
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def test_ensure_handler_installs_when_only_streamhandler_present(tmp_path: Path) -> None:
    """If a logger already has handlers (e.g. a console handler from a prior
    call) but no FileHandler, the next call to get_scanner_logger must
    install a date-prefixed FileHandler without duplicating anything.
    """
    name = f"test_no_file_handler_{id(test_ensure_handler_installs_when_only_streamhandler_present)}"

    # Pre-create a logger with only a StreamHandler — bypasses get_scanner_logger
    # so we control the handler set exactly.
    bare = logging.getLogger(name)
    bare.setLevel(logging.INFO)
    bare.handlers.clear()
    stream = logging.StreamHandler()
    stream.setLevel(logging.INFO)
    bare.addHandler(stream)

    try:
        result = get_scanner_logger(name, log_dir=str(tmp_path))
        assert result is bare
        file_handlers = _file_handlers(result)
        assert len(file_handlers) == 1, (
            f"expected exactly one FileHandler after installing from a "
            f"no-file-handler state, got {len(file_handlers)}"
        )
        today_str = datetime.now(_ET).strftime("%Y-%m-%d")
        assert today_str in file_handlers[0].baseFilename
        # StreamHandler preserved
        assert _console_handlers(result), "console handler should have been preserved"
    finally:
        for h in list(bare.handlers):
            bare.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


def test_recovers_when_existing_handler_basefilename_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If resolving the existing FileHandler's baseFilename raises (e.g.
    the handler was constructed against a path that's since been moved),
    _ensure_current_day_handler must catch and fall back to installing a
    fresh handler.
    """
    name = f"test_unresolvable_{id(test_recovers_when_existing_handler_basefilename_unresolvable)}"

    logger = get_scanner_logger(name, log_dir=str(tmp_path))
    existing = _file_handlers(logger)[0]

    # Force Path(...).resolve() to raise to trigger the except branch.
    import log_setup  # noqa: PLC0415  (local import to ensure patched module is the live one)

    def _boom(_self, *args, **kwargs):  # noqa: ANN001
        raise OSError("simulated unresolvable path")

    monkeypatch.setattr(log_setup.Path, "resolve", _boom)

    try:
        # Second call — should hit the except branch and install a fresh handler.
        result = get_scanner_logger(name, log_dir=str(tmp_path))
        assert result is logger
        file_handlers = _file_handlers(result)
        # The stale handler was closed and replaced with a fresh one.
        assert len(file_handlers) == 1
        assert file_handlers[0] is not existing, (
            "stale FileHandler should have been replaced after unresolvable path"
        )
    finally:
        for h in list(logger.handlers):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
