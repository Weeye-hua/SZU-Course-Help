"""One in-memory, account-bound undergraduate enrollment appointment."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from services import school_clock

BEIJING = ZoneInfo("Asia/Shanghai")
_lock = threading.RLock()
_wake = threading.Event()
_generation = 0
_state = {"status": "idle", "target_at": "", "student_id": "", "mode": "", "message": ""}


def status() -> dict[str, str]:
    with _lock:
        state = dict(_state)
    current, source = school_clock.now()
    return {
        **state,
        "current_time": current.astimezone(BEIJING).isoformat(timespec="seconds"),
        "clock_source": source,
    }


def parse_target(value: str) -> datetime:
    """Require an explicit Beijing offset; never interpret browser local time."""
    try:
        target = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("请输入有效的北京时间") from exc
    if target.tzinfo is None or target.utcoffset() != timedelta(hours=8):
        raise ValueError("预约时间必须带有北京时间 +08:00 时区")
    target = target.astimezone(UTC)
    current, _ = school_clock.now()
    if not timedelta(seconds=30) <= target - current <= timedelta(days=7):
        raise ValueError("预约时间须在 30 秒至 7 天后")
    return target


def arm(
    target: datetime,
    student_id: str,
    start: Callable[[], tuple[bool, str]],
    sync_clock: Callable[[], datetime | None] | None = None,
    mode: str = "custom",
) -> dict[str, str]:
    global _generation
    with _lock:
        if _state["status"] == "starting":
            raise RuntimeError("预约正在启动，请稍候")
        _generation += 1
        generation = _generation
        _state.update(
            status="armed",
            target_at=target.astimezone(BEIJING).isoformat(timespec="seconds"),
            student_id=student_id,
            mode=mode,
            message="已预约，到点后会重新核验学校批次并启动",
        )
        _wake.set()
    threading.Thread(
        target=_run,
        args=(generation, target, start, sync_clock, mode),
        name="scheduled-enrollment",
        daemon=True,
    ).start()
    return status()


def cancel() -> bool:
    global _generation
    with _lock:
        if _state["status"] != "armed":
            return False
        _generation += 1
        _state.update(status="idle", target_at="", student_id="", mode="", message="预约已取消")
        _wake.set()
        return True


def _run(
    generation: int,
    target: datetime,
    start: Callable[[], tuple[bool, str]],
    sync_clock: Callable[[], datetime | None] | None,
    mode: str,
) -> None:
    synced_near_target = False
    next_sync_at = time.monotonic() + 300

    def refresh_target() -> None:
        nonlocal synced_near_target, target
        if sync_clock is None:
            return
        with suppress(Exception):
            updated = sync_clock()
            if mode != "school" or updated is None or updated == target or updated.tzinfo is None:
                return
            with _lock:
                if generation != _generation:
                    return
                target = updated.astimezone(UTC)
                if (target - school_clock.now()[0]).total_seconds() > 120:
                    synced_near_target = False
                _state.update(
                    target_at=target.astimezone(BEIJING).isoformat(timespec="seconds"),
                    message="学校开抢时间已更新，预约已同步调整",
                )

    while True:
        with _lock:
            if generation != _generation:
                return
        current, _ = school_clock.now()
        remaining = (target - current).total_seconds()
        if remaining <= 0:
            break
        if sync_clock and time.monotonic() >= next_sync_at:
            next_sync_at = time.monotonic() + 300
            refresh_target()
            continue
        if sync_clock and 30 < remaining <= 120 and not synced_near_target:
            synced_near_target = True
            refresh_target()
            continue
        _wake.wait(min(remaining, 30.0 if remaining > 120 else 1.0 if remaining > 5 else 0.1))
        _wake.clear()
    with _lock:
        if generation != _generation:
            return
        _state.update(status="starting", message="到点，正在核验学校状态")
    try:
        success, message = start()
    except Exception:
        success, message = False, "预约启动失败，请手动检查登录和学校状态"
    with _lock:
        if generation == _generation:
            _state.update(status="started" if success else "failed", message=message)
