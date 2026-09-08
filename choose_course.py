"""Wire-compatible school enrollment and enrolled-course requests."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

import config
from campus import DEFAULT_CAMPUS_CODE, normalize_campus_code
from school_session import is_session_expired_response
from services import backend_service

REQUEST_TIMEOUT = (5, 20)
logger = logging.getLogger(__name__)


class SchoolSessionExpiredError(RuntimeError):
    """Raised when the school responds with an expired-session signal."""


def _school_request(
    path: str,
    *,
    data=None,
    params=None,
    token: str = "",
    cookie: str | None = None,
    read_only: bool = False,
    timeout=REQUEST_TIMEOUT,
):
    def sender(**kwargs):
        kwargs.pop("method", None)
        kwargs.pop("json", None)
        return requests.post(**kwargs)

    return backend_service.request_with_failover(
        "POST",
        path,
        sender=sender,
        data=data,
        params=params,
        token=token,
        cookie=cookie,
        timeout=timeout,
        read_only=read_only,
        # WebVPN is a read-only fallback. Enrollment and withdrawal always use
        # the primary school endpoint, even if a prior query used WebVPN.
        preference=None if read_only else config.BACKEND_PRIMARY,
    )


def query_enrolled_courses(
    combined_cookie: str,
    token: str,
    *,
    timeout=REQUEST_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return the current student's selected courses from the school system."""
    timestamp = int(time.time() * 1000)
    from study_program import is_graduate

    if is_graduate():
        from services import graduate_service

        try:
            return graduate_service.query_selected(
                combined_cookie, timeout=timeout or REQUEST_TIMEOUT
            )
        except graduate_service.GraduateSessionExpiredError as exc:
            raise SchoolSessionExpiredError(str(exc)) from exc
    response = _school_request(
        f"elective/courseResult.do?timestamp={timestamp}&studentCode={config.student_id}",
        token=token,
        cookie=combined_cookie,
        read_only=True,
        timeout=timeout,
    )

    if is_session_expired_response(
        status_code=response.status_code,
        text=response.text,
    ):
        raise SchoolSessionExpiredError("school session expired")
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        if is_session_expired_response(text=response.text):
            raise SchoolSessionExpiredError("school returned the login page") from exc
        raise ValueError("school enrolled-course response was not JSON") from exc

    if not isinstance(payload, dict):
        raise ValueError("school enrolled-course response must be an object")
    if is_session_expired_response(
        status_code=response.status_code,
        code=payload.get("code"),
        text=response.text,
    ):
        raise SchoolSessionExpiredError("school session expired")

    code = payload.get("code")
    if code is not None and str(code).strip().lower() not in {"1", "200", "ok", "success"}:
        raise ValueError(f"school enrolled-course query was rejected with code {code}")
    if "dataList" not in payload:
        raise ValueError("school enrolled-course response has no dataList")

    data_list = payload.get("dataList")
    if data_list is None:
        data_list = []
    if not isinstance(data_list, list):
        raise ValueError("school enrolled-course dataList must be a list")

    return data_list


def enrolled_teaching_class_id(item: Any) -> str:
    """Return the canonical teaching-class ID from one selected-course row."""
    if not isinstance(item, dict):
        return ""
    return str(
        item.get("teachingClassID")
        or item.get("teachingClassId")
        or item.get("teaching_class_id")
        or ""
    ).strip()


def enrolled_teaching_class_ids(items: list[dict[str, Any]]) -> set[str]:
    """Strictly validate selected-course rows before using them as proof."""
    ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"school enrolled-course row {index} must be an object")
        class_id = enrolled_teaching_class_id(item)
        if not class_id:
            raise ValueError(f"school enrolled-course row {index} has no teaching-class ID")
        ids.add(class_id)
    return ids


def submit_course_selection(
    class_id: str,
    teaching_class_type: str,
    campus_code: str = DEFAULT_CAMPUS_CODE,
):
    """Submit one course-selection request using the school's legacy payload."""
    from study_program import is_graduate

    if is_graduate():
        from services.graduate_service import submit_selection

        return submit_selection(class_id, teaching_class_type)
    normalized_campus = normalize_campus_code(campus_code)
    form_data = {
        "addParam": (
            r"""{"data":{"operationType":"1","studentCode":%s,"electiveBatchCode":%s,"teachingClassId":%s,"isMajor":"1","campus":"%s","teachingClassType":%s,"chooseVolunteer":"1"}}"""  # noqa: UP031 - exact legacy wire template
            % (
                str(config.student_id),
                config.elective_batch_code,
                class_id,
                normalized_campus,
                teaching_class_type,
            )
        )
    }
    logger.info(
        "Submitting enrollment request: class=%s type=%s campus=%s",
        class_id,
        teaching_class_type,
        normalized_campus,
    )
    return _school_request(
        "elective/volunteer.do",
        data=form_data,
        token=config.token,
        cookie=backend_service.cookie_header(backend_service.get_profile(config.BACKEND_PRIMARY)),
    )


__all__ = [
    "REQUEST_TIMEOUT",
    "SchoolSessionExpiredError",
    "enrolled_teaching_class_id",
    "enrolled_teaching_class_ids",
    "query_enrolled_courses",
    "submit_course_selection",
]
