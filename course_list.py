"""School course-list requests.

The endpoint names and form fields in this module mirror the school client and
must remain wire-compatible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import requests

import config
from campus import DEFAULT_CAMPUS_CODE, normalize_campus_code
from course_models import CoursesResponse
from services import backend_service

REQUEST_TIMEOUT = (5, 20)


@dataclass(frozen=True, slots=True)
class CourseQueryFailure:
    """A non-JSON or malformed school course response."""

    text: str
    status_code: int
    error: Exception


@dataclass(frozen=True, slots=True)
class CatalogRequestContext:
    """Immutable school-session fields used by a background catalog read."""

    student_id: str
    campus_code: str
    batch_code: str
    token: str
    cookie: str
    backend_preference: str = field(default_factory=backend_service.get_preference)

    def __post_init__(self) -> None:
        if not all((self.student_id, self.campus_code, self.batch_code, self.token, self.cookie)):
            raise ValueError("complete catalog request context is required")


def get_headers() -> dict[str, str]:
    """Build headers from the latest in-memory school session."""
    profile = backend_service.session_profile()
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Cookie": backend_service.cookie_header(profile),
        "Host": profile.host,
        "Origin": profile.origin,
        "Referer": f"{profile.base_url}*default/grablessons.do?token={config.token}",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"),
        "X-Requested-With": "XMLHttpRequest",
        "token": config.token,
    }


def _query_courses(
    teaching_class_type: str,
    page: int,
    endpoint: str = "elective/programCourse.do",
    context: CatalogRequestContext | None = None,
) -> CoursesResponse | CourseQueryFailure:
    """Query one zero-based page without changing the school request contract."""
    context = context or CatalogRequestContext(
        student_id=str(config.student_id),
        campus_code=str(getattr(config, "campus_code", DEFAULT_CAMPUS_CODE)),
        batch_code=str(config.elective_batch_code),
        token=str(config.token),
        cookie=str(config.combined_cookie),
        backend_preference=backend_service.get_preference(),
    )
    campus_code = normalize_campus_code(
        context.campus_code,
        fallback=DEFAULT_CAMPUS_CODE,
    )
    query_setting = {
        "data": {
            "studentCode": context.student_id,
            "campus": campus_code,
            "electiveBatchCode": context.batch_code,
            "isMajor": "1",
            "teachingClassType": teaching_class_type,
            "checkConflict": "2",
            "checkCapacity": "2",
            "queryContent": "YCJX:2,MOOC:2,",
        },
        "pageSize": "10",
        "pageNumber": page,
        "order": "",
        "orderBy": "courseNumber",
    }

    def sender(**kwargs):
        kwargs.pop("method", None)
        return requests.post(**kwargs)

    response = backend_service.request_with_failover(
        "POST",
        endpoint,
        sender=sender,
        token=context.token,
        content_type="application/x-www-form-urlencoded; charset=UTF-8",
        accept="application/json, text/javascript, */*; q=0.01",
        cookie=context.cookie,
        data={
            "querySetting": json.dumps(
                query_setting,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        },
        timeout=REQUEST_TIMEOUT,
        read_only=True,
        preference=context.backend_preference,
    )
    try:
        return CoursesResponse.from_response(response)
    except (AttributeError, TypeError, ValueError) as exc:
        return CourseQueryFailure(response.text, response.status_code, exc)


def recommended_course(page: int) -> CoursesResponse | CourseQueryFailure:
    """Query recommended/home-class courses (TJKC)."""
    return _query_courses("TJKC", page, "elective/recommendedCourse.do")


def programmed_course(page: int) -> CoursesResponse | CourseQueryFailure:
    """Query courses inside the student's program (FANKC)."""
    return _query_courses("FANKC", page)


def non_programmed_course(page: int) -> CoursesResponse | CourseQueryFailure:
    """Query courses outside the student's program (FAWKC)."""
    return _query_courses("FAWKC", page)


def public_course(page: int) -> CoursesResponse | CourseQueryFailure:
    """Query university-wide public courses (XGXK)."""
    return _query_courses("XGXK", page)


def sport_course(page: int) -> CoursesResponse | CourseQueryFailure:
    """Query physical-education courses (TYKC)."""
    return _query_courses("TYKC", page)


def minor_course(page: int) -> CoursesResponse | CourseQueryFailure:
    """Query minor-program courses (FXKC)."""
    return _query_courses("FXKC", page)


def mooc_course(page: int) -> CoursesResponse | CourseQueryFailure:
    """Query online courses (MOOC)."""
    return _query_courses("MOOC", page)


def query_course_page(
    course_type: str,
    page: int,
    context: CatalogRequestContext,
) -> CoursesResponse | CourseQueryFailure:
    """Query one category with a captured context instead of mutable globals."""
    normalized = str(course_type or "").strip().upper()
    endpoints = {
        "TJKC": ("TJKC", "elective/recommendedCourse.do"),
        "FANKC": ("FANKC", "elective/programCourse.do"),
        "FAWKC": ("FAWKC", "elective/programCourse.do"),
        "XGXK": ("XGXK", "elective/programCourse.do"),
        "TYKC": ("TYKC", "elective/programCourse.do"),
        "MOOC": ("MOOC", "elective/programCourse.do"),
        "FXKC": ("FXKC", "elective/programCourse.do"),
    }
    if normalized not in endpoints:
        raise ValueError(f"unsupported course type: {normalized}")
    teaching_class_type, endpoint = endpoints[normalized]
    return _query_courses(teaching_class_type, page, endpoint, context)


__all__ = [
    "CourseQueryFailure",
    "CatalogRequestContext",
    "REQUEST_TIMEOUT",
    "minor_course",
    "mooc_course",
    "non_programmed_course",
    "programmed_course",
    "public_course",
    "recommended_course",
    "query_course_page",
    "sport_course",
]
