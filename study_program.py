"""Process-level choice of school system, selected before runtime imports."""

from __future__ import annotations

import os

PROGRAM_ENV = "COURSE_SELECT_PROGRAM"
UNDERGRADUATE = "undergraduate"
GRADUATE = "graduate"
GRADUATE_BASE_URL = "https://ehall.szu.edu.cn/yjsxkapp/sys/xsxkapp/"


def current_program() -> str:
    value = os.getenv(PROGRAM_ENV, UNDERGRADUATE).strip().lower()
    if value not in {UNDERGRADUATE, GRADUATE}:
        raise ValueError("COURSE_SELECT_PROGRAM must be undergraduate or graduate")
    return value


def is_graduate() -> bool:
    return current_program() == GRADUATE


def program_payload() -> dict:
    graduate = is_graduate()
    return {
        "program": current_program(),
        "program_label": "研究生" if graduate else "本科生",
        "captcha_kind": "text" if graduate else "click",
        "supports_campus_switch": not graduate,
        "supports_webvpn": not graduate,
        "school_url": GRADUATE_BASE_URL + "*default/index.do"
        if graduate
        else "http://bkxk.szu.edu.cn/xsxkapp/sys/xsxkapp/*default/index.do",
    }
