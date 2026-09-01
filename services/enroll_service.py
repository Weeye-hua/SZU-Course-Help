"""
抢课服务模块

职责：
    1. 执行抢课循环（遍历购物车课程，调用学校选课接口）
    2. 对学校返回结果分类处理（成功 / 容量满重试 / 终态失败 / 会话过期）
    3. 多门课程轮询抢课：抢到的立即停止，未抢到的继续
    4. 会话过期自动重登录，连续多次失败后暂停并保留队列
    5. 线程安全的进度与事件跟踪，供 Web UI 轮询

核心约束：
    - 保持 choose_course.submit_course_selection() 的学校请求字段兼容
    - 学校提交响应只作为候选结果，最终以已选课程中的教学班 ID 为准
    - 保留 PENDING → ENROLLING → SUCCESS/FAILED 的状态语义
"""

import copy
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import requests

import choose_course
import config
import database
from campus import DEFAULT_CAMPUS_CODE, campus_name
from course_models import priority_group_key, time_signature
from school_session import is_session_expired_response
from services import cart_service
from services.auth_service import (
    attempt_automatic_relogin,
)
from services.course_service import query_courses

_task_state_lock = threading.RLock()
_task_condition = threading.Condition(_task_state_lock)
_task_state = {
    "running": False,
    "paused": False,
    "pause_acknowledged": False,
    "pause_reason": "",
    "pause_source": "",
    "paused_at": "",
    "stop_requested": False,
    "stop_reason": "",
    "mode": "boost",
    "queue_revision": 0,
}
_settings = {
    "boost_interval_ms": 1000,
    "normal_interval_ms": 10000,
    "scan_interval_ms": 60000,
    "boost_failure_limit": 5,
    "normal_failure_limit": 10,
}
MAX_MODE_FAILURE_LIMIT = 1_000_000
_business_failure_counts: dict[str, int] = {}
_unknown_streak_counts: dict[str, int] = {}
_network_streak_counts: dict[str, int] = {}
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EnrollmentCourse:
    """Minimal immutable course data used by the background worker."""

    id: str
    type: str
    name: str
    campus_code: str = DEFAULT_CAMPUS_CODE
    campus_name: str = ""
    course_number: str = ""
    teaching_place: str = ""
    time_signature: str = ""
    priority_group: str = ""
    priority_rank: int = 0


class GrabOutcome(StrEnum):
    """Why one enrollment pass returned control to the worker."""

    COMPLETED = "completed"
    CONTINUE = "continue"
    SESSION_EXPIRED = "session_expired"
    PAUSED = "paused"


class EnrollmentConfirmation(StrEnum):
    """Result of checking the authoritative selected-course list."""

    CONFIRMED = "confirmed"
    ABSENT = "absent"
    SESSION_EXPIRED = "session_expired"
    UNAVAILABLE = "unavailable"
    INTERRUPTED = "interrupted"


# ====================================================================
# 学校返回结果分类关键词
# ====================================================================
# 抢到成功
SUCCESS_KEYWORD = "添加选课志愿成功"
# 容量已满：属于可重试情形，继续下一轮，不受未知返回阈值限制
CAPACITY_FULL_KEYWORDS = (
    "该课程超过课容量",
    "超过课容量",
    "课容量已满",
    "课程容量已满",
    "超过课程容量",
    "选课人数已满",
    "课程人数已满",
    "教学班容量已满",
    "该教学班已满",
)
# 学校批次存在，但此刻不在实际开放时段。此类结果只能暂停，不能永久失败。
WINDOW_CLOSED_KEYWORDS = (
    "当前时间不在选课开放时间范围内",
    "不在选课开放时间",
    "未在选课开放时间",
    "不在开放时间范围内",
    "不在选课时间",
    "不在补选时间",
    "当前时间不在补选",
    "非选课时间",
    "未到选课时间",
    "选课时间未到",
    "选课时间已过",
    "选课尚未开始",
    "选课未开始",
    "选课暂未开放",
    "选课已结束",
    "选课已经结束",
)
# 学校临时繁忙：继续轮询，但不计入未知返回。
RETRYABLE_ERROR_KEYWORDS = (
    "系统繁忙",
    "服务繁忙",
    "请稍后再试",
    "请求频繁",
    "操作频繁",
    "网络繁忙",
)
# A duplicate response may mean the first request succeeded even when its reply
# was lost. It is therefore a confirmation candidate, not a terminal failure.
ALREADY_SELECTED_KEYWORDS = (
    "已经选过",
    "已选过",
    "已经选择",
    "重复选课",
    "已存在",
)
# 终态失败：重试也不会成功，直接标记 FAILED 并移出活动集
TERMINAL_ERROR_KEYWORDS = (
    "时间冲突",
    "上课时间冲突",
    "冲突",
    "不在补选",
    "超过学分",
    "学分已满",
    "学分已达上限",
    "学分已达到上限",
    "选课门数已达上限",
    "选课门数已达到上限",
    "志愿数已达上限",
    "志愿数已达到上限",
    "不满足",
    "无权限",
    "不允许",
    "不符合",
)
# 连续未知或网络异常达到阈值时触发保护性暂停，不会把课程写成 FAILED。
MAX_NETWORK_STREAK = 8
NETWORK_BACKOFF_BASE_MS = 500
NETWORK_BACKOFF_CAP_MS = 30_000
UNKNOWN_BACKOFF_STEP_MS = 200
UNKNOWN_BACKOFF_CAP_MS = 2_000
ENROLLMENT_CONFIRM_ATTEMPTS = 3
ENROLLMENT_CONFIRM_RETRY_SECONDS = 0.5
ENROLLMENT_CONFIRM_TIMEOUT = (3, 8)
# 事件队列上限（仅保留最近的事件）
MAX_EVENTS = 200


# ====================================================================
# 进度与事件跟踪（线程安全）
# ====================================================================
_progress_lock = threading.Lock()
_progress = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "courses": {},  # id -> {"id","name","type","status","attempts","message"}
    "events": [],  # [{"ts","level","message"}]
    "mode": "boost",
    "settings": dict(_settings),
    "retryable_ids": set(),
}


