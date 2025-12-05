"""
@author: Bin Lee
@email: blee@filynai.com

Centralized logging helpers shared across scripts and services.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

DEFAULT_LOG_LEVEL = "DEBUG"
LOG_ENV_VAR = "LOG_LEVEL"
DEFAULT_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

log_level_name: str | None = os.getenv(LOG_ENV_VAR, DEFAULT_LOG_LEVEL).upper()
if log_level_name is None:
    log_level_name = DEFAULT_LOG_LEVEL

try:
    LOG_NUMERIC_LEVEL = logging.getLevelName(log_level_name)
except Exception as exc:
    raise ValueError(f"Invalid LOG_LEVEL {log_level_name!r}") from exc

_logger = logging.getLogger("utils")

logging.basicConfig(
    level=LOG_NUMERIC_LEVEL,
    format=DEFAULT_LOG_FORMAT,
    datefmt=DATE_FORMAT,
    stream=sys.stdout,
)


def _log(
    level: int, *args: Any, sep: str = " ", end: str = "\n", **kwargs: Any
) -> None:
    """Internal helper that formats arguments exactly as print() does."""

    # Recreate the print output
    message = sep.join(str(a) for a in args) + end
    # ``logging`` strips the trailing newline automatically, so we just log.
    _logger.log(level, message.rstrip("\n"))


def debug(*args: Any, **kwargs: Any) -> None:
    """Log at DEBUG level."""
    _log(logging.DEBUG, *args, **kwargs)


def info(*args: Any, **kwargs: Any) -> None:
    """Log at INFO level."""
    _log(logging.INFO, *args, **kwargs)


def warning(*args: Any, **kwargs: Any) -> None:
    """Log at WARNING level."""
    _log(logging.WARNING, *args, **kwargs)


def error(*args: Any, **kwargs: Any) -> None:
    """Log at ERROR level."""
    _log(logging.ERROR, *args, **kwargs)
