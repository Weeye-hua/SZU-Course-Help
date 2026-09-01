"""Course catalog and selected-course service boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import requests

import choose_course
import config
from course_list import (
    CatalogRequestContext,
    CourseQueryFailure,
    minor_course,
    mooc_course,
    non_programmed_course,
    programmed_course,
    public_course,
    query_course_page,
    recommended_course,
    sport_course,
)
from course_models import CoursesResponse
from school_session import is_session_expired_response
from services.catalog_pacing import pace_catalog_request

logger = logging.getLogger(__name__)
SESSION_EXPIRED = "SESSION_EXPIRED"
COURSE_QUERY_REJECTED = "COURSE_QUERY_REJECTED"
COURSE_QUERY_THROTTLED = "COURSE_QUERY_THROTTLED"
COURSE_RESPONSE_INVALID = "COURSE_RESPONSE_INVALID"
COURSE_WINDOW_CLOSED = "COURSE_WINDOW_CLOSED"

COURSE_WINDOW_CLOSED_KEYWORDS = (
    "非选课时间",
    "不在选课时间",
    "未开放",
    "尚未开放",
    "未开始",
    "已结束",
    "已截止",
    "暂停选课",
)

COURSE_THROTTLE_KEYWORDS = (
    "请求过快",
    "请求太快",
    "请求过于频繁",
    "请求频繁",
    "操作过于频繁",
    "操作频繁",
    "访问过于频繁",
    "访问频繁",
    "提交过于频繁",
    "提交频繁",
    "too many requests",
    "rate limit",
)

CourseQuery = Callable[[int], CoursesResponse | CourseQueryFailure]
COURSE_TYPE_MAP: dict[str, tuple[str, CourseQuery]] = {
    "TJKC": ("本班课程(推荐)", recommended_course),
    "FANKC": ("方案内课程", programmed_course),
    "FAWKC": ("方案外课程", non_programmed_course),
    "XGXK": ("校公选课", public_course),
    "TYKC": ("体育课程", sport_course),
    "MOOC": ("慕课", mooc_course),
    "FXKC": ("辅修课程", minor_course),
}

# The school endpoint is known to reject this category for some students.
UNSUPPORTED_TYPES = frozenset({"FXKC"})


def is_supported_type(course_type: str) -> bool:
    """Return whether this installation exposes a school course category."""
    normalized = str(course_type or "").strip().upper()
    return normalized in COURSE_TYPE_MAP and normalized not in UNSUPPORTED_TYPES


def get_unsupported_message(course_type: str) -> str:
    """Return a stable user-facing category error."""
    if course_type == "FXKC":
        return "辅修课程暂不支持，如需选辅修课请前往学校官方选课系统"
    return f"不支持的课程类型: {course_type}"


def _looks_like_closed_window(message: str) -> bool:
    normalized = str(message or "").strip()
    return any(keyword in normalized for keyword in COURSE_WINDOW_CLOSED_KEYWORDS)


def _looks_like_throttling(message: str) -> bool:
    normalized = str(message or "").strip().lower()
    return any(keyword in normalized for keyword in COURSE_THROTTLE_KEYWORDS)


def query_courses(
    course_type: str,
    page: int,
    context: CatalogRequestContext | None = None,
) -> tuple[bool, Any, str]:
    """Query one zero-based page and normalize school failures."""
    normalized_type = str(course_type or "").strip().upper()
    if page < 0:
        return False, "页码必须大于等于 0", ""
    if normalized_type not in COURSE_TYPE_MAP:
        return False, get_unsupported_message(normalized_type), ""
    if normalized_type in UNSUPPORTED_TYPES:
        return False, get_unsupported_message(normalized_type), ""

    type_name, query_function = COURSE_TYPE_MAP[normalized_type]
    pace_catalog_request()
    result = query_course_page(normalized_type, page, context) if context else query_function(page)

    if isinstance(result, CoursesResponse):
        if is_session_expired_response(code=result.code, text=result.msg):
            logger.info("School session expired while querying %s", type_name)
            return False, SESSION_EXPIRED, type_name
        if str(result.code) == "1":
            response = result.to_course_list_response()
            logger.info(
                "Course query %s: total=%s page_count=%s",
                type_name,
                response.total_count,
                len(response.courses),
            )
            return True, response, type_name

        logger.warning(
            "Course query %s failed with code=%s message=%s",
            type_name,
            result.code,
            result.msg,
        )
        if _looks_like_closed_window(result.msg):
            return False, COURSE_WINDOW_CLOSED, type_name
        normalized_code = str(result.code or "").strip().lower()
        if normalized_code in {"429", "too_many_requests", "rate_limit"} or _looks_like_throttling(
            result.msg
        ):
            return False, COURSE_QUERY_THROTTLED, type_name
        return False, COURSE_QUERY_REJECTED, type_name

    if is_session_expired_response(
        status_code=result.status_code,
        text=result.text,
    ):
        logger.info(
            "School session expired while querying %s (HTTP %s)",
            type_name,
            result.status_code,
        )
        return False, SESSION_EXPIRED, type_name

    if result.status_code == 429 or _looks_like_throttling(result.text):
        logger.info("School throttled course query %s", type_name)
        return False, COURSE_QUERY_THROTTLED, type_name

    logger.warning(
        "Course query %s returned malformed data: HTTP=%s error=%s",
        type_name,
        result.status_code,
        type(result.error).__name__,
    )
    return False, COURSE_RESPONSE_INVALID, type_name


def _map_enrolled_row(item: dict) -> dict[str, str]:
    """Extract display fields without trusting optional school values."""
    if not isinstance(item, dict):
        return {}
    return {
        "teaching_class_id": choose_course.enrolled_teaching_class_id(item),
        "course_name": str(item.get("courseName") or "未命名课程"),
        "teacher_name": str(item.get("teacherName") or ""),
        "teaching_place": str(item.get("teachingPlace") or ""),
        "credit": str(item.get("credit") or ""),
        "course_number": str(item.get("courseNumber") or ""),
        "course_type_name": str(item.get("courseTypeName") or item.get("typeName") or ""),
        "campus_name": str(item.get("campusName") or ""),
    }


def get_enrolled_courses() -> tuple[bool, list[dict[str, str]] | str]:
    """Return selected courses, distinguishing expiry from network failure."""
    if not config.token or not config.combined_cookie:
        return False, SESSION_EXPIRED

    try:
        data_list = choose_course.query_enrolled_courses(
            config.combined_cookie,
            config.token,
        )
    except choose_course.SchoolSessionExpiredError:
        logger.info("School session expired while querying selected courses")
        return False, SESSION_EXPIRED
    except requests.RequestException:
        logger.exception("Network error while querying selected courses")
        return False, "获取已选课程失败，请稍后重试"
    except (TypeError, ValueError):
        logger.exception("Malformed selected-course response")
        return False, "获取已选课程失败，请稍后重试"

    courses = [mapped for item in data_list if (mapped := _map_enrolled_row(item))]
    logger.info("Selected-course query returned %s course(s)", len(courses))
    return True, courses


__all__ = [
    "COURSE_TYPE_MAP",
    "COURSE_QUERY_REJECTED",
    "COURSE_QUERY_THROTTLED",
    "COURSE_RESPONSE_INVALID",
    "COURSE_WINDOW_CLOSED",
    "SESSION_EXPIRED",
    "get_enrolled_courses",
    "get_unsupported_message",
    "is_supported_type",
    "query_courses",
]
