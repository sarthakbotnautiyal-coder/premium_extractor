"""Logging setup for premium_extractor."""

import logging
from pathlib import Path


def get_scanner_logger(name: str, log_dir: str = "logs") -> logging.Logger:
    """Get a configured logger with daily rotation."""
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True, parents=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    handler = logging.FileHandler(log_path / f"{name}.log")
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    return logger
