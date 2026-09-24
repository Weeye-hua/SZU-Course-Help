"""Best-effort clock sample from the school's HTTP Date response header."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

_lock = threading.Lock()
_sample: tuple[datetime, float] | None = None


def observe(date_header: str | None, received_at: float) -> None:
    """Anchor school time to a monotonic clock when the header is usable."""
    if not date_header:
        return
    try:
        server_time = parsedate_to_datetime(date_header)
        if server_time.tzinfo is None:
            return
        server_time = server_time.astimezone(UTC)
    except (TypeError, ValueError, IndexError):
        return
    with _lock:
        global _sample
        _sample = (server_time, received_at)


def now() -> tuple[datetime, str]:
    """Return current UTC time and the source used for scheduling."""
    with _lock:
        sample = _sample
    if sample:
        return sample[0] + timedelta(seconds=max(0, time.monotonic() - sample[1])), "school"
    return datetime.now(UTC), "local"