def _reset_progress(courses) -> None:
    """初始化进度状态（后台任务开始时调用）。"""
    with _progress_lock:
        _progress["running"] = True
        _progress["started_at"] = datetime.now().isoformat(timespec="seconds")
        _progress["finished_at"] = None
        _progress["courses"] = {
            course.id: {
                "id": course.id,
                "name": course.name,
                "type": course.type,
                "campus_code": getattr(course, "campus_code", DEFAULT_CAMPUS_CODE),
                "campus_name": getattr(course, "campus_name", ""),
                "status": database.STATUS_IN_PROGRESS,
                "attempts": 0,
                "failures": 0,
                "message": "等待抢课",
            }
            for course in courses
        }
        _progress["events"] = []
        _progress["mode"] = _task_state["mode"]
        _progress["settings"] = dict(_settings)
        _progress["retryable_ids"] = set()


def _set_progress_finished() -> None:
    with _progress_lock:
        _progress["running"] = False
        _progress["finished_at"] = datetime.now().isoformat(timespec="seconds")


def _update_course_progress(course_id, *, increment_attempts=False, **fields) -> None:
    with _progress_lock:
        entry = _progress["courses"].get(course_id)
        if entry is None:
            return
        if increment_attempts:
            entry["attempts"] = entry.get("attempts", 0) + 1
        entry.update(fields)


def _remove_course_progress(course_id: str) -> bool:
    """Remove a deleted cart item from the task snapshot shown in the UI."""
    with _progress_lock:
        return _progress["courses"].pop(course_id, None) is not None


def _ensure_course_progress(course: EnrollmentCourse) -> bool:
    """Add a newly queued course without resetting existing attempt counters."""
    with _progress_lock:
        entry = _progress["courses"].get(course.id)
        if entry is not None:
            reactivated = entry.get("status") in (
                database.STATUS_SUCCESS,
                database.STATUS_FAILED,
                database.STATUS_NOT_STARTED,
            )
            entry.update(
                {
                    "name": course.name,
                    "type": course.type,
                    "campus_code": course.campus_code,
                    "campus_name": course.campus_name,
                    "status": database.STATUS_IN_PROGRESS,
                }
            )
            if reactivated:
                entry["message"] = "已重新加入本轮队列，等待抢课"
            return reactivated
        _progress["courses"][course.id] = {
            "id": course.id,
            "name": course.name,
            "type": course.type,
            "campus_code": course.campus_code,
            "campus_name": course.campus_name,
            "status": database.STATUS_IN_PROGRESS,
            "attempts": 0,
            "failures": 0,
            "message": "已加入本轮队列，等待抢课",
        }
        return True


def _add_event(level: str, message: str) -> None:
    """记录一条事件并打印到终端。level: info/success/warn/error。"""
    with _progress_lock:
        _progress["events"].append(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "level": level,
                "message": message,
            }
        )
        if len(_progress["events"]) > MAX_EVENTS:
            del _progress["events"][:-MAX_EVENTS]
    log_method = logger.error if level == "error" else logger.info
    log_method("Enrollment event [%s]: %s", level, message)


def get_enroll_progress() -> dict:
    """返回当前抢课进度的快照（供 API 轮询）。"""
    with _progress_lock:
        courses = [copy.deepcopy(entry) for entry in _progress["courses"].values()]
        events = copy.deepcopy(_progress["events"])
        snapshot = {
            "running": _progress["running"],
            "started_at": _progress["started_at"],
            "finished_at": _progress["finished_at"],
            "courses": courses,
            "events": events,
            "mode": str(_task_state["mode"]),
            "settings": dict(_settings),
            "queue_revision": int(_task_state["queue_revision"]),
        }
    total = len(courses)
    success = sum(1 for c in courses if c["status"] == database.STATUS_SUCCESS)
    failed = sum(1 for c in courses if c["status"] == database.STATUS_FAILED)
    snapshot["counts"] = {
        "total": total,
        "success": success,
        "failed": failed,
        "active": total - success - failed,
    }
    snapshot.update(get_enroll_task_state())
    return snapshot


# ====================================================================
# 任务占用管理
# ====================================================================
def reserve_enroll_task() -> bool:
    """Atomically reserve the single enrollment worker slot."""
    global _business_failure_counts, _network_streak_counts, _unknown_streak_counts
    with _task_condition:
        if _task_state["running"]:
            return False
        _task_state.update(
            {
                "running": True,
                "paused": False,
                "pause_acknowledged": False,
                "pause_reason": "",
                "pause_source": "",
                "paused_at": "",
                "stop_requested": False,
                "stop_reason": "",
                "mode": "boost",
            }
        )
        _business_failure_counts = {}
        _unknown_streak_counts = {}
        _network_streak_counts = {}
        return True


def start_enroll_worker() -> bool:
    """Reserve and launch the long-running worker outside the HTTP request lifecycle."""
    if not reserve_enroll_task():
        return False
    worker = threading.Thread(
        target=run_enroll_task,
        args=(True,),
        name="course-enrollment-worker",
        daemon=True,
    )
    try:
        worker.start()
    except Exception:
        _release_enroll_task()
        raise
    return True


def is_enroll_task_running() -> bool:
    with _task_state_lock:
        return bool(_task_state["running"])


def get_enroll_task_state() -> dict[str, str | bool]:
    """Return a thread-safe, JSON-ready snapshot of task controls."""
    with _task_state_lock:
        return {
            "running": bool(_task_state["running"]),
            "paused": bool(_task_state["paused"]),
            "pause_acknowledged": bool(_task_state["pause_acknowledged"]),
            "pause_reason": str(_task_state["pause_reason"]),
            "pause_source": str(_task_state["pause_source"]),
            "paused_at": str(_task_state["paused_at"]),
            "stopping": bool(_task_state["stop_requested"]),
            "stopping_reason": str(_task_state["stop_reason"]),
            "mode": str(_task_state["mode"]),
            "settings": dict(_settings),
            "queue_revision": int(_task_state["queue_revision"]),
        }


