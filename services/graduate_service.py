"""Graduate school wire protocol, separate from undergraduate enrollment.

Queries mirror the official yjsxkapp client. Only submit_selection writes a
course choice; there is deliberately no withdrawal implementation.
"""

from __future__ import annotations

import base64
import io
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import requests
from PIL import Image

import config
from course_models import CoursesResponse
from desencode import str_enc
from services.catalog_pacing import pace_catalog_request
from study_program import GRADUATE_BASE_URL

TIMEOUT = (5, 20)
CAPTCHA_TIMEOUT = (3, 8)
CATEGORIES = {
    "GPN": ("方案内课程", "loadFanCourseInfo.do", "2"),
    "GCROSS": ("跨专业课程", "loadWzyCourseInfo.do", "6"),
    "GALL": ("开课查询", "loadAllCourseInfo.do", None),
}
_window_lock = threading.RLock()
_window: GraduateWindow | None = None
_ocr_lock = threading.Lock()


class GraduateSessionExpiredError(RuntimeError):
    """Graduate session cookie has expired or the server returned a login page."""


class GraduateResponseError(ValueError):
    """The school response does not meet the endpoint's contract."""


@dataclass(frozen=True)
class GraduateWindow:
    batch_code: str
    batch_name: str
    semester: str
    opens_at: datetime | None
    closes_at: datetime | None
    server_now: datetime | None
    received_at: float
    enabled: bool
    strategy: int | None

    def __iter__(self):
        yield self.batch_code
        yield self.batch_name

    def status(self) -> tuple[str, str]:
        if not self.enabled:
            return config.PHASE_CLOSED, "学校当前未开放研究生选课"
        if not all(
            (self.batch_code, self.semester, self.opens_at, self.closes_at, self.server_now)
        ):
            return config.PHASE_UNKNOWN, "学校开放时间或轮次信息不完整，请重新检查状态"
        if self.opens_at >= self.closes_at or self.strategy not in {0, 1, 2}:
            return config.PHASE_UNKNOWN, "学校开放时间或选课策略异常，请重新检查状态"
        now = self.server_now + timedelta(seconds=max(0, time.monotonic() - self.received_at))
        if now < self.opens_at:
            return (
                config.PHASE_CLOSED,
                f"研究生选课尚未开始，将于 {self.opens_at:%Y-%m-%d %H:%M:%S} 开放",
            )
        if now >= self.closes_at:
            return config.PHASE_CLOSED, "研究生选课已结束"
        if self.strategy == 2:
            return config.PHASE_CLOSED, "学校当前策略为不可选可退，不能启动抢课"
        return config.PHASE_AUTOMATIC, "研究生选课进行中"

    def payload(self) -> dict:
        phase, message = self.status()
        return {
            "phase": phase,
            "phase_message": message,
            "batch_code": self.batch_code,
            "batch_name": self.batch_name,
            "semester": self.semester,
            "opens_at": self.opens_at.isoformat(sep=" ") if self.opens_at else "",
            "closes_at": self.closes_at.isoformat(sep=" ") if self.closes_at else "",
            "selection_strategy": {0: "可选可退", 1: "可选不可退", 2: "不可选可退"}.get(
                self.strategy, "未知"
            ),
        }


