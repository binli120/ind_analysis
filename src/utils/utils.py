from datetime import timezone, datetime


def _extract_event_time(record: Dict[str, Any]) -> str:
    """Extract the ingested timestamp from the record or fall back to now."""
    timestamp = record.get("eventTime")
    if timestamp:
        return timestamp
    return _utc_now()


def _utc_now() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()