def _queue_mutation_block_locked() -> tuple[str, str] | None:
    """Return a stable error when a running worker has not reached a safe boundary."""
    if not _task_state["running"]:
        return None
    if _task_state["stop_requested"]:
        return "抢课任务正在结束，请稍候后再修改清单", "ENROLL_TASK_STOPPING"
    if not _task_state["paused"]:
        return "请先暂停抢课任务，再修改清单", "ENROLL_TASK_NOT_PAUSED"
    if not _task_state["pause_acknowledged"]:
        return "正在完成当前学校请求，安全暂停后即可修改清单", "ENROLL_TASK_PAUSE_PENDING"
    return None


def _advance_queue_revision_locked() -> int:
    _task_state["queue_revision"] = int(_task_state["queue_revision"]) + 1
    _task_condition.notify_all()
    return int(_task_state["queue_revision"])


def add_cart_course(course) -> dict[str, object]:
    """Add to the DB while holding the same lock used by pause/resume."""
    with _task_condition:
        blocked = _queue_mutation_block_locked()
        if blocked:
            message, error_code = blocked
            return {"success": False, "message": message, "error_code": error_code}
        normalized_id = str(getattr(course, "id", "") or "").strip()
        if any(
            str(item.get("id") or "") == normalized_id
            for item in cart_service.get_courses_by_status("")
        ):
            return {
                "success": False,
                "message": "该课程已在本地清单中，请使用现有课程项进行调整",
                "error_code": "COURSE_ALREADY_IN_CART",
            }
        result = cart_service.add_course(course)
        if not result["success"]:
            return result
        revision = _advance_queue_revision_locked()
        return {**result, "queue_revision": revision}


def update_cart_course_preferences(course_id: str, **fields) -> dict[str, object]:
    """Update queue controls only outside a task or at an acknowledged pause."""
    with _task_condition:
        blocked = _queue_mutation_block_locked()
        if blocked:
            message, error_code = blocked
            return {"success": False, "message": message, "error_code": error_code}
        if not cart_service.update_course_preferences(course_id, **fields):
            return {
                "success": False,
                "message": "课程不存在或偏好设置无效",
                "error_code": "COURSE_UPDATE_FAILED",
            }
        revision = _advance_queue_revision_locked()
        return {
            "success": True,
            "message": "课程优先级设置已更新",
            "queue_revision": revision,
        }


def retry_cart_course(course_id: str) -> dict[str, object]:
    """Requeue a failed course under the same pause-boundary contract."""
    with _task_condition:
        blocked = _queue_mutation_block_locked()
        if blocked:
            message, error_code = blocked
            return {"success": False, "message": message, "error_code": error_code}
        result = cart_service.retry_failed_course(course_id)
        if not result["success"]:
            return result
        revision = _advance_queue_revision_locked()
        return {**result, "queue_revision": revision}


def update_cart_course_priorities(updates: list[tuple[str, int]]) -> dict[str, object]:
    """Apply a rank swap atomically at an acknowledged pause boundary."""
    with _task_condition:
        blocked = _queue_mutation_block_locked()
        if blocked:
            message, error_code = blocked
            return {"success": False, "message": message, "error_code": error_code}
        if not cart_service.update_course_priorities(updates):
            return {
                "success": False,
                "message": "课程排序无效或课程不存在",
                "error_code": "COURSE_PRIORITY_UPDATE_FAILED",
            }
        revision = _advance_queue_revision_locked()
        return {
            "success": True,
            "message": "课程优先级顺序已更新",
            "queue_revision": revision,
        }