def _date(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def parse_window(payload: dict) -> GraduateWindow:
    if not isinstance(payload, dict):
        raise GraduateResponseError("研究生开放状态响应格式异常")
    batch = payload.get("lcxx") or {}
    if not isinstance(batch, dict) or "xksfkf" not in payload:
        raise GraduateResponseError("研究生开放状态缺少必要字段")
    strategy = batch.get("XKCL")
    if isinstance(strategy, bool):
        strategy = None
    elif str(strategy) in {"0", "1", "2"}:
        strategy = int(strategy)
    else:
        strategy = None
    return GraduateWindow(
        str(batch.get("WID") or ""),
        str(batch.get("MC") or ""),
        str(batch.get("XNXQDM") or ""),
        _date(batch.get("KFKSSJ")),
        _date(batch.get("KFJSSJ")),
        _date(payload.get("dqsj")),
        time.monotonic(),
        str(payload.get("xksfkf")) == "1",
        strategy,
    )


def clear_window() -> None:
    global _window
    with _window_lock:
        _window = None


def window_payload() -> dict:
    with _window_lock:
        return (
            _window.payload()
            if _window
            else {
                "phase": config.PHASE_UNKNOWN,
                "phase_message": "尚未读取研究生选课开放状态，请重新检查状态",
            }
        )


def _request(method: str, path: str, *, cookie: str = "", data=None, params=None, timeout=TIMEOUT):
    # Fixed origin and no redirects prevent cookies from escaping to an SSO page.
    headers = {
        "Referer": GRADUATE_BASE_URL + "*default/index.do",
        "Origin": "https://ehall.szu.edu.cn",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    if cookie:
        headers["Cookie"] = cookie
    response = requests.request(
        method,
        GRADUATE_BASE_URL + path,
        headers=headers,
        data=data,
        params=params,
        timeout=timeout,
        allow_redirects=False,
    )
    response.encoding = "utf-8"
    return response


def _json(response) -> dict:
    text = str(response.text or "")
    if response.status_code in {301, 302, 303, 307, 308, 401, 403} or (
        "<html" in text.lower()
        and any(word in text for word in ("loginPwd", "loginName", "authserver"))
    ):
        raise GraduateSessionExpiredError("研究生选课登录已过期")
    response.raise_for_status()
    try:
        value = response.json()
    except ValueError as exc:
        raise GraduateResponseError("研究生系统返回了非 JSON 数据") from exc
    if not isinstance(value, dict):
        raise GraduateResponseError("研究生系统响应必须为对象")
    if str(value.get("code")) in {"302", "401", "403"} or any(
        word in str(value.get("msg") or "")
        for word in ("登录超时", "未登录", "请重新登录", "登录已过期")
    ):
        raise GraduateSessionExpiredError("研究生选课登录已过期")
    return value


def refresh_window(*, publish: bool = True) -> GraduateWindow:
    global _window
    result = parse_window(_json(_request("GET", "xsxkHome/loadPublicInfo.do")))
    if publish:
        with _window_lock:
            _window = result
    return result


def publish_window(window: GraduateWindow) -> None:
    global _window
    with _window_lock:
        _window = window


def fetch_batch(student_id: str, cookie: str) -> GraduateWindow:
    profile = _json(_request("GET", "xsxkHome/loadStdInfo.do", cookie=cookie))
    if str(profile.get("XH") or "") != student_id:
        raise GraduateSessionExpiredError("学校未确认当前研究生账号，请重新登录")
    return refresh_window(publish=False)


def merge_cookies(*values: str) -> str:
    pairs = {}
    for value in values:
        for part in str(value or "").split(";"):
            name, sep, content = part.strip().partition("=")
            if sep and name and content and not any(c in name + content for c in "\r\n"):
                pairs[name] = content
    return "; ".join(f"{key}={value}" for key, value in pairs.items())


def _response_cookies(response) -> str:
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in response.cookies)


def validate_text_captcha(value: Any) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"[A-Za-z0-9]{4}", text) else ""


def fetch_captcha() -> dict:
    import logic

    window = refresh_window()
    if not window.enabled:
        raise logic.CaptchaUnavailableError("学校当前未开放研究生登录")
    response = _request("GET", "login/4/vcode.do", timeout=CAPTCHA_TIMEOUT)
    payload = _json(response)
    data = payload.get("data")
    token = data.get("token") if isinstance(data, dict) else None
    if str(payload.get("code")) != "1" or not isinstance(token, str) or not token.strip():
        raise logic.CaptchaResponseError("研究生验证码令牌响应异常")
    cookie = _response_cookies(response)
    image = _request(
        "GET",
        "login/vcode/image.do",
        cookie=cookie,
        params={"vtoken": token},
        timeout=CAPTCHA_TIMEOUT,
    )
    image.raise_for_status()
    cookie = merge_cookies(cookie, _response_cookies(image))
    raw = image.content
    if not raw or len(raw) > logic.MAX_CAPTCHA_BYTES or not cookie:
        raise logic.CaptchaResponseError("研究生验证码图片或 Cookie 不完整")
    try:
        with Image.open(io.BytesIO(raw)) as decoded:
            if not (20 <= decoded.width <= 1000 and 10 <= decoded.height <= 500):
                raise ValueError("unexpected captcha dimensions")
            mime = Image.MIME.get(decoded.format, "image/png")
            decoded.verify()
    except (OSError, ValueError) as exc:
        raise logic.CaptchaResponseError("研究生验证码图片无法解析") from exc
    return {
        "vtoken": token,
        "cookie": cookie,
        "captcha_kind": "text",
        "imageUrl": f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"),
    }


