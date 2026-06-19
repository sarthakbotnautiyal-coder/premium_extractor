# Premium Extractor

SPX 0DTE option scanner with IBKR connectivity.

## Features

- Polls IBKR for SPX option chains every 60 seconds
- Calculates 0.03-delta spread premiums (10pt and 20pt widths)
- Saves scan results to local SQLite database
- Optional dual-write to Supabase cloud (best-effort)
- Automatic market hours detection

## Installation

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

(`requirements-dev.txt` pulls in `requirements.txt` plus `pytest` and
`pytest-cov`.)

## Configuration

See `config/config.yaml` for detailed options.

## Usage

Requires TWS/IB Gateway running on localhost:7497 (paper) or 4001 (live).

```bash
python run.py
```

## Logging

All log output routes through `src/log_setup.py:get_scanner_logger()`,
which writes to **`logs/<name>.YYYY-MM-DD.log`** (UTC date — one file
per day, automatically rotated when the date changes) and mirrors the
same lines to stdout. The default level is `INFO`, so successful
Supabase dual-writes are visible by default.

To enable verbose debug logs, set the logger level via Python:

```python
import logging
from log_setup import get_scanner_logger
get_scanner_logger("scanner").setLevel(logging.DEBUG)
```

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt   # one-time
.venv/bin/python -m pytest tests/ -v
```

Coverage report (targets `src/log_setup.py` and `src/supabase_writer.py`,
both ≥95%):

```bash
.venv/bin/python -m pytest tests/ --cov=log_setup --cov=supabase_writer --cov-report=term-missing
```

Smoke-test the dual-write path end-to-end without committing the harness:

```bash
# write a one-off validation script in /tmp/, run it, then `trash` it
# do NOT commit validation_test.py or smoke_*.py — they are .gitignore'd
```

## Test layout

```
tests/
├── __init__.py
├── conftest.py                    # sys.path setup for src/ imports
├── test_log_setup.py              # date-prefixed rotation, INFO level,
│                                  # no-duplicate-handlers, day rollover
└── test_supabase_writer_logging.py  # logger routing, _to_cloud_row
                                     # required columns, write_scan paths,
                                     # retry_pending_writes behavior
```