def pause_enroll_task(
    reason: str = "用户已暂停抢课任务",
    *,
    source: str = "user",
) -> tuple[bool, str]:
    """Pause before the next school request while retaining the active queue."""
    normalized_reason = str(reason or "抢课任务已暂停").strip()
    changed = False
    with _task_condition:
        if not _task_state["running"]:
            return False, "当前没有正在运行的抢课任务"
        if _task_state["stop_requested"]:
            return False, "抢课任务正在结束，请稍候"
        if not _task_state["paused"]:
            changed = True
            _task_state.update(
                {
                    "paused": True,
                    "pause_acknowledged": False,
                    "pause_reason": normalized_reason,
                    "pause_source": str(source or "user"),
                    "paused_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            )
        _task_condition.notify_all()

    if changed:
        level = "info" if source == "user" else "warn"
        _add_event(level, normalized_reason)
    return True, normalized_reason


def resume_enroll_task() -> tuple[bool, str]:
    """Resume a paused task without resetting courses or attempt counters."""
    with _task_condition:
        if not _task_state["running"]:
            return False, "当前没有可继续的抢课任务"
        if _task_state["stop_requested"]:
            return False, "待处理课程已清空，抢课任务正在结束"
        if not _task_state["paused"]:
            return True, "抢课任务已经在运行"
        _task_state.update(
            {
                "paused": False,
                "pause_acknowledged": False,
                "pause_reason": "",
                "pause_source": "",
                "paused_at": "",
            }
        )
        _task_condition.notify_all()

    _add_event("info", "抢课任务已继续")
    return True, "抢课任务已继续"


def set_enroll_mode(mode: str) -> bool:
    """Change the mode used at the next request boundary."""
    normalized = str(mode or "").strip().lower()
    if normalized not in {"boost", "normal", "scan"}:
        return False
    with _task_condition:
        _task_state["mode"] = normalized
        _task_condition.notify_all()
    with _progress_lock:
        _progress["mode"] = normalized
    return True


def get_enroll_settings() -> dict:
    with _task_state_lock:
        return {**_settings, "mode": _task_state["mode"]}


def update_enroll_settings(**values) -> dict:
    with _task_state_lock:
        for key in ("boost_interval_ms", "normal_interval_ms", "scan_interval_ms"):
            if key in values:
                _settings[key] = max(0, min(300000, int(values[key])))
        for key in ("boost_failure_limit", "normal_failure_limit"):
            if key not in values:
                continue
            value = values[key]
            if value is None:
                _settings[key] = None
                continue
            if isinstance(value, bool):
                raise ValueError("failure limit must be an integer or null")
            _settings[key] = max(1, min(MAX_MODE_FAILURE_LIMIT, int(value)))
        return get_enroll_settings()


def _mode_interval_seconds(mode: str) -> float:
    """Return a mode interval while honoring the legacy zero-delay override."""
    if int(getattr(config, "delay", 1) or 0) == 0:
        return 0.0
    return max(0, int(_settings[f"{mode}_interval_ms"])) / 1000.0


def _mode_failure_limit(mode: str) -> int | None:
    """Return the automatic downgrade threshold, or ``None`` when disabled."""
    if mode not in {"boost", "normal"}:
        return None
    with _task_state_lock:
        value = _settings[f"{mode}_failure_limit"]
    return None if value is None else max(1, int(value))


def _wait_until_resumed() -> bool:
    """Block at a request boundary and acknowledge that cart edits are safe."""
    with _task_condition:
        if _task_state["running"] and _task_state["paused"] and not _task_state["stop_requested"]:
            _task_state["pause_acknowledged"] = True
            _task_condition.notify_all()
        while (
            _task_state["running"] and _task_state["paused"] and not _task_state["stop_requested"]
        ):
            _task_condition.wait(timeout=1.0)
        _task_state["pause_acknowledged"] = False
        return bool(_task_state["running"] and not _task_state["stop_requested"])


def _wait_between_requests(seconds: float) -> bool:
    """Use an interruptible delay so a pause takes effect promptly."""
    standalone = False
    with _task_condition:
        if _task_state["paused"] or _task_state["stop_requested"]:
            return False
        if not _task_state["running"]:
            standalone = True
        else:
            _task_condition.wait(timeout=max(0.0, float(seconds)))
            return bool(
                _task_state["running"]
                and not _task_state["paused"]
                and not _task_state["stop_requested"]
            )
    if standalone:
        time.sleep(max(0.0, float(seconds)))
    return True


def is_stop_requested() -> bool:
    """Return whether the worker should stop at the next request boundary."""
    with _task_state_lock:
        return bool(_task_state["stop_requested"])


def stop_enroll_task() -> bool:
    """Request a graceful stop while preserving courses that were not completed."""
    with _task_condition:
        if not _task_state["running"]:
            return False
        _task_state.update(
            {
                "stop_requested": True,
                "stop_reason": "用户请求停止抢课任务",
                "paused": False,
            }
        )
        _task_condition.notify_all()
    _add_event("info", "收到停止请求，抢课任务将在当前请求边界结束")
    return True


def _release_enroll_task() -> None:
    with _task_condition:
        _task_state.update(
            {
                "running": False,
                "paused": False,
                "pause_acknowledged": False,
                "pause_reason": "",
                "pause_source": "",
                "paused_at": "",
                "stop_requested": False,
                "stop_reason": "",
                "mode": "boost",
            }
        )
        _task_condition.notify_all()


def _response_payload(response) -> dict:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except (ValueError, AttributeError):
        return {}


def _response_code(response) -> str | None:
    payload = _response_payload(response)
    code = payload.get("code")
    return str(code) if code is not None else None


def _response_message(response) -> str:
    payload = _response_payload(response)
    for key in ("msg", "message", "error_msg"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return (getattr(response, "text", "") or "").strip()


def _classify_response(response) -> str:
    """把学校返回结果归类为一个动作标签。

    返回：success | already_selected | retry | terminal | expired |
    window_closed | transient | unknown
    """
    text = getattr(response, "text", "") or ""
    message = _response_message(response)
    searchable = f"{message}\n{text}"
    code = _response_code(response)
    status_code = getattr(response, "status_code", None)

    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return "transient"
    if is_session_expired_response(
        status_code=status_code,
        code=code,
        text=text,
    ):
        return "expired"
    if SUCCESS_KEYWORD in searchable:
        return "success"
    if any(keyword in searchable for keyword in ALREADY_SELECTED_KEYWORDS):
        return "already_selected"
    if any(keyword in searchable for keyword in CAPACITY_FULL_KEYWORDS):
        return "retry"
    if any(keyword in searchable for keyword in WINDOW_CLOSED_KEYWORDS):
        return "window_closed"
    if any(keyword in searchable for keyword in RETRYABLE_ERROR_KEYWORDS):
        return "retry"
    if any(keyword in searchable for keyword in TERMINAL_ERROR_KEYWORDS):
        return "terminal"
    return "unknown"


def _confirm_course_enrolled(course: EnrollmentCourse) -> EnrollmentConfirmation:
    """Confirm one enrollment by exact class ID in the school's selected list."""
    latest_snapshot_valid = False
    last_error = ""

    for attempt in range(1, ENROLLMENT_CONFIRM_ATTEMPTS + 1):
        if is_stop_requested() or get_enroll_task_state()["paused"]:
            return EnrollmentConfirmation.INTERRUPTED
        _update_course_progress(
            course.id,
            message=(
                f"学校已受理，正在核对学校已选课程（{attempt}/{ENROLLMENT_CONFIRM_ATTEMPTS}）"
            ),
        )
        try:
            rows = choose_course.query_enrolled_courses(
                config.combined_cookie,
                config.token,
                timeout=ENROLLMENT_CONFIRM_TIMEOUT,
            )
            selected_ids = choose_course.enrolled_teaching_class_ids(rows)
            latest_snapshot_valid = True
            if course.id in selected_ids:
                return EnrollmentConfirmation.CONFIRMED
        except choose_course.SchoolSessionExpiredError:
            return EnrollmentConfirmation.SESSION_EXPIRED
        except (requests.RequestException, ConnectionError, TypeError, ValueError) as exc:
            latest_snapshot_valid = False
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Enrollment confirmation query failed for %s (%s/%s): %s",
                course.name,
                attempt,
                ENROLLMENT_CONFIRM_ATTEMPTS,
                last_error,
            )

        if attempt < ENROLLMENT_CONFIRM_ATTEMPTS and not _wait_between_requests(
            ENROLLMENT_CONFIRM_RETRY_SECONDS
        ):
            return EnrollmentConfirmation.INTERRUPTED

    if latest_snapshot_valid:
        return EnrollmentConfirmation.ABSENT
    logger.warning(
        "Enrollment confirmation unavailable for %s after %s attempt(s): %s",
        course.name,
        ENROLLMENT_CONFIRM_ATTEMPTS,
        last_error or "no valid selected-course snapshot",
    )
    return EnrollmentConfirmation.UNAVAILABLE


def _record_business_failure(
    course: EnrollmentCourse,
    business_failures: dict[str, int],
    message: str,
) -> None:
    """Record a retryable school rejection and apply configured mode limits."""
    business_failures[course.id] += 1
    _business_failure_counts[course.id] = business_failures[course.id]
    _update_course_progress(
        course.id,
        failures=business_failures[course.id],
        mode=get_enroll_task_state()["mode"],
        message=message,
    )
    current_mode = get_enroll_task_state()["mode"]
    failure_limit = _mode_failure_limit(current_mode)
    if (
        get_enroll_task_state()["running"]
        and current_mode == "boost"
        and failure_limit is not None
        and business_failures[course.id] >= failure_limit
    ):
        set_enroll_mode("normal")
        _add_event(
            "warn",
            f"{course.name} boost 业务失败达到 {failure_limit} 次，降为一般模式",
        )
    elif (
        get_enroll_task_state()["running"]
        and current_mode == "normal"
        and failure_limit is not None
        and business_failures[course.id] >= failure_limit
    ):
        set_enroll_mode("scan")
        _add_event(
            "warn",
            f"{course.name} 一般模式业务失败达到 {failure_limit} 次，降为扫描模式",
        )


def _active_course_ids() -> set:
    """从数据库读取仍需抢课（未成功/未失败）的课程 id 集合。"""
    stored = {item["id"]: item for item in cart_service.get_courses_by_status("")}
    active = set()
    for course_id, row in stored.items():
        if row.get("status") not in (database.STATUS_SUCCESS, database.STATUS_FAILED) and row.get(
            "auto_enabled", 1
        ):
            active.add(course_id)
    return active


def _course_group(course) -> str:
    return priority_group_key(
        explicit_group=getattr(course, "priority_group", ""),
        course_number=getattr(course, "course_number", ""),
        schedule_signature=(
            getattr(course, "time_signature", "")
            or time_signature(getattr(course, "teaching_place", ""))
        ),
        course_id=course.id,
    )


def _numeric(value):
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _scan_course_available(course) -> int:
    """Probe catalog pages without submitting a selection request."""
    try:
        page = 0
        while page < 100:
            success, result, _ = query_courses(course.type, page)
            if not success:
                return 0
            listed_courses = getattr(result, "courses", None)
            if listed_courses is None:
                listed_courses = getattr(result, "data_list", None)
            if listed_courses is None and isinstance(result, dict):
                listed_courses = result.get("courses") or result.get("dataList") or []
            for listed in listed_courses:
                teaching_classes = getattr(listed, "teaching_classes", None)
                if teaching_classes is None and isinstance(listed, dict):
                    teaching_classes = listed.get("teaching_classes") or listed.get("tcList") or []
                for teaching_class in teaching_classes or []:
                    class_id = getattr(teaching_class, "teaching_class_id", None)
                    if class_id is None and isinstance(teaching_class, dict):
                        class_id = teaching_class.get("teaching_class_id") or teaching_class.get(
                            "teachingClassID"
                        )
                    if str(class_id or "") != str(course.id):
                        continue
                    selected_value = getattr(teaching_class, "number_of_selected", None)
                    capacity_value = getattr(teaching_class, "class_capacity", None)
                    if isinstance(teaching_class, dict):
                        selected_value = teaching_class.get(
                            "number_of_selected"
                        ) or teaching_class.get("numberOfSelected")
                        capacity_value = teaching_class.get("class_capacity") or teaching_class.get(
                            "classCapacity"
                        )
                    selected = _numeric(selected_value)
                    capacity = _numeric(capacity_value)
                    if capacity is not None and selected is not None and selected < capacity:
                        return capacity - selected
            total_count_value = getattr(result, "total_count", None)
            if total_count_value is None and isinstance(result, dict):
                total_count_value = result.get("total_count") or result.get("totalCount")
            total_count = _numeric(total_count_value) or 0
            if not listed_courses or page + 1 >= max(1, math.ceil(total_count / 10)):
                break
            page += 1
    except Exception as exc:
        logger.info("Scan query failed for %s: %s", course.name, exc)
    return 0


def remove_cart_course(course_id: str) -> dict[str, bool | str]:
    """Safely remove a cart item, including while a worker is fully paused.

    A pause request is not enough on its own: the worker may still have one school
    request in flight.  Deletion is allowed only after ``_wait_until_resumed`` has
    acknowledged the pause at a request boundary.
    """
    normalized_id = str(course_id or "").strip()
    if not normalized_id:
        return {
            "success": False,
            "message": "课程 ID 不能为空",
            "error_code": "INVALID_COURSE_ID",
        }

    should_stop = False
    was_running = False
    with _task_condition:
        stored_course = next(
            (
                item
                for item in cart_service.get_courses_by_status("")
                if item.get("id") == normalized_id
            ),
            None,
        )
        if stored_course is None:
            return {
                "success": False,
                "message": "删除失败或课程不存在",
                "error_code": "COURSE_NOT_FOUND",
            }
        is_terminal = stored_course.get("status") in (
            database.STATUS_SUCCESS,
            database.STATUS_FAILED,
        )
        was_running = bool(_task_state["running"])
        if was_running and _task_state["stop_requested"]:
            return {
                "success": False,
                "message": "抢课任务正在结束，请稍候后再修改清单",
                "error_code": "ENROLL_TASK_STOPPING",
            }
        if was_running and not is_terminal:
            if not _task_state["paused"]:
                return {
                    "success": False,
                    "message": "请先暂停抢课任务，再移除课程",
                    "error_code": "ENROLL_TASK_NOT_PAUSED",
                }
            if not _task_state["pause_acknowledged"]:
                return {
                    "success": False,
                    "message": "正在完成当前学校请求，安全暂停后即可移除",
                    "error_code": "ENROLL_TASK_PAUSE_PENDING",
                }

        result = cart_service.delete_course(normalized_id)
        if not result["success"]:
            return {
                **result,
                "error_code": "COURSE_NOT_FOUND",
            }
        revision = _advance_queue_revision_locked()

        if was_running and not _active_course_ids():
            should_stop = True
            stop_reason = "待处理课程已全部移除，抢课任务正在结束"
            _task_state.update(
                {
                    "paused": False,
                    "pause_acknowledged": False,
                    "pause_reason": "",
                    "pause_source": "",
                    "paused_at": "",
                    "stop_requested": True,
                    "stop_reason": stop_reason,
                }
            )
            _task_condition.notify_all()

    removed_from_progress = _remove_course_progress(normalized_id)
    if was_running or removed_from_progress:
        _add_event("info", "课程已从本地清单和本轮任务中移除")

    if should_stop:
        return {
            "success": True,
            "message": "课程已移除；清单中已无待处理课程，任务正在结束",
            "task_stopping": True,
            "queue_revision": revision,
        }
    return {
        "success": True,
        "message": "课程已从本地清单移除",
        "task_stopping": False,
        "queue_revision": revision,
    }


def _course_from_row(item: dict) -> EnrollmentCourse:
    return EnrollmentCourse(
        id=str(item["id"]),
        type=str(item["type"]),
        name=str(item["name"]),
        campus_code=str(item.get("campus_code") or DEFAULT_CAMPUS_CODE),
        campus_name=str(
            item.get("campus_name") or campus_name(item.get("campus_code") or DEFAULT_CAMPUS_CODE)
        ),
        course_number=str(item.get("course_number") or ""),
        teaching_place=str(item.get("teaching_place") or ""),
        time_signature=str(
            item.get("time_signature") or time_signature(item.get("teaching_place", ""))
        ),
        priority_group=str(item.get("priority_group") or ""),
        priority_rank=int(item.get("priority_rank", 0) or 0),
    )


def _reconcile_courses(courses: list[EnrollmentCourse]) -> list[EnrollmentCourse]:
    """Rebuild the active queue from SQLite without resetting unchanged progress."""
    rows = cart_service.get_courses_by_status("")
    rows_by_id = {str(row.get("id")): row for row in rows}
    active_rows = [
        row
        for row in rows
        if row.get("status") in (database.STATUS_NOT_STARTED, database.STATUS_IN_PROGRESS)
        and row.get("auto_enabled", 1)
    ]
    active_ids = {str(row["id"]) for row in active_rows}

    for old_course in list(courses):
        row = rows_by_id.get(old_course.id)
        if row is not None and not row.get("auto_enabled", 1):
            if row.get("status") == database.STATUS_IN_PROGRESS:
                cart_service.update_status(old_course.id, database.STATUS_NOT_STARTED)
            _remove_course_progress(old_course.id)

    reconciled = [_course_from_row(row) for row in active_rows]
    reconciled.sort(key=lambda course: (_course_group(course), course.priority_rank, course.id))
    for course in reconciled:
        if rows_by_id[course.id].get("status") == database.STATUS_NOT_STARTED:
            cart_service.update_status(course.id, database.STATUS_IN_PROGRESS)
        if _ensure_course_progress(course):
            _add_event("info", f"{course.name} 已加入当前抢课队列")

    courses[:] = reconciled
    for mapping in (_business_failure_counts, _unknown_streak_counts, _network_streak_counts):
        for course_id in list(mapping):
            if course_id not in active_ids:
                mapping.pop(course_id, None)
    return courses


def grab_courses(courses: list) -> GrabOutcome:
    """Run bounded enrollment rounds using the current mode and priorities."""
    active = _reconcile_courses(courses)
    unknown_streak_limit = max(1, int(config.unknown_response_pause_threshold))
    unknown_streak = _unknown_streak_counts
    network_streak = _network_streak_counts
    business_failures = {course.id: _business_failure_counts.get(course.id, 0) for course in active}
    for course in active:
        unknown_streak.setdefault(course.id, 0)
        network_streak.setdefault(course.id, 0)

    if not active:
        return GrabOutcome.COMPLETED

    for _ in range(max(1, int(config.count))):
        if is_stop_requested():
            return GrabOutcome.COMPLETED
        active = _reconcile_courses(courses)
        if not active:
            break
        business_failures = {
            course.id: _business_failure_counts.get(course.id, 0) for course in active
        }
        for course in active:
            unknown_streak.setdefault(course.id, 0)
            network_streak.setdefault(course.id, 0)
        for course in list(active):
            if is_stop_requested():
                return GrabOutcome.COMPLETED
            if get_enroll_task_state()["paused"]:
                return GrabOutcome.PAUSED

            mode = get_enroll_task_state()["mode"]
            if mode == "scan":
                _update_course_progress(course.id, message="扫描课程目录，等待课程放课")
                available_slots = _scan_course_available(course)
                if available_slots <= 0:
                    if not _wait_between_requests(_mode_interval_seconds("scan")):
                        return GrabOutcome.PAUSED
                    continue
                set_enroll_mode("boost")
                _add_event("info", f"检测到 {course.name} 有可选容量，切换爆发模式")

            try:
                _update_course_progress(
                    course.id, increment_attempts=True, mode=get_enroll_task_state()["mode"]
                )
                course_campus = str(getattr(course, "campus_code", "") or "").strip()
                if course_campus:
                    response = choose_course.submit_course_selection(
                        course.id, course.type, course_campus
                    )
                else:
                    response = choose_course.submit_course_selection(course.id, course.type)
                network_streak[course.id] = 0
                action = _classify_response(response)
                if action != "unknown":
                    unknown_streak[course.id] = 0

                if action in {"success", "already_selected"}:
                    confirmation = _confirm_course_enrolled(course)
                    if confirmation == EnrollmentConfirmation.CONFIRMED:
                        if not cart_service.update_status(course.id, database.STATUS_SUCCESS):
                            reason = (
                                f"{course.name} 已由学校确认选中，但本地状态写入失败，"
                                "任务已保护性暂停"
                            )
                            _update_course_progress(course.id, message=reason)
                            pause_enroll_task(reason, source="local_state_error")
                            return GrabOutcome.PAUSED
                        _update_course_progress(
                            course.id,
                            status=database.STATUS_SUCCESS,
                            message="已由学校已选课程列表确认",
                        )
                        _add_event(
                            "success",
                            f"{course.name} 已由学校已选课程列表确认选中",
                        )
                        active.remove(course)
                    elif confirmation == EnrollmentConfirmation.SESSION_EXPIRED:
                        _add_event("warn", "核对选课结果时检测到登录已过期，准备自动重新登录")
                        return GrabOutcome.SESSION_EXPIRED
                    elif confirmation == EnrollmentConfirmation.UNAVAILABLE:
                        reason = (
                            f"{course.name} 的提交结果暂时无法向学校已选课程列表核实，"
                            "任务已保护性暂停；请稍后刷新“我的课程”并继续"
                        )
                        _update_course_progress(course.id, message=reason)
                        pause_enroll_task(reason, source="confirmation_unavailable")
                        return GrabOutcome.PAUSED
                    elif confirmation == EnrollmentConfirmation.INTERRUPTED:
                        return GrabOutcome.COMPLETED if is_stop_requested() else GrabOutcome.PAUSED
                    else:
                        reason = _response_message(response)[:80]
                        _record_business_failure(
                            course,
                            business_failures,
                            (
                                "学校提示已受理，但已选课程列表尚未出现该教学班，继续尝试"
                                f"：{reason}"
                                if reason
                                else "学校提示已受理，但已选课程列表尚未出现该教学班，继续尝试"
                            ),
                        )
                        logger.warning(
                            "School accepted enrollment for %s but class %s was absent "
                            "from the selected-course list",
                            course.name,
                            course.id,
                        )
                        if not _wait_between_requests(
                            _mode_interval_seconds(get_enroll_task_state()["mode"])
                        ):
                            return GrabOutcome.PAUSED
                        continue
                elif action == "retry":
                    reason = _response_message(response)
                    _record_business_failure(
                        course,
                        business_failures,
                        (
                            "课容量已满，继续尝试"
                            if any(keyword in reason for keyword in CAPACITY_FULL_KEYWORDS)
                            else f"学校暂时未受理，继续尝试：{reason[:80] or '请稍后再试'}"
                        ),
                    )
                    if not _wait_between_requests(
                        _mode_interval_seconds(get_enroll_task_state()["mode"])
                    ):
                        return GrabOutcome.PAUSED
                    continue
                elif action == "expired":
                    _add_event("warn", "检测到登录已过期，准备自动重新登录")
                    return GrabOutcome.SESSION_EXPIRED
                elif action == "window_closed":
                    reason = _response_message(response)[:160] or "学校当前未开放选课"
                    message = f"学校提示“{reason}”，任务已自动暂停；开放后可点击继续"
                    _update_course_progress(course.id, message=message)
                    pause_enroll_task(message, source="school_window")
                    return GrabOutcome.PAUSED
                elif action == "terminal":
                    cart_service.update_status(course.id, database.STATUS_FAILED)
                    reason = _response_message(response)[:160]
                    _update_course_progress(
                        course.id,
                        status=database.STATUS_FAILED,
                        message=reason or "该课程无法抢到",
                    )
                    _add_event("error", f"{course.name} 无法抢到：{reason or '学校返回终态错误'}")
                    active.remove(course)
                elif action == "transient":
                    with _progress_lock:
                        _progress["retryable_ids"].add(course.id)
                    _update_course_progress(course.id, message="学校系统暂时过载，稍后重试")
                    if not _wait_between_requests(
                        max(
                            _settings[f"{get_enroll_task_state()['mode']}_interval_ms"],
                            NETWORK_BACKOFF_BASE_MS,
                        )
                        / 1000.0
                    ):
                        return GrabOutcome.PAUSED
                    continue
                else:
                    unknown_streak[course.id] += 1
                    snippet = _response_message(response)[:160]
                    if unknown_streak[course.id] >= unknown_streak_limit:
                        reason = f"{course.name} 连续 {unknown_streak_limit} 次收到无法识别的学校返回，任务已保护性暂停：{snippet or '响应内容为空'}"
                        _update_course_progress(course.id, message=reason)
                        pause_enroll_task(reason, source="unknown_response")
                        return GrabOutcome.PAUSED
                    backoff_ms = min(
                        UNKNOWN_BACKOFF_STEP_MS * unknown_streak[course.id], UNKNOWN_BACKOFF_CAP_MS
                    )
                    _update_course_progress(
                        course.id,
                        message=f"学校返回暂时无法识别，继续观察（{unknown_streak[course.id]}/{unknown_streak_limit}）",
                    )
                    if not _wait_between_requests(
                        _mode_interval_seconds(get_enroll_task_state()["mode"])
                        + backoff_ms / 1000.0
                    ):
                        return GrabOutcome.PAUSED
                    continue
            except choose_course.SchoolSessionExpiredError:
                _add_event("warn", "检测到登录已过期，准备自动重新登录")
                return GrabOutcome.SESSION_EXPIRED
            except KeyboardInterrupt:
                logger.info("Enrollment worker interrupted")
                return GrabOutcome.COMPLETED
            except (requests.RequestException, ConnectionError) as exc:
                with _progress_lock:
                    _progress["retryable_ids"].add(course.id)
                unknown_streak[course.id] = 0
                network_streak[course.id] += 1
                message = f"学校请求异常（{network_streak[course.id]}/{MAX_NETWORK_STREAK}）：{str(exc)[:100] or type(exc).__name__}"
                _update_course_progress(course.id, message=message)
                if network_streak[course.id] >= MAX_NETWORK_STREAK:
                    reason = f"{course.name} 连续请求学校失败，任务已保护性暂停"
                    _update_course_progress(course.id, message=reason)
                    pause_enroll_task(reason, source="network_error")
                    return GrabOutcome.PAUSED
                backoff_ms = min(
                    NETWORK_BACKOFF_BASE_MS * (2 ** (network_streak[course.id] - 1)),
                    NETWORK_BACKOFF_CAP_MS,
                )
                if not _wait_between_requests(
                    max(
                        _mode_interval_seconds(get_enroll_task_state()["mode"]), backoff_ms / 1000.0
                    )
                ):
                    return GrabOutcome.PAUSED
                continue
            except Exception as exc:
                logger.exception("Enrollment worker failed for %s", course.name)
                reason = f"{course.name} 抢课任务发生内部异常，已保护性暂停：{type(exc).__name__}"
                _update_course_progress(course.id, message=reason)
                pause_enroll_task(reason, source="internal_error")
                return GrabOutcome.PAUSED

            if active and not _wait_between_requests(
                _mode_interval_seconds(get_enroll_task_state()["mode"])
            ):
                return GrabOutcome.PAUSED

    return GrabOutcome.COMPLETED if not active else GrabOutcome.CONTINUE


def relogin_and_continue(courses: list) -> bool:
    """自动重登录并继续抢课（供命令行兼容路径调用）。"""
    success, error = attempt_automatic_relogin(max_attempts=config.ocr_relogin_max_attempts)
    if success:
        logger.info("OCR automatic re-login succeeded")
        return grab_courses(courses) == GrabOutcome.COMPLETED
    logger.warning("Automatic re-login failed: %s", error)
    return False


def _return_unresolved_to_pending(courses: list) -> None:
    """Keep non-terminal courses retryable if the worker exits unexpectedly."""
    stored = {item["id"]: item for item in cart_service.get_courses_by_status("")}
    for course in courses:
        if stored.get(course.id, {}).get("status") == database.STATUS_IN_PROGRESS:
            cart_service.update_status(course.id, database.STATUS_NOT_STARTED)
            _update_course_progress(
                course.id,
                status=database.STATUS_NOT_STARTED,
                message="任务已结束，课程已保留，可重新启动",
            )


def run_enroll_task(reserved: bool = False):
    """
    后台抢课任务入口

    从数据库读取所有 PENDING 状态的课程，执行抢课循环。
    会话过期时自动重登录，连续多次失败后暂停并保留队列。
    """
    if reserved:
        if not is_enroll_task_running() and not reserve_enroll_task():
            logger.warning("Could not restore the reserved enrollment worker slot")
            return
    elif not reserve_enroll_task():
        logger.warning("Ignored duplicate enrollment worker request")
        return

    try:
        _run_enroll_task()
    finally:
        _release_enroll_task()
        _set_progress_finished()


def _run_enroll_task():
    """Internal worker body; callers must hold the task reservation."""
    courses_data = [
        item
        for item in cart_service.get_courses_by_status(database.STATUS_NOT_STARTED)
        if item.get("auto_enabled", 1)
    ]

    courses = [_course_from_row(item) for item in courses_data]
    courses.sort(key=lambda item: (_course_group(item), item.priority_rank, item.id))

    if not courses:
        logger.info("No pending cart courses")
        return

    _reset_progress(courses)
    logger.info("Starting enrollment worker with %s course(s)", len(courses))
    for c in courses:
        cart_service.update_status(c.id, database.STATUS_IN_PROGRESS)
        logger.info("Enrollment queue item: %s", c.name)
    _add_event("info", f"开始抢课，共 {len(courses)} 门课程")

    consecutive_relogin_failures = 0
    max_failures = max(1, int(config.relogin_max_retries))

    try:
        while True:
            if get_enroll_task_state()["paused"]:
                if not _wait_until_resumed():
                    break
                continue
            try:
                outcome = grab_courses(courses)
            except KeyboardInterrupt:
                logger.info("Enrollment worker interrupted")
                break

            if outcome == GrabOutcome.COMPLETED:
                _add_event("info", "抢课流程结束")
                break
            if outcome == GrabOutcome.CONTINUE:
                continue
            if outcome == GrabOutcome.PAUSED:
                if not _wait_until_resumed():
                    break
                continue

            # 会话过期：尝试自动重登录
            _add_event(
                "warn",
                f"学校会话已过期，正在 OCR 自动重新登录（最多 {config.ocr_relogin_max_attempts} 次）",
            )
            success, error = attempt_automatic_relogin(max_attempts=config.ocr_relogin_max_attempts)
            if success:
                consecutive_relogin_failures = 0
                _add_event("info", "会话过期，已自动重新登录，继续抢课")
                continue

            consecutive_relogin_failures += 1
            _add_event(
                "warn",
                f"自动重登录失败（{consecutive_relogin_failures}/{max_failures}）：{error}",
            )
            if consecutive_relogin_failures >= max_failures:
                reason = "连续多次自动重登录失败，任务已暂停；请手动登录后点击继续"
                for course in courses:
                    _update_course_progress(course.id, message=reason)
                pause_enroll_task(reason, source="relogin_failed")
                if not _wait_until_resumed():
                    break
                consecutive_relogin_failures = 0
                continue
            if (
                not _wait_between_requests(min(2.0 * consecutive_relogin_failures, 8.0))
                and not _wait_until_resumed()
            ):
                break
    finally:
        _return_unresolved_to_pending(courses)
        snapshot = get_enroll_progress()
        counts = snapshot["counts"]
        _add_event(
            "info",
            (
                f"任务收尾：成功 {counts['success']} 门，失败 {counts['failed']} 门，"
                f"保留 {counts['active']} 门"
            ),
        )
