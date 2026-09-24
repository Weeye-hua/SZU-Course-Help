"""One cancellable, process-local undergraduate enrollment appointment."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from services import school_clock

BEIJING = school_clock.BEIJING
MAX_DELAY = timedelta(days=7)
MAX_LATENESS = timedelta(minutes=2)
logger = logging.getLogger(__name__)
_lock = threading.RLock()
_generation = 0
_instance_id = secrets.token_hex(16)
_snapshot_sequence = 0
_shutdown = False
_wake: threading.Event | None = None
_state: dict[str, Any] = {
    "status": "idle",
    "target_at": "",
    "student_id": "",
    "mode": "",
    "message": "",
    "batch_code": "",
    "batch_name": "",
    "sync_message": "",
}


class ScheduleConflictError(RuntimeError):
    """The user's appointment revision is no longer current."""


class ScheduleInvalidatedError(RuntimeError):
    """An account, batch, cancellation or shutdown invalidated this appointment."""


def status() -> dict[str, Any]:
    global _snapshot_sequence
    with _lock:
        _snapshot_sequence += 1
        state = {
            **_state,
            "revision": _generation,
            "instance_id": _instance_id,
            "snapshot_sequence": _snapshot_sequence,
        }
    current, source = school_clock.now()
    return {
        **state,
        "current_time": current.astimezone(BEIJING).isoformat(timespec="milliseconds"),
        "clock_source": source,
    }


def is_active() -> bool:
    with _lock:
        return _state["status"] in {"armed", "starting"}


def parse_target(value: str) -> datetime:
    """Require explicit Beijing time without depending on OS timezone data."""
    try:
        target = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("请输入有效的北京时间") from exc
    if target.tzinfo is None or target.utcoffset() != timedelta(hours=8):
        raise ValueError("预约时间必须带有北京时间 +08:00 时区")
    target = target.astimezone(UTC)
    current, _ = school_clock.now()
    if not timedelta(seconds=30) <= target - current <= MAX_DELAY:
        raise ValueError("预约时间须在 30 秒至 7 天后")
    return target


def _check_revision(expected_revision: int | None, expected_instance: str | None = None) -> None:
    if expected_instance is not None and expected_instance != _instance_id:
        raise ScheduleConflictError("程序已重新启动，请刷新后重新确认预约")
    if expected_revision is not None and expected_revision != _generation:
        raise ScheduleConflictError("预约已在其他操作中更新，请刷新后重新确认")


def cancel(
    *,
    expected_revision: int | None = None,
    expected_instance: str | None = None,
    message: str = "预约已取消",
) -> bool:
    global _generation
    with _lock:
        _check_revision(expected_revision, expected_instance)
        active = _state["status"] in {"armed", "starting"}
        if not active and expected_revision is not None:
            return False
        _generation += 1
        if _wake is not None:
            _wake.set()
        _state.update(
            status="idle",
            target_at="",
            student_id="",
            mode="",
            message=message,
            batch_code="",
            batch_name="",
            sync_message="",
        )
        return active


def startup() -> None:
    global _shutdown
    with _lock:
        _shutdown = False


def shutdown() -> None:
    global _shutdown
    with _lock:
        _shutdown = True
        cancel(message="程序已退出，预约已取消")


