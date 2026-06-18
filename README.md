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
.venv/bin/pip install -r requirements.txt
```

## Configuration

See `config/config.yaml` for detailed options.

## Usage

Requires TWS/IB Gateway running on localhost:7497 (paper) or 4001 (live).

```bash
python run.py
```
