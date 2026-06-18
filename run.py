#!/usr/bin/env python3
"""Premium Extractor - SPX option scanner service."""

import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from log_setup import get_scanner_logger

log = get_scanner_logger("premium_extractor")


def main():
    log.info("🚀 Premium Extractor started")
    log.info("Note: Full IBKR scanner implementation from monolith needs to be ported here")
    
    def handle_sigterm(signum, frame):
        log.info("SIGTERM received, shutting down...")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        # Placeholder: In Phase 2, integrate full scan_premiums.py logic
        while True:
            log.info("Scanner running (placeholder)")
            import time
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Interrupted, shutting down...")


if __name__ == "__main__":
    main()
