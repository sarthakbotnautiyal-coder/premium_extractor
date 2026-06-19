"""Pytest configuration for premium_extractor.

Adds the project root to sys.path so tests can ``from src.X import Y``
without an install step.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Make ``src/`` importable as a top-level package (e.g. ``from log_setup
# import get_scanner_logger``) without an editable install.
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
