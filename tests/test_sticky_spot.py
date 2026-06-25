"""Regression tests for the sticky-value SPX spot guard in src/ibkr_scanner.py.

Background (2026-06-25): the IBKR SPX index feed got stuck reprinting an exact
stale level (7352.36) ~5pts off the true price. The existing guards missed it —
SPOT_STALENESS_SECS needs the value unchanged for 120s (small jitters reset the
timer) and TV_CROSSCHECK_DIVERGENCE only fires above 15pts. The sticky guard
catches an index feed that returns the identical value on two consecutive scans.
"""

from __future__ import annotations

import math

import ibkr_scanner as scanner
from ibkr_scanner import ScanCache, _is_sticky_spot


def test_first_scan_is_never_sticky() -> None:
    # No prior reading -> cannot be sticky.
    assert _is_sticky_spot(7352.36, None) is False


def test_identical_consecutive_values_are_sticky() -> None:
    assert _is_sticky_spot(7352.36, 7352.36) is True


def test_changed_value_is_not_sticky() -> None:
    assert _is_sticky_spot(7352.36, 7351.45) is False


def test_nan_readings_are_not_sticky() -> None:
    assert _is_sticky_spot(float("nan"), 7352.36) is False
    assert _is_sticky_spot(7352.36, float("nan")) is False


def test_cache_tracks_prev_primary_spot_across_scans() -> None:
    """Simulate the run_scan bookkeeping: stash the raw index reading each scan
    and confirm a repeat is flagged on the *second* identical reading, not the
    first."""
    cache = ScanCache()
    assert cache.prev_primary_spot is None

    # Scan 1: first reading, nothing to compare against.
    s1 = 7352.36
    assert _is_sticky_spot(s1, cache.prev_primary_spot) is False
    cache.prev_primary_spot = s1

    # Scan 2: identical reading -> sticky.
    s2 = 7352.36
    assert _is_sticky_spot(s2, cache.prev_primary_spot) is True
    cache.prev_primary_spot = s2

    # Scan 3: feed moves -> healthy again.
    s3 = 7353.10
    assert _is_sticky_spot(s3, cache.prev_primary_spot) is False


def test_reset_clears_prev_primary_spot() -> None:
    cache = ScanCache()
    cache.prev_primary_spot = 7352.36
    cache.reset()
    assert cache.prev_primary_spot is None


def test_guard_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setattr(scanner, "STICKY_SPOT_GUARD", False)
    assert _is_sticky_spot(7352.36, 7352.36) is False