def recognize_captcha(image_url: str) -> str:
    import logic

    try:
        header, encoded = image_url.split(",", 1)
        if not header.startswith("data:image/") or len(encoded) > 3 * 1024 * 1024:
            raise ValueError
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as decoded:
            if decoded.width > 1000 or decoded.height > 500:
                raise ValueError
            decoded.verify()
    except (ValueError, OSError) as exc:
        raise GraduateResponseError("验证码图片无效") from exc
    with _ocr_lock:
        recognizer = logic._ddddocr_engines()[1]
        return validate_text_captcha(recognizer.predict(raw))


def encrypt_password(password: str) -> str:
    return str_enc(password, "1", "2", "3")


def login(student_id: str, vtoken: str, login_pwd: str, code: str, cookie: str) -> dict:
    if not validate_text_captcha(code) or not cookie or not vtoken:
        return {
            "success": False,
            "error_msg": "请输入完整的四位验证码",
            "error_code": "INVALID_CAPTCHA",
        }
    response = _request(
        "POST",
        "login/check/login.do",
        cookie=cookie,
        data={
            "loginName": student_id,
            "loginPwd": login_pwd,
            "verifyCode": code,
            "vtoken": vtoken,
        },
    )
    payload = _json(response)
    result_code = str(payload.get("code"))
    if result_code != "1":
        return {
            "success": False,
            "error_code": result_code,
            "error_msg": {
                "2": "学号或密码不正确",
                "3": "验证码不正确，请刷新后重试",
                "4": "学校在线人数已达上限，请稍后重试",
            }.get(result_code, str(payload.get("msg") or "研究生系统拒绝登录")),
        }
    combined = merge_cookies(cookie, _response_cookies(response))
    profile_response = _request("GET", "xsxkHome/loadStdInfo.do", cookie=combined)
    profile = _json(profile_response)
    combined = merge_cookies(combined, _response_cookies(profile_response))
    if str(profile.get("XH") or "") != student_id or str(profile.get("CODE")) == "0":
        return {
            "success": False,
            "error_msg": "学校未返回该账号的有效研究生学籍信息",
            "error_code": "INVALID_PROFILE",
        }
    # The graduate server authenticates by cookie and returns no API token.
    # This local nonce participates in existing session-generation guards only.
    return {
        "success": True,
        "cookie": combined,
        "token": secrets.token_urlsafe(24),
        "name": profile.get("XM"),
        "error_msg": None,
    }


def automatic_login(
    student_id: str, password: str, max_attempts: int, *, progress=None
) -> tuple[dict, str]:
    import logic

    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    structural = 0
    encrypted = encrypt_password(password)
    for attempt in range(1, max_attempts + 1):
        if progress:
            progress(attempt, max_attempts)
        try:
            captcha = fetch_captcha()
            code = recognize_captcha(captcha["imageUrl"])
            if code:
                result = login(student_id, captcha["vtoken"], encrypted, code, captcha["cookie"])
                if result["success"] or result.get("error_code") != "3":
                    return result, captcha["cookie"]
            structural = 0
        except (logic.CaptchaResponseError, GraduateResponseError):
            structural += 1
            if structural >= logic.STRUCTURAL_CAPTCHA_ABORT_AFTER:
                raise
        if attempt < max_attempts:
            time.sleep(min(0.25 * attempt, 1.0))
    return {
        "success": False,
        "error_msg": f"研究生验证码连续 {max_attempts} 次未通过，请手动登录",
    }, ""