def arm(
    target: datetime,
    student_id: str,
    start: Callable[[int], tuple[bool, str]],
    sync_clock: Callable[[], datetime | None] | None = None,
    mode: str = "custom",
    *,
    expected_revision: int | None = None,
    expected_instance: str | None = None,
    batch_code: str = "",
    batch_name: str = "",
) -> dict[str, Any]:
    global _generation, _wake
    if target.tzinfo is None or mode not in {"school", "custom"}:
        raise ValueError("无效的预约时间或模式")
    with _lock:
        _check_revision(expected_revision, expected_instance)
        if _shutdown:
            raise ScheduleInvalidatedError("程序正在退出，不能创建预约")
        if _state["status"] == "starting":
            raise ScheduleConflictError("预约正在核验启动，请先取消后再修改")
        if _wake is not None:
            _wake.set()
        _wake = wake = threading.Event()
        _generation += 1
        generation = _generation
        _state.update(
            status="armed",
            target_at=target.astimezone(BEIJING).isoformat(),
            student_id=student_id,
            mode=mode,
            batch_code=batch_code,
            batch_name=batch_name,
            message="已预约，到点后会重新核验学校批次并启动",
            sync_message="",
        )
        worker = threading.Thread(
            target=_run,
            args=(generation, target, start, sync_clock, mode, wake),
            name="scheduled-enrollment",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            _state.update(status="failed", message="预约线程启动失败，请重新预约")
            raise
    return status()


def commit_start(generation: int | None, launch: Callable[[], bool]) -> bool:
    """Serialize the final worker launch against cancellation and manual starts."""
    with _lock:
        if _shutdown:
            raise ScheduleInvalidatedError("程序正在退出，未启动抢课")
        if generation is not None:
            if generation != _generation or _state["status"] != "starting":
                raise ScheduleInvalidatedError("预约已取消或更改，未启动抢课")
            target = datetime.fromisoformat(_state["target_at"])
            remaining = target - school_clock.now()[0]
            if remaining > timedelta(0) or remaining < -MAX_LATENESS:
                raise ScheduleInvalidatedError("预约时间已变化或已错过，请核对后重新预约")
        started = launch()
        if started:
            if generation is None:
                cancel(message="已手动启动抢课，原预约已取消")
            else:
                _state.update(status="started", message="预约核验通过，抢课任务已启动")
        return started


def _run(
    generation: int,
    target: datetime,
    start: Callable[[int], tuple[bool, str]],
    sync_clock: Callable[[], datetime | None] | None,
    mode: str,
    wake: threading.Event,
) -> None:
    synced_near_target = False
    next_sync_at = time.monotonic() + 300

    def publish(**values: Any) -> bool:
        with _lock:
            if generation != _generation or _shutdown:
                return False
            _state.update(values)
            return True

    def refresh_target(*, required: bool = False) -> bool:
        nonlocal synced_near_target, target
        try:
            updated = sync_clock() if sync_clock is not None else None
            if mode == "school":
                if updated is None or updated.tzinfo is None:
                    raise RuntimeError("未能确认当前批次的学校开抢时间")
                updated = updated.astimezone(UTC)
                if updated - school_clock.now()[0] > MAX_DELAY:
                    raise ScheduleInvalidatedError("学校开抢时间已超出 7 天预约范围，请重新确认")
                if updated != target:
                    target = updated
                    synced_near_target = (target - school_clock.now()[0]).total_seconds() <= 120
                    publish(
                        target_at=target.astimezone(BEIJING).isoformat(),
                        message="学校开抢时间已更新，预约已同步调整",
                    )
            publish(sync_message="")
            return True
        except ScheduleInvalidatedError as exc:
            publish(status="failed", message=str(exc))
            return False
        except Exception:
            logger.warning("Scheduled enrollment time check failed", exc_info=True)
            if required:
                publish(status="failed", message="到点时无法确认学校状态，预约未启动，请重新检查")
                return False
            publish(sync_message="学校状态暂未刷新，将在到点时再次核验；请检查网络和登录状态")
            return True

    while not wake.is_set():
        with _lock:
            if generation != _generation or _shutdown:
                return
        remaining = (target - school_clock.now()[0]).total_seconds()
        if remaining < -MAX_LATENESS.total_seconds():
            publish(status="failed", message="已错过预约时间超过 2 分钟，未自动补启动，请重新预约")
            return
        if remaining <= 0:
            if not publish(status="starting", message="到点，正在核验学校状态和登录会话"):
                return
            if not refresh_target(required=True):
                return
            if wake.is_set():
                return
            if target > school_clock.now()[0]:
                publish(status="armed", message="学校推迟了开抢时间，继续等待新时间")
                continue
            try:
                success, message = start(generation)
            except Exception:
                logger.exception("Scheduled enrollment start failed")
                success, message = False, "预约启动失败，请手动检查登录和学校状态"
            publish(status="started" if success else "failed", message=message)
            return
        if sync_clock and (
            time.monotonic() >= next_sync_at or (30 < remaining <= 120 and not synced_near_target)
        ):
            next_sync_at = time.monotonic() + 300
            synced_near_target = True
            if not refresh_target():
                return
            continue
        wake.wait(min(remaining, 30.0 if remaining > 120 else 1.0 if remaining > 5 else 0.1))
