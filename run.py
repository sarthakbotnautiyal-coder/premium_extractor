#!/usr/bin/env python3
"""Premium Extractor - SPX option scanner service."""

import signal
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

# Import config before ibkr_scanner
from config import CONFIG

def _handle_sigterm(signum, frame):
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, _handle_sigterm)

# Run the scanner - it contains the main polling loop
if __name__ == "__main__":
    from ibkr_scanner import main
    main()
