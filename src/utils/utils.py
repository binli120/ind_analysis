# Copyright (c) 2025 filynai.com
# Author: Bin Lee
# Email: blee@filynai.com

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict


def _extract_event_time(record: Dict[str, Any]) -> str:
    """Extract the ingested timestamp from the record or fall back to now."""
    timestamp = record.get("eventTime")
    if timestamp:
        return timestamp
    return _utc_now()


def _utc_now() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()