def _rows(payload: dict, key: str) -> list[dict]:
    if key not in payload or not isinstance(payload[key], list):
        raise GraduateResponseError(f"研究生课程响应缺少有效 {key}")
    if any(
        not isinstance(row, dict) or not str(row.get("BJDM") or "").strip() for row in payload[key]
    ):
        raise GraduateResponseError("研究生课程响应缺少教学班编号")
    return payload[key]


def selected_rows(cookie: str, *, timeout=TIMEOUT) -> list[dict]:
    payload = _json(
        _request("GET", "xsxkCourse/loadStdCourseInfo.do", cookie=cookie, timeout=timeout)
    )
    return _rows(payload, "results")


def _schedule(value: Any) -> str:
    text = re.sub(r"<br\s*/?>", "；", str(value or ""), flags=re.I)
    return re.sub(r"[\[【]\s*(\d{1,2}(?:\s*-\s*\d{1,2})?\s*节)\s*[\]】]", r" \1 ", text).strip()


def map_selected(row: dict) -> dict:
    return {
        "teachingClassID": str(row["BJDM"]),
        "courseName": str(row.get("KCMC") or "未命名课程"),
        "courseNumber": str(row.get("KCDM") or ""),
        "teacherName": str(row.get("RKJS") or ""),
        "teachingPlace": _schedule(row.get("PKSJDDMS") or row.get("PKSJDD")),
        "credit": row.get("XF", ""),
        "courseTypeName": str(row.get("KCLBMC") or ""),
        "campusName": "",
    }


def query_selected(cookie: str, *, timeout=TIMEOUT) -> list[dict]:
    return [map_selected(row) for row in selected_rows(cookie, timeout=timeout)]


def query_timetable(cookie: str) -> dict:
    from services.course_service import _map_enrolled_row
    from services.timetable_service import DAY_NAMES, build_timetable

    # The selected list is authoritative for membership. The timetable endpoint
    # supplies structured periods even when the selected row's text is missing.
    courses = [_map_enrolled_row(row) for row in query_selected(cookie)]
    warning = ""
    try:
        payload = _json(_request("GET", "xsxkCourse/loadKbxx.do", cookie=cookie))
        periods = _rows(payload, "results")
    except GraduateSessionExpiredError:
        raise
    except (GraduateResponseError, requests.RequestException):
        periods = []
        warning = "学校详细课表暂不可用，当前按已选课程中的教学安排展示；请稍后刷新"
    by_id = {course["teaching_class_id"]: course for course in courses}
    fragments: dict[str, list[str]] = {}
    for period in periods:
        class_id = str(period["BJDM"])
        if class_id not in by_id:
            continue
        try:
            day, start, end = (int(period[key]) for key in ("XQ", "KSJCDM", "JSJCDM"))
        except (KeyError, TypeError, ValueError):
            continue
        if not (1 <= day <= 7 and 1 <= start <= end <= 14):
            continue
        weeks = str(period.get("ZCMC") or "")
        if weeks and "周" not in weeks:
            weeks += "周"
        fragment = (
            f"{weeks} {DAY_NAMES[day - 1]} {start}-{end}节 {period.get('JASMC') or ''}".strip()
        )
        fragments.setdefault(class_id, [])
        if fragment not in fragments[class_id]:
            fragments[class_id].append(fragment)
        if not by_id[class_id]["teacher_name"]:
            by_id[class_id]["teacher_name"] = str(period.get("JSXM") or "")
    for class_id, schedule in fragments.items():
        by_id[class_id]["teaching_place"] = "；".join(schedule)
    return {
        "courses": courses,
        "total_count": len(courses),
        "timetable": build_timetable(courses),
        "timetable_warning": warning,
    }


