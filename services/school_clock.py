"""Best-effort clock sample from the school's HTTP Date response header."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from math import isfinite

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
MAX_SAMPLE_AGE_SECONDS = 600
MAX_REQUEST_SECONDS = 10
MAX_WALL_DRIFT_SECONDS = 10
_lock = threading.Lock()
_sample: tuple[datetime, float, float, float] | None = None


def observe(
    date_header: str | None,
    received_at: float,
    *,
    sent_at: float | None = None,
    age_header: str | None = None,
) -> None:
    """Anchor school time to a monotonic clock when the header is usable."""
    if not date_header:
        return
    try:
        sent_at = received_at if sent_at is None else sent_at
        if (
            not isfinite(received_at)
            or not isfinite(sent_at)
            or not 0 <= received_at - sent_at <= MAX_REQUEST_SECONDS
            or float(age_header or 0) != 0
        ):
            return
        server_time = parsedate_to_datetime(date_header)
        if server_time.tzinfo is None or not 2000 <= server_time.year <= 2100:
            return
        server_time = server_time.astimezone(UTC)
    except (TypeError, ValueError, IndexError, OverflowError):
        return
    with _lock:
        global _sample
        if _sample is None or sent_at >= _sample[2]:
            _sample = (server_time, received_at, sent_at, time.time())


def reset() -> None:
    global _sample
    with _lock:
        _sample = None


def now() -> tuple[datetime, str]:
    """Return current UTC time and the source used for scheduling."""
    with _lock:
        sample = _sample
    if sample:
        elapsed = time.monotonic() - sample[1]
        wall_elapsed = time.time() - sample[3]
        # Some OS monotonic clocks exclude suspend time. Also invalidate
        # samples after a significant system-clock adjustment.
        if (
            0 <= elapsed <= MAX_SAMPLE_AGE_SECONDS
            and abs(wall_elapsed - elapsed) <= MAX_WALL_DRIFT_SECONDS
        ):
            return sample[0] + timedelta(seconds=elapsed), "school"
    return datetime.now(UTC), "local"
