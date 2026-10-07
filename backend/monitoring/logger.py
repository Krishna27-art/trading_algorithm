"""
Structured logging framework for trading operations, order routing, and trade journaling.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


class TradeLogFormatter(logging.Formatter):
    """Custom color-coded formatter for clean terminal output."""
    grey = "\x1b[38;20m"
    cyan = "\x1b[36;20m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    bold_red = "\x1b[31;1m"
    green = "\x1b[32;20m"
    reset = "\x1b[0m"
    log_format = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"

    FORMATS = {
        logging.DEBUG: grey + log_format + reset,
        logging.INFO: cyan + log_format + reset,
        logging.WARNING: yellow + log_format + reset,
        logging.ERROR: red + log_format + reset,
        logging.CRITICAL: bold_red + log_format + reset,
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno, self.log_format)
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)


def setup_logger(name: str = "trading_engine", log_dir: Optional[Path] = None, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.hasHandlers():
        return logger

    logger.setLevel(level)

    # Console Handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(TradeLogFormatter())
    logger.addHandler(ch)

    # File Handler
    if log_dir is None:
        log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    fh = logging.FileHandler(log_dir / f"trading_{today}.log")
    fh.setLevel(level)
    file_format = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
    fh.setFormatter(file_format)
    logger.addHandler(fh)

    return logger


logger = setup_logger("algo_engine")