def query_catalog(
    course_type: str,
    page: int,
    *,
    cookie: str | None = None,
    keyword: str = "",
    department: str = "",
    hide_conflict: bool = False,
    hide_full: bool = False,
    only_plan: bool = False,
) -> CoursesResponse:
    if course_type not in CATEGORIES or page < 0:
        raise ValueError("不支持的研究生课程类型或页码")
    cookie = config.combined_cookie if cookie is None else cookie
    pace_catalog_request()
    payload = _json(
        _request(
            "POST",
            "xsxkCourse/" + CATEGORIES[course_type][1],
            cookie=cookie,
            data={
                "pageIndex": page + 1,
                "pageSize": 10,
                "sortField": "",
                "sortOrder": "",
                "query_keyword": keyword,
                "query_kkyx": department,
                "query_sfct": "0" if hide_conflict else "",
                "query_sfym": "0" if hide_full else "",
                "query_jxsjhnkc": "1" if only_plan else "0",
            },
        )
    )
    rows = _rows(payload, "datas")
    try:
        total = int(payload["total"])
        reported_page = int(payload["pageIndex"])
        reported_size = int(payload["pageSize"])
    except (KeyError, ValueError, TypeError) as exc:
        raise GraduateResponseError("研究生课程分页数据异常") from exc
    if total < len(rows) or total < 0 or reported_page != page + 1 or reported_size != 10:
        raise GraduateResponseError("研究生课程分页信息不一致")
    selected = {str(row["BJDM"]) for row in selected_rows(cookie)}
    mapped = []
    for row in rows:
        class_id = str(row["BJDM"])
        capacity, count = row.get("KXRS"), row.get("DQRS")
        full = str(capacity).isdigit() and str(count).isdigit() and int(count) >= int(capacity)
        teaching_class = {
            "teachingClassID": class_id,
            "courseNumber": str(row.get("KCDM") or ""),
            "classCapacity": capacity,
            "numberOfSelected": count,
            "isChoose": "1" if class_id in selected else "0",
            "isConflict": "1" if str(row.get("IS_CONFLICT")) == "1" else "0",
            "isFull": "1" if full else "0",
            "courseIndex": row.get("BJMC"),
            "teacherName": row.get("RKJS"),
            "teachingPlace": _schedule(row.get("PKSJDDMS") or row.get("PKSJDD")),
            "courseTypeName": row.get("KCLBMC"),
            "extInfo": row.get("XKBZ"),
        }
        mapped.append(
            {
                "courseNumber": str(row.get("KCDM") or ""),
                "courseName": row.get("KCMC"),
                "credit": row.get("KCXF", row.get("XF", "")),
                "departmentName": row.get("RWKKDWMC"),
                "courseNatureName": row.get("KCCCMC"),
                "hours": row.get("KCZXS"),
                "type": course_type,
                "number": 1,
                "selected": class_id in selected,
                "tcList": [teaching_class],
                "campusName": "",
            }
        )
    return CoursesResponse.from_dict({"code": "1", "totalCount": total, "dataList": mapped})


def departments(cookie: str) -> list[dict]:
    payload = _json(_request("GET", "xsxkHome/loadDwXb.do", cookie=cookie))
    rows = payload.get("dwxb")
    if isinstance(rows, str):
        rows = json.loads(rows)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise GraduateResponseError("开课院系列表格式异常")
    return [
        {"code": str(row.get("DM") or ""), "name": str(row.get("MC") or "")}
        for row in rows
        if row.get("DM") and row.get("MC")
    ]


def submit_selection(class_id: str, course_type: str):
    if course_type not in CATEGORIES or CATEGORIES[course_type][2] is None:
        raise ValueError("开课查询不能直接提交选课，请从方案内或跨专业课程加入清单")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", class_id):
        raise ValueError("研究生教学班编号无效")
    phase = window_payload()
    if phase["phase"] != config.PHASE_AUTOMATIC:
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {"code": "2", "msg": "当前不在选课时间：" + phase["phase_message"]}, ensure_ascii=False
        ).encode()
        response.encoding = "utf-8"
        return response
    return _request(
        "POST",
        "xsxkCourse/choiceCourse.do",
        cookie=config.combined_cookie,
        data={
            "bjdm": class_id,
            "lx": CATEGORIES[course_type][2],
        },
    )
