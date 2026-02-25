"""In-process rate limiting helpers for API endpoints."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict, Optional, Tuple

from fastapi import HTTPException, Request

from pdf_analysis.api.constants import (
    LLM_RATE_LIMIT_APPROVE_REQUESTS,
    LLM_RATE_LIMIT_SUMMARY_REQUESTS,
    LLM_RATE_LIMIT_TABULATED_REQUESTS,
    LLM_RATE_LIMIT_WINDOW_SECONDS,
)

_RATE_LIMIT_LOCK = Lock()
_RATE_LIMIT_BUCKETS: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)
_LLM_RATE_LIMIT_POLICIES: Dict[str, Tuple[int, int]] = {
    "summary": (LLM_RATE_LIMIT_SUMMARY_REQUESTS, LLM_RATE_LIMIT_WINDOW_SECONDS),
    "tabulated": (LLM_RATE_LIMIT_TABULATED_REQUESTS, LLM_RATE_LIMIT_WINDOW_SECONDS),
    "summary_approve": (
        LLM_RATE_LIMIT_APPROVE_REQUESTS,
        LLM_RATE_LIMIT_WINDOW_SECONDS,
    ),
}


def _resolve_request_client_id(request: Request) -> str:
    """Resolve request client id."""
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        first_hop = forwarded_for.split(",", 1)[0].strip()
        if first_hop:
            return first_hop
    client_host = request.client.host if request.client else None
    if client_host:
        return client_host
    return "unknown"


def _consume_rate_limit_token(
    *,
    scope: str,
    client_id: str,
    max_requests: int,
    window_seconds: int,
    now: Optional[float] = None,
) -> Optional[int]:
    """Consume rate limit token."""
    if max_requests <= 0 or window_seconds <= 0:
        return None

    current_time = now if now is not None else time.monotonic()
    bucket_key = (scope, client_id)

    with _RATE_LIMIT_LOCK:
        bucket = _RATE_LIMIT_BUCKETS[bucket_key]
        cutoff = current_time - window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= max_requests:
            retry_after = max(1, int(bucket[0] + window_seconds - current_time))
            return retry_after
        bucket.append(current_time)
        return None


def _enforce_llm_endpoint_rate_limit(request: Request, endpoint: str) -> None:
    """Enforce llm endpoint rate limit."""
    policy = _LLM_RATE_LIMIT_POLICIES.get(endpoint)
    if not policy:
        return
    max_requests, window_seconds = policy
    retry_after = _consume_rate_limit_token(
        scope=endpoint,
        client_id=_resolve_request_client_id(request),
        max_requests=max_requests,
        window_seconds=window_seconds,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded for this endpoint.",
            headers={"Retry-After": str(retry_after)},
        )
