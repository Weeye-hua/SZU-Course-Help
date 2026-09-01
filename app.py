"""FastAPI controller and local static-file server."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.responses import FileResponse, JSONResponse

import config
import database
import logic
from campus import (
    DEFAULT_CAMPUS_CODE,
    campus_name,
    campus_options_payload,
    get_campus,
)
from card_key import verify_card_key
from course_list import CatalogRequestContext
from logging_config import configure_logging
from project_paths import external_process_env, resource_path
from services import backend_service, cart_service, webvpn_auth_service
from services.auth_service import (
    LOGIN_ERROR_MSG,
    attempt_automatic_relogin,
    automatic_relogin_available,
    clear_elective_batch,
    clear_login_state,
    encrypt_password,
    get_session_snapshot,
    perform_school_login,
    refresh_elective_batch,
    save_login_state,
    set_current_campus,
    start_automatic_relogin,
    update_backend_preference,
    validate_login_params,
)
from services.cache_service import get_no_cache_headers
from services.course_cache_service import (
    CatalogCacheScope,
    get_latest_full_for_student,
    get_latest_page_for_student,
    scope_from_snapshot,
)
from services.course_cache_service import get_full as get_full_course_cache
from services.course_cache_service import get_page as get_course_cache
from services.course_cache_service import put_full as put_full_course_cache
from services.course_cache_service import put_page as put_course_cache
from services.course_service import (
    COURSE_QUERY_REJECTED,
    COURSE_RESPONSE_INVALID,
    COURSE_WINDOW_CLOSED,
    SESSION_EXPIRED,
    get_enrolled_courses,
    get_unsupported_message,
    is_supported_type,
    query_courses,
)
from services.enroll_service import (
    add_cart_course,
    get_enroll_progress,
    get_enroll_settings,
    get_enroll_task_state,
    is_enroll_task_running,
    pause_enroll_task,
    remove_cart_course,
    resume_enroll_task,
    retry_cart_course,
    set_enroll_mode,
    start_enroll_worker,
    stop_enroll_task,
    update_cart_course_preferences,
    update_cart_course_priorities,
    update_enroll_settings,
)
from services.session_manager import session_manager
from services.timetable_service import build_timetable
from services.webvpn_auth_service import ControlledBrowserUnavailableError

SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 8000
RUNTIME_PORT_ENV = "COURSE_SELECT_RUNTIME_PORT"
UI_ASSET_BUILD = "20260901.1"
logger = logging.getLogger(__name__)
OFFICIAL_SCHOOL_HOME_URL = f"{config.SCHOOL_BASE_URL}*default/index.do"


def _preferred_port() -> int:
    try:
        port = int(os.getenv("COURSE_SELECT_PORT", str(DEFAULT_SERVER_PORT)))
    except ValueError:
        return DEFAULT_SERVER_PORT
    return port if 1 <= port <= 65535 else DEFAULT_SERVER_PORT


def _find_available_port(preferred: int, attempts: int = 20) -> int:
    """Choose a local port early so terminal output and browser URL stay aligned."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    last_candidate = min(preferred + attempts - 1, 65535)
    for candidate in range(preferred, last_candidate + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((SERVER_HOST, candidate))
            except OSError:
                continue
            return candidate
    raise RuntimeError(f"端口 {preferred} 至 {last_candidate} 均被占用")


def _server_port() -> int:
    """Reuse the port selected by the reload supervisor in child processes."""
    runtime_port = os.getenv(RUNTIME_PORT_ENV, "").strip()
    if runtime_port:
        try:
            port = int(runtime_port)
        except ValueError:
            port = 0
        if 1 <= port <= 65535:
            return port
    return _find_available_port(_preferred_port())


SERVER_PORT = _server_port()
LOCAL_ORIGINS = (
    f"http://127.0.0.1:{SERVER_PORT}",
    f"http://localhost:{SERVER_PORT}",
)

_runtime_prefill = {"student_id": "", "card_key": ""}


app = FastAPI(title="深大抢课助手 API", version="3.6.4")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"^http://(?:127\.0\.0\.1|localhost|\[::1\]):[1-9]\d{0,4}$",
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def restrict_api_origin(request: Request, call_next):
    """Reject browser API requests originating outside this local service."""
    path = request.url.path
    if path == "/api" or path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin and not _is_local_service_origin(request, origin):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "message": "请求来源不是本地服务，已拒绝访问",
                    "is_error": True,
                    "error_code": "INVALID_ORIGIN",
                    "retryable": False,
                    "requires_manual_login": False,
                },
            )
    return await call_next(request)


def _is_local_service_origin(request: Request, origin: str) -> bool:
    """Match the origin to this loopback endpoint, including reload port drift."""
    try:
        origin_url = urlsplit(origin)
        request_url = request.url
        origin_port = origin_url.port or (443 if origin_url.scheme == "https" else 80)
        request_port = request_url.port or (443 if request_url.scheme == "https" else 80)
    except ValueError:
        return False

    # TestClient uses a synthetic host, so retain the explicit configured list
    # for it while production requests must match their actual loopback port.
    if request_url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return origin in LOCAL_ORIGINS

    return (
        origin_url.scheme == request_url.scheme == "http"
        and origin_url.hostname in {"127.0.0.1", "localhost", "::1"}
        and not origin_url.username
        and not origin_url.password
        and not origin_url.path
        and not origin_url.query
        and not origin_url.fragment
        and origin_port == request_port
    )


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    student_id: str = Field(min_length=6, max_length=12, pattern=r"^\d+$")
    password: str = Field(min_length=1, max_length=256)
    card_key: str = Field(min_length=1, max_length=2048)
    vtoken: str = Field(min_length=1, max_length=512)
    verify_code: list[list[int]] = Field(
        alias="verifyCode",
        min_length=4,
        max_length=4,
    )
    cookie: str = Field(min_length=1, max_length=8192)
    backend: str = Field(default=config.BACKEND_AUTO, pattern=r"^(auto|primary|webvpn)$")


class BackendSelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str = Field(pattern=r"^(auto|primary|webvpn)$")


class WebVPNAuthStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_path: str = Field(
        default="/xsxkapp/sys/xsxkapp/*default/index.do",
        min_length=1,
        max_length=512,
    )


class ApiMessage(BaseModel):
    message: str
    is_error: bool


class CartCourse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(min_length=1, max_length=128)
    course_type: str = Field(
        alias="type",
        min_length=1,
        max_length=16,
        pattern=r"^[A-Z]+$",
    )
    name: str = Field(min_length=1, max_length=512)
    campus_code: str = Field(default=DEFAULT_CAMPUS_CODE, pattern=r"^\d{2}$")
    campus_name: str = Field(default="", max_length=64)
    teaching_place: str = Field(default="", max_length=512)
    course_name: str = Field(default="", max_length=256)
    teacher_name: str = Field(default="", max_length=128)
    credit: str = Field(default="", max_length=32)
    is_choose: str = Field(default="", max_length=8)
    is_conflict: str = Field(default="", max_length=8)
    is_full: str = Field(default="", max_length=8)
    status: str = Field(default="", max_length=16)
    auto_enabled: bool = True
    priority_group: str = Field(default="", max_length=256)
    priority_rank: int = Field(default=0, ge=0, le=100000)
    course_number: str = Field(default="", max_length=128)
    time_signature: str = Field(default="", max_length=256)
    number_of_selected: str = Field(default="", max_length=32)
    class_capacity: str = Field(default="", max_length=32)

    @property
    def type(self) -> str:
        """Expose the school wire name to legacy persistence/service code."""
        return self.course_type


class CartPreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_enabled: StrictBool | None = None
    priority_group: str | None = Field(default=None, max_length=256, strict=True)
    priority_rank: int | None = Field(default=None, ge=0, le=100000, strict=True)


class EnrollmentStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed_phase: bool = False


class EnrollmentModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = Field(pattern=r"^(boost|normal|scan)$")


class EnrollmentSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boost_interval_ms: int | None = Field(default=None, ge=0, le=300000)
    normal_interval_ms: int | None = Field(default=None, ge=0, le=300000)
    scan_interval_ms: int | None = Field(default=None, ge=0, le=300000)
    boost_failure_limit: int | None = Field(default=None, ge=1, le=1_000_000, strict=True)
    normal_failure_limit: int | None = Field(default=None, ge=1, le=1_000_000, strict=True)
    mode: str | None = Field(default=None, pattern=r"^(boost|normal|scan)$")


class CampusSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campus_code: str = Field(pattern=r"^\d{2}$")


static_dir = resource_path("static_dist")
_runtime_start_lock = threading.Lock()
_runtime_started = False


def _start_runtime_services() -> None:
    """Start the one process-local session coordinator once."""
    global _runtime_started
    with _runtime_start_lock:
        if _runtime_started and session_manager.keepalive_running:
            return
        started = session_manager.start_keepalive(
            _keep_alive_once,
            is_enroll_task_running,
            interval_seconds=KEEP_ALIVE_INTERVAL_SECONDS,
        )
        _runtime_started = started or session_manager.keepalive_running
        if started:
            logger.info(
                "Session coordinator started (active-task keep-alive every %ds)",
                KEEP_ALIVE_INTERVAL_SECONDS,
            )


async def startup_runtime_services() -> None:
    _start_runtime_services()


async def shutdown_runtime_services() -> None:
    global _runtime_started
    session_manager.stop_keepalive()
    with _runtime_start_lock:
        _runtime_started = session_manager.keepalive_running
    webvpn_auth_service.close_auth()
    database.DatabaseManager.close_all()


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    """Manage the process-level session coordinator and browser cleanup."""
    await startup_runtime_services()
    try:
        yield
    finally:
        await shutdown_runtime_services()


app.router.lifespan_context = app_lifespan


def configure_runtime_prefill(student_id: str, card_key: str) -> None:
    """Set one-process login defaults generated by the terminal flow."""
    _runtime_prefill["student_id"] = str(student_id).strip()
    _runtime_prefill["card_key"] = str(card_key).strip()


def get_server_url() -> str:
    return f"http://{SERVER_HOST}:{SERVER_PORT}"


def get_frontend_url(path: str = "/") -> str:
    """Return a stable local frontend URL."""
    normalized_path = f"/{str(path).lstrip('/')}"
    return f"{get_server_url()}{normalized_path}"


def get_login_url() -> str:
    return get_frontend_url("/login")


def _cart_from_row(row: dict) -> CartCourse:
    return CartCourse(
        id=row.get("id", ""),
        course_type=row.get("type", ""),
        name=row.get("name", ""),
        campus_code=row.get("campus_code", DEFAULT_CAMPUS_CODE),
        campus_name=(
            row.get("campus_name", "") or campus_name(row.get("campus_code", DEFAULT_CAMPUS_CODE))
        ),
        teaching_place=row.get("teaching_place", ""),
        course_name=row.get("course_name", ""),
        teacher_name=row.get("teacher_name", ""),
        credit=row.get("credit", ""),
        status=row.get("status", ""),
        auto_enabled=bool(row.get("auto_enabled", 1)),
        priority_group=row.get("priority_group", ""),
        priority_rank=row.get("priority_rank", 0),
        course_number=row.get("course_number", ""),
        time_signature=row.get("time_signature", ""),
    )


def _api_error(
    status_code: int,
    message: str,
    error_code: str,
    *,
    retryable: bool,
    requires_manual_login: bool = False,
    **extra,
):
    """Return one stable error envelope for frontend recovery decisions."""
    return JSONResponse(
        status_code=status_code,
        content={
            "message": message,
            "is_error": True,
            "error_code": error_code,
            "retryable": retryable,
            "requires_manual_login": requires_manual_login,
            **extra,
        },
        headers=get_no_cache_headers(),
    )


def _not_logged_in_response(message: str | None = None, error_code: str | None = None):
    snapshot = get_session_snapshot()
    task_state = get_enroll_task_state()
    if snapshot["relogin_in_progress"] or (
        task_state["running"]
        and not task_state["paused"]
        and snapshot["relogin_status"] == "failed"
    ):
        return _api_error(
            409,
            "学校会话正在自动恢复，请稍候；课程和抢课任务不会丢失",
            "SESSION_RECOVERY_IN_PROGRESS",
            retryable=True,
        )
    return _api_error(
        401,
        message or "登录状态无效，请重新登录",
        error_code or "NOT_LOGGED_IN",
        retryable=False,
        requires_manual_login=True,
    )


def _session_payload() -> dict:
    snapshot = get_session_snapshot()
    task_state = get_enroll_task_state()
    batch_name = str(snapshot["batch_name"])
    phase = config.classify_elective_phase(batch_name)
    return {
        **snapshot,
        "phase": phase,
        "automatic_enroll_allowed": phase == config.PHASE_AUTOMATIC,
        "task_running": task_state["running"],
        "task_paused": task_state["paused"],
        "task_pause_acknowledged": task_state["pause_acknowledged"],
        "task_pause_reason": task_state["pause_reason"],
        "task_pause_source": task_state["pause_source"],
        "task_paused_at": task_state["paused_at"],
        "task_stopping": task_state["stopping"],
        "task_stopping_reason": task_state["stopping_reason"],
        "task_queue_revision": task_state["queue_revision"],
        "campus_options": campus_options_payload(),
        **backend_service.backend_payload(),
    }


def _raise_if_session_expired_result(result) -> None:
    """Turn tuple-style school service expiry results into one shared signal."""
    if (
        isinstance(result, tuple)
        and len(result) >= 2
        and result[0] is False
        and result[1] == SESSION_EXPIRED
    ):
        raise logic.SchoolBatchSessionExpiredError("学校登录状态已过期")


def _automatic_relogin_response(
    error: str,
    *,
    status_code: int = 401,
    error_code: str = "SESSION_RECOVERY_FAILED",
):
    """Return the common recoverable response when OCR login cannot finish."""
    return _api_error(
        status_code,
        "登录已过期且 OCR 自动重登录失败，请手动登录",
        error_code,
        retryable=False,
        requires_manual_login=True,
        recovery_message=str(error or "自动重登录失败"),
    )


@app.get("/api/bootstrap")
async def api_bootstrap():
    """Return local runtime defaults and safety guidance for the login page."""
    return JSONResponse(
        content={
            "student_id": _runtime_prefill["student_id"],
            "card_key": _runtime_prefill["card_key"],
            "ui_asset_build": UI_ASSET_BUILD,
            "phase_notice": "预选阶段只用于浏览和整理课程；自动抢课仅用于复选、正选或补选阶段。",
            **backend_service.backend_payload(),
        },
        headers=get_no_cache_headers(),
    )


@app.post("/api/backend/select")
async def api_select_backend(req: BackendSelectRequest):
    """Select the login/read preference; enrollment writes stay on primary."""
    preference = update_backend_preference(req.backend)
    payload = backend_service.backend_payload()
    if preference == config.BACKEND_WEBVPN and not payload["webvpn_authenticated"]:
        return _api_error(
            409,
            "请先完成 WebVPN 统一认证",
            "WEBVPN_AUTH_REQUIRED",
            retryable=True,
            **payload,
        )
    return JSONResponse(content=payload, headers=get_no_cache_headers())


@app.post("/api/webvpn/auth/start")
async def api_start_webvpn_auth():
    """Start the local controlled-browser WebVPN authentication flow."""
    try:
        payload = await asyncio.to_thread(webvpn_auth_service.start_auth)
    except ControlledBrowserUnavailableError as exc:
        recovery_status = webvpn_auth_service.get_status()
        recovery_status.pop("message", None)
        return _api_error(
            503,
            str(exc),
            "WEBVPN_AUTH_START_FAILED",
            retryable=True,
            **recovery_status,
        )
    return JSONResponse(content=payload, headers=get_no_cache_headers())


@app.get("/api/webvpn/auth/status")
async def api_webvpn_auth_status():
    """Return the current state of the local WebVPN authentication browser."""
    return JSONResponse(
        content=webvpn_auth_service.get_status(),
        headers=get_no_cache_headers(),
    )


@app.post("/api/login", status_code=status.HTTP_200_OK)
async def api_login(user: LoginRequest):
    """Verify the local card key, then establish a school session."""
    try:
        update_backend_preference(user.backend)
        if user.backend == config.BACKEND_WEBVPN and not backend_service.has_webvpn_cookies():
            return _api_error(
                409,
                "请先完成 WebVPN 统一认证",
                "WEBVPN_AUTH_REQUIRED",
                retryable=True,
            )
        student_id = user.student_id.strip()
        err = validate_login_params(
            student_id,
            user.password,
            user.card_key,
            user.verify_code,
            user.vtoken,
            user.cookie,
        )
        if err:
            return JSONResponse(
                status_code=400,
                content={"message": err, "is_error": True},
            )

        if not verify_card_key(student_id, user.card_key):
            return JSONResponse(
                status_code=400,
                content={"message": LOGIN_ERROR_MSG, "is_error": True},
            )

        login_pwd = encrypt_password(user.password)
        centres_string = logic.serialize_captcha_coordinates(user.verify_code)
        semi_cookie = logic.parse_cookie(user.cookie)
        if not centres_string or not semi_cookie:
            return JSONResponse(
                status_code=400,
                content={"message": LOGIN_ERROR_MSG, "is_error": True},
            )

        logger.info("Starting school login for student ending in %s", student_id[-4:])
        login_result = await asyncio.to_thread(
            perform_school_login,
            student_id,
            user.vtoken,
            login_pwd,
            centres_string,
            semi_cookie,
        )
        if not login_result["success"]:
            logger.warning("School rejected login: %s", login_result.get("error_msg"))
            return JSONResponse(
                status_code=400,
                content={"message": LOGIN_ERROR_MSG, "is_error": True},
            )

        save_login_state(
            login_result["cookie"],
            semi_cookie,
            student_id,
            user.password,
            login_result["token"],
        )

        message = "登录成功"
        try:
            await asyncio.to_thread(refresh_elective_batch, student_id, config.token, True)
        except Exception as exc:
            logger.warning("Login succeeded but batch refresh failed: %s", exc)
            message = "登录成功，选课批次暂未读取到，请稍后刷新"
        return ApiMessage(message=message, is_error=False)
    except Exception as exc:
        logger.exception("Unexpected login failure: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"message": LOGIN_ERROR_MSG, "is_error": True},
        )


@app.get("/api/captcha")
async def api_captcha(backend: str | None = Query(default=None)):
    """Fetch one manual-login captcha and expose a finite, classified failure state."""
    try:
        if backend:
            update_backend_preference(backend)
        # Manual refresh is the retry boundary. One server attempt prevents stale
        # requests from accumulating after a browser timeout.
        result = await asyncio.to_thread(logic.fetch_vtoken_and_image, 1)
        return JSONResponse(content=result, headers=get_no_cache_headers())
    except logic.CaptchaUnavailableError:
        logger.info("School captcha is unavailable in the current window")
        return _api_error(
            409,
            "学校当前未提供登录验证码，可能尚未开放选课、正在切换阶段或处于维护时段。请稍后手动重试。",
            "CAPTCHA_UNAVAILABLE",
            retryable=True,
        )
    except backend_service.WebVPNAuthenticationRequiredError:
        return _api_error(
            409,
            "主站暂时无法访问，请先完成 WebVPN 统一认证后重试",
            "WEBVPN_AUTH_REQUIRED",
            retryable=True,
        )
    except requests.Timeout:
        logger.warning("School captcha request timed out")
        return _api_error(
            504,
            "连接学校验证码服务超时，本次加载已停止。请检查网络后手动重试。",
            "CAPTCHA_TIMEOUT",
            retryable=True,
        )
    except requests.RequestException as exc:
        logger.warning("School captcha network failure: %s", type(exc).__name__)
        return _api_error(
            503,
            "暂时无法连接学校验证码服务，本次加载已停止。请检查网络或稍后手动重试。",
            "CAPTCHA_NETWORK_ERROR",
            retryable=True,
        )
    except (logic.CaptchaResponseError, ValueError, RuntimeError) as exc:
        logger.warning("School captcha response rejected: %s", type(exc).__name__)
        return _api_error(
            502,
            "学校验证码响应异常，本次加载已停止。请稍后手动重试；持续失败时请确认学校系统是否开放。",
            "CAPTCHA_INVALID_RESPONSE",
            retryable=True,
        )
    except Exception as exc:
        logger.exception("Unexpected captcha failure: %s", exc)
        return _api_error(
            500,
            "验证码服务发生意外错误，本次加载已停止。请重新启动程序后再试。",
            "CAPTCHA_INTERNAL_ERROR",
            retryable=False,
        )


CAPTCHA_SOLVE_MAX_RETRIES = config.ocr_relogin_max_attempts


@app.post("/api/captcha/solve", status_code=status.HTTP_200_OK)
async def api_captcha_solve(payload: dict):
    """Run local OCR on the captcha image and return four click coordinates.

    If OCR fails on the provided image, the server automatically fetches fresh
    captchas and retries.  When a retry succeeds the new captcha data
    (``vtoken``, ``cookie``, ``imageUrl``) is returned so the frontend can
    update its state.
    """
    import base64

    image_url = str(payload.get("imageUrl", "")).strip()
    if not image_url.startswith("data:image/"):
        return JSONResponse(
            status_code=400,
            content={"message": "缺少验证码图片数据", "is_error": True},
        )

    current_vtoken = str(payload.get("vtoken", "")).strip()
    current_cookie = str(payload.get("cookie", "")).strip()
    current_image_url = image_url

    for attempt in range(1, CAPTCHA_SOLVE_MAX_RETRIES + 1):
        try:
            header, encoded = current_image_url.split(",", 1)
            image_bytes = base64.b64decode(encoded)
            image_path = logic._captcha_image_path()
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(image_bytes)

            centers = await asyncio.to_thread(logic.recognize_captcha_centers)
            if centers and len(centers) == 4:
                response = {"points": centers, "message": ""}
                if attempt > 1:
                    response["captcha"] = {
                        "vtoken": current_vtoken,
                        "cookie": current_cookie,
                        "imageUrl": current_image_url,
                    }
                return JSONResponse(content=response)

            logger.info(
                "Captcha solve attempt %s/%s: OCR returned %s points",
                attempt,
                CAPTCHA_SOLVE_MAX_RETRIES,
                len(centers) if centers else 0,
            )
        except (ImportError, ModuleNotFoundError) as exc:
            logger.warning("OCR dependency unavailable: %s", exc)
            return JSONResponse(
                status_code=500,
                content={"points": [], "message": f"OCR 依赖不可用: {exc}"},
            )
        except Exception as exc:
            logger.warning(
                "Captcha solve attempt %s/%s failed: %s", attempt, CAPTCHA_SOLVE_MAX_RETRIES, exc
            )

        # Fetch a fresh captcha for the next retry
        if attempt < CAPTCHA_SOLVE_MAX_RETRIES:
            try:
                fresh = await asyncio.to_thread(logic.fetch_vtoken_and_image, 1)
                current_vtoken = fresh["vtoken"]
                current_cookie = fresh["cookie"]
                current_image_url = fresh["imageUrl"]
            except Exception as exc:
                logger.warning("Failed to fetch fresh captcha for retry: %s", exc)
                break

    return JSONResponse(
        status_code=200,
        content={
            "points": [],
            "message": "OCR 多次尝试未能识别，请手动点击或刷新验证码重试",
            "captcha": {
                "vtoken": current_vtoken,
                "cookie": current_cookie,
                "imageUrl": current_image_url,
            },
        },
    )


@app.get("/api/session")
async def api_session():
    return JSONResponse(
        content=_session_payload(),
        headers=get_no_cache_headers(),
    )


@app.post("/api/session/recover")
async def api_session_recover(request: Request):
    """Start OCR recovery using credentials retained only in process memory."""
    if (await request.body()).strip():
        return _api_error(
            400,
            "自动重登录只使用当前进程内存中的登录凭据",
            "SESSION_RECOVERY_BODY_NOT_ALLOWED",
            retryable=False,
        )
    started, message = start_automatic_relogin()
    if not started:
        snapshot = get_session_snapshot()
        failure_count = int(snapshot.get("relogin_failure_count", 0))
        max_retries = int(snapshot.get("relogin_max_retries", config.relogin_max_retries))
        exhausted = failure_count >= max(1, max_retries)
        return _api_error(
            401 if exhausted else 409,
            message,
            "SESSION_RECOVERY_EXHAUSTED" if exhausted else "SESSION_RECOVERY_UNAVAILABLE",
            retryable=not exhausted,
            requires_manual_login=exhausted,
        )
    payload = _session_payload()
    payload["message"] = message
    return JSONResponse(content=payload, headers=get_no_cache_headers())


@app.post("/api/session/refresh")
async def api_session_refresh():
    """Refresh the school-provided batch without touching enrollment state."""
    snapshot = get_session_snapshot()
    if not snapshot["logged_in"] or not snapshot["student_id"]:
        return _not_logged_in_response()

    try:
        await asyncio.to_thread(
            refresh_elective_batch,
            str(snapshot["student_id"]),
            config.token,
        )
    except logic.SchoolBatchSessionExpiredError:
        logger.info("Batch refresh detected expiry; starting OCR recovery")
        recovered, error = await asyncio.to_thread(
            attempt_automatic_relogin,
            config.ocr_relogin_max_attempts,
        )
        if not recovered:
            logger.warning("OCR session recovery failed during batch refresh: %s", error)
            return _api_error(
                401,
                "登录已过期且 OCR 自动重登录失败，请手动登录",
                "SESSION_RECOVERY_FAILED",
                retryable=False,
                requires_manual_login=True,
            )
    except backend_service.WebVPNAuthenticationRequiredError:
        return _api_error(
            409,
            "主站暂时无法访问，请先完成 WebVPN 统一认证后重试",
            "WEBVPN_AUTH_REQUIRED",
            retryable=True,
        )
    except logic.ElectiveBatchUnavailableError as exc:
        clear_elective_batch()
        logger.info("School currently exposes no elective batch: %s", exc)
        return _api_error(
            409,
            "登录仍然有效，但学校当前未开放可用的选课批次。你的本地清单已保留，可稍后重新检查。",
            "BATCH_UNAVAILABLE",
            retryable=True,
            session=_session_payload(),
        )
    except requests.Timeout:
        logger.warning("Batch refresh timed out")
        return _api_error(
            504,
            "检查开放状态超时，登录状态和本地清单不受影响，请稍后重试",
            "SCHOOL_TIMEOUT",
            retryable=True,
        )
    except requests.RequestException as exc:
        logger.warning("Batch refresh network failure: %s", exc)
        return _api_error(
            503,
            "暂时无法连接学校选课系统，登录状态和本地清单不受影响",
            "SCHOOL_NETWORK_ERROR",
            retryable=True,
        )
    except Exception as exc:
        logger.exception("Batch refresh failed: %s", exc)
        return _api_error(
            502,
            "学校返回的选课批次信息暂时无法识别，请稍后重新检查",
            "SCHOOL_BATCH_INVALID",
            retryable=True,
        )

    return JSONResponse(
        content={**_session_payload(), "message": "开放状态已更新"},
        headers=get_no_cache_headers(),
    )


@app.post("/api/logout")
async def api_logout():
    if is_enroll_task_running():
        return JSONResponse(
            status_code=409,
            content={"message": "抢课任务运行中，暂不能清除登录态", "is_error": True},
        )
    clear_login_state()
    return JSONResponse(
        content=ApiMessage(message="已清除本地登录态", is_error=False).model_dump(),
        headers=get_no_cache_headers(),
    )


@app.post("/api/session/campus")
async def api_switch_campus(request: CampusSwitchRequest):
    """Select the campus used by subsequent catalog requests."""
    if not config.token or not config.combined_cookie:
        return _not_logged_in_response()
    if get_campus(request.campus_code) is None:
        return _api_error(
            400,
            "不支持的校区，请刷新页面后重试",
            "INVALID_CAMPUS",
            retryable=False,
        )
    try:
        await asyncio.to_thread(set_current_campus, request.campus_code)
    except RuntimeError:
        return _not_logged_in_response()
    logger.info("Course catalog campus changed to %s", campus_name(request.campus_code))
    return JSONResponse(
        content={**_session_payload(), "message": f"已切换到{campus_name(request.campus_code)}"},
        headers=get_no_cache_headers(),
    )


_full_catalog_refreshing: set[str] = set()
_full_catalog_refresh_lock = threading.Lock()


def _catalog_context() -> tuple[CatalogCacheScope, CatalogRequestContext]:
    """Capture every cache/request scope field before background work starts."""
    snapshot = get_session_snapshot()
    scope = scope_from_snapshot(snapshot)
    context = CatalogRequestContext(
        student_id=str(snapshot["student_id"]),
        campus_code=str(snapshot["campus_code"]),
        batch_code=str(snapshot["batch_code"]),
        token=str(config.token),
        cookie=str(config.combined_cookie),
        backend_preference=backend_service.get_preference(),
    )
    return scope, context


def _catalog_context_is_current(scope: CatalogCacheScope, context: CatalogRequestContext) -> bool:
    """Reject a cache write if login, batch, campus, token, or cookie changed."""
    try:
        current_scope = scope_from_snapshot(get_session_snapshot())
    except ValueError:
        return False
    return (
        current_scope == scope
        and str(config.token) == context.token
        and str(config.combined_cookie) == context.cookie
    )


def _cached_catalog_for_request(
    snapshot: dict,
    course_type: str,
    page: int,
    page_size: int,
) -> dict | None:
    """Resolve cache data without contacting school or crossing account scopes."""
    try:
        scope = scope_from_snapshot(snapshot)
    except ValueError:
        scope = None
    if scope is not None:
        cached = get_full_course_cache(scope, course_type)
        if cached is None:
            cached = get_course_cache(scope, course_type, page, page_size)
        return cached

    student_id = str(snapshot.get("student_id") or _runtime_prefill["student_id"] or "").strip()
    if not student_id:
        return None
    cached = get_latest_full_for_student(student_id, course_type)
    if cached is None:
        cached = get_latest_page_for_student(student_id, course_type, page, page_size)
    return cached


def _cache_full_catalog(
    scope: CatalogCacheScope,
    context: CatalogRequestContext,
    course_type: str,
    requested_page: int,
    first_payload: dict,
) -> None:
    """Fetch every page under one immutable account/batch/campus snapshot."""
    normalized_type = course_type.strip().upper()
    if normalized_type not in {"TJKC", "FANKC"}:
        return
    try:
        total_count = int(first_payload.get("total_count", 0))
    except (TypeError, ValueError):
        return
    if total_count <= 0 or not first_payload.get("courses"):
        return

    total_pages = max(1, (total_count + 9) // 10)
    all_courses: list[dict] = []
    for page in range(1, total_pages + 1):
        if not _catalog_context_is_current(scope, context):
            logger.info("Full catalog refresh cancelled because the session scope changed")
            return
        if page == requested_page:
            content = first_payload
        else:
            success, data, _ = query_courses(normalized_type, page - 1, context)
            if not success:
                logger.info(
                    "Full %s catalog cache refresh stopped on page %s", normalized_type, page
                )
                return
            content = data.to_api_dict() if hasattr(data, "to_api_dict") else data
        if not isinstance(content, dict) or not isinstance(content.get("courses"), list):
            return
        if not content["courses"]:
            return
        if not _catalog_context_is_current(scope, context):
            logger.info("Full catalog refresh discarded a page after the session scope changed")
            return
        put_course_cache(scope, normalized_type, page, 10, content)
        all_courses.extend(content["courses"])

    if len(all_courses) < total_count:
        logger.info("Full %s catalog cache refresh was incomplete", normalized_type)
        return
    if not _catalog_context_is_current(scope, context):
        return
    put_full_course_cache(
        scope,
        normalized_type,
        {
            "total_count": total_count,
            "courses": all_courses[:total_count],
            "msg": str(first_payload.get("msg") or ""),
            "is_error": False,
        },
    )


async def _run_full_catalog_cache_refresh(
    scope: CatalogCacheScope,
    context: CatalogRequestContext,
    course_type: str,
    requested_page: int,
    first_payload: dict,
) -> None:
    try:
        await asyncio.to_thread(
            _cache_full_catalog,
            scope,
            context,
            course_type,
            requested_page,
            first_payload,
        )
    except Exception:
        logger.exception("Full %s catalog cache refresh failed", course_type)
    finally:
        with _full_catalog_refresh_lock:
            _full_catalog_refreshing.discard(f"{scope.digest}:{course_type}")


def _schedule_full_catalog_cache_refresh(
    scope: CatalogCacheScope,
    context: CatalogRequestContext,
    course_type: str,
    requested_page: int,
    first_payload: dict,
) -> None:
    if course_type not in {"TJKC", "FANKC"}:
        return
    refresh_key = f"{scope.digest}:{course_type}"
    with _full_catalog_refresh_lock:
        if refresh_key in _full_catalog_refreshing:
            return
        _full_catalog_refreshing.add(refresh_key)
    asyncio.create_task(
        _run_full_catalog_cache_refresh(
            scope,
            context,
            course_type,
            requested_page,
            first_payload,
        )
    )


@app.get("/api/school/courses")
async def api_school_courses(
    course_type: str = Query(
        default="TJKC",
        alias="type",
        min_length=1,
        max_length=16,
    ),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=10, ge=1, le=100),
    cache_mode: bool = Query(default=False),
):
    if page_size != 10:
        return _api_error(
            400,
            "学校接口固定每页 10 门课程",
            "INVALID_PAGE_SIZE",
            retryable=False,
        )
    normalized_type = course_type.strip().upper()
    if not is_supported_type(normalized_type):
        return _api_error(
            400,
            get_unsupported_message(normalized_type),
            "UNSUPPORTED_COURSE_TYPE",
            retryable=False,
        )

    snapshot = get_session_snapshot()
    if cache_mode:
        cached = _cached_catalog_for_request(snapshot, normalized_type, page, page_size)
        if cached is not None:
            return JSONResponse(content=cached, headers=get_no_cache_headers())
        return _api_error(
            404,
            "当前终端学号、批次和校区下暂无可用的本地课程缓存",
            "COURSE_CACHE_MISS",
            retryable=False,
        )

    if not config.token or not config.combined_cookie:
        return _not_logged_in_response()

    phase = config.classify_elective_phase(str(snapshot["batch_name"]))
    if phase == config.PHASE_CLOSED:
        return _api_error(
            409,
            "你已登录，但当前不在开放选课时间，学校课程目录暂不可用。可稍后重新检查开放状态。",
            "COURSE_WINDOW_CLOSED",
            retryable=True,
            phase=phase,
        )
    if not snapshot["batch_code"]:
        return _api_error(
            409,
            "你已登录，但暂未读取到有效选课批次。请先重新检查开放状态。",
            "BATCH_UNAVAILABLE",
            retryable=True,
            phase=phase,
        )
    try:
        cache_scope, request_context = _catalog_context()
    except ValueError:
        return _api_error(
            409,
            "当前账号、批次或校区信息不完整，请重新检查开放状态",
            "CACHE_SCOPE_UNAVAILABLE",
            retryable=True,
        )
    # WebUI 对用户使用 1-based 页码；学校接口的 pageNumber 从 0 开始。
    school_page = page - 1
    try:
        success, data, _ = await asyncio.to_thread(
            query_courses,
            normalized_type,
            school_page,
            request_context,
        )
        if not success and data == SESSION_EXPIRED:
            logger.info("Course query detected expiry; starting OCR recovery")
            recovered, error = await asyncio.to_thread(
                attempt_automatic_relogin, config.ocr_relogin_max_attempts
            )
            if not recovered:
                logger.warning("OCR session recovery failed: %s", error)
                return _api_error(
                    401,
                    "登录已过期且 OCR 自动重登录失败，请手动登录",
                    "SESSION_RECOVERY_FAILED",
                    retryable=False,
                    requires_manual_login=True,
                )
            try:
                cache_scope, request_context = _catalog_context()
            except ValueError:
                return _not_logged_in_response()
            success, data, _ = await asyncio.to_thread(
                query_courses,
                normalized_type,
                school_page,
                request_context,
            )
        if success:
            content = data.to_api_dict() if hasattr(data, "to_api_dict") else data
            if isinstance(content, dict):
                if _catalog_context_is_current(cache_scope, request_context):
                    put_course_cache(
                        cache_scope,
                        normalized_type,
                        page,
                        page_size,
                        content,
                    )
                    if content.get("courses"):
                        _schedule_full_catalog_cache_refresh(
                            cache_scope,
                            request_context,
                            normalized_type,
                            page,
                            content,
                        )
                cached_page = get_course_cache(
                    cache_scope,
                    normalized_type,
                    page,
                    page_size,
                )
                content = {
                    **content,
                    "cached": False,
                    "has_cache": cached_page is not None,
                    "cached_at": cached_page.get("cached_at") if cached_page else None,
                    "cache_schema_version": (
                        cached_page.get("cache_schema_version") if cached_page else None
                    ),
                }
            return JSONResponse(content=content, headers=get_no_cache_headers())
        failure_map = {
            COURSE_WINDOW_CLOSED: (
                409,
                "学校提示当前不在开放选课时间，课程目录暂不可用。请重新检查开放状态。",
                "COURSE_WINDOW_CLOSED",
            ),
            COURSE_QUERY_REJECTED: (
                502,
                "学校暂时拒绝了课程目录请求，请稍后刷新；本地清单不受影响。",
                "SCHOOL_COURSE_REJECTED",
            ),
            COURSE_RESPONSE_INVALID: (
                502,
                "学校课程接口返回了无法识别的数据，请稍后刷新。",
                "SCHOOL_RESPONSE_INVALID",
            ),
        }
        response_status, message, error_code = failure_map.get(
            data,
            (502, "获取课程列表失败，请稍后刷新", "SCHOOL_COURSE_ERROR"),
        )
        return _api_error(
            response_status,
            message,
            error_code,
            retryable=True,
        )
    except backend_service.WebVPNAuthenticationRequiredError:
        return _api_error(
            409,
            "主站暂时无法访问，请先完成 WebVPN 统一认证后重试",
            "WEBVPN_AUTH_REQUIRED",
            retryable=True,
        )
    except requests.Timeout:
        logger.warning("Course-list endpoint timed out")
        return _api_error(
            504,
            "学校课程接口响应超时，请稍后刷新；本地清单不受影响。",
            "SCHOOL_TIMEOUT",
            retryable=True,
        )
    except requests.RequestException as exc:
        logger.warning("Course-list endpoint network failure: %s", exc)
        return _api_error(
            503,
            "暂时无法连接学校选课系统，请检查网络后刷新；本地清单不受影响。",
            "SCHOOL_NETWORK_ERROR",
            retryable=True,
        )
    except Exception as exc:
        logger.exception("Course-list endpoint failed: %s", exc)
        return _api_error(
            502,
            "获取课程列表时出现未预期错误，请稍后刷新；本地清单不受影响。",
            "SCHOOL_COURSE_ERROR",
            retryable=True,
        )


@app.get("/api/school/enrolled")
async def api_school_enrolled():
    """Return courses already selected by the current student."""
    if not config.token or not config.combined_cookie:
        return _not_logged_in_response()
    try:
        success, data = await asyncio.to_thread(get_enrolled_courses)
        if not success and data == SESSION_EXPIRED:
            logger.info("Selected-course query detected expiry; starting OCR recovery")
            recovered, error = await asyncio.to_thread(
                attempt_automatic_relogin,
                config.ocr_relogin_max_attempts,
            )
            if not recovered:
                logger.warning("OCR session recovery failed: %s", error)
                return _api_error(
                    401,
                    "登录已过期且 OCR 自动重登录失败，请手动登录",
                    "SESSION_RECOVERY_FAILED",
                    retryable=False,
                    requires_manual_login=True,
                )
            success, data = await asyncio.to_thread(get_enrolled_courses)
        if success:
            return JSONResponse(
                content={
                    "courses": data,
                    "total_count": len(data),
                    "timetable": build_timetable(data),
                },
                headers=get_no_cache_headers(),
            )
        return _api_error(
            502,
            str(data) or "获取已选课程失败，请稍后刷新",
            "SCHOOL_ENROLLED_ERROR",
            retryable=True,
        )
    except backend_service.WebVPNAuthenticationRequiredError:
        return _api_error(
            409,
            "主站暂时无法访问，请先完成 WebVPN 统一认证后重试",
            "WEBVPN_AUTH_REQUIRED",
            retryable=True,
        )
    except requests.Timeout:
        logger.warning("Selected-course endpoint timed out")
        return _api_error(
            504,
            "学校已选课程接口响应超时，请稍后刷新",
            "SCHOOL_TIMEOUT",
            retryable=True,
        )
    except requests.RequestException as exc:
        logger.warning("Selected-course endpoint network failure: %s", exc)
        return _api_error(
            503,
            "暂时无法连接学校选课系统，请检查网络后刷新",
            "SCHOOL_NETWORK_ERROR",
            retryable=True,
        )
    except Exception as exc:
        logger.exception("Selected-course endpoint failed: %s", exc)
        return _api_error(
            502,
            "获取已选课程失败，请稍后刷新",
            "SCHOOL_ENROLLED_ERROR",
            retryable=True,
        )


@app.post("/api/courses/add")
async def api_cart_add(cart: CartCourse):
    result = add_cart_course(cart)
    error_code = str(result.get("error_code", ""))
    if not result["success"] and error_code.startswith("ENROLL_TASK_"):
        return _api_error(
            409,
            str(result["message"]),
            error_code,
            retryable=error_code == "ENROLL_TASK_PAUSE_PENDING",
        )
    if not result["success"] and error_code == "COURSE_ALREADY_IN_CART":
        return _api_error(
            409,
            str(result["message"]),
            error_code,
            retryable=False,
        )
    return JSONResponse(
        content={
            **result,
            "is_error": not bool(result["success"]),
            "progress": get_enroll_progress(),
        },
        headers=get_no_cache_headers(),
    )


@app.post("/api/courses/delete")
async def api_cart_delete(
    course_id: str = Query(alias="id", min_length=1, max_length=128),
):
    result = remove_cart_course(course_id)
    if not result["success"] and str(result.get("error_code", "")).startswith("ENROLL_TASK_"):
        return _api_error(
            409,
            str(result["message"]),
            str(result["error_code"]),
            retryable=result.get("error_code") == "ENROLL_TASK_PAUSE_PENDING",
        )
    if not result["success"]:
        return ApiMessage(message=str(result["message"]), is_error=True)
    return JSONResponse(
        content={
            "message": result["message"],
            "is_error": False,
            "task_stopping": bool(result.get("task_stopping")),
            "progress": get_enroll_progress(),
        },
        headers=get_no_cache_headers(),
    )


@app.post("/api/courses/retry")
async def api_cart_retry(
    course_id: str = Query(alias="id", min_length=1, max_length=128),
):
    """Return an explicitly failed item to PENDING without duplicating it."""
    result = retry_cart_course(course_id)
    error_code = str(result.get("error_code", ""))
    if not result["success"] and error_code.startswith("ENROLL_TASK_"):
        return _api_error(
            409,
            str(result["message"]),
            error_code,
            retryable=error_code == "ENROLL_TASK_PAUSE_PENDING",
        )
    return JSONResponse(
        content={**result, "is_error": not bool(result["success"])},
        headers=get_no_cache_headers(),
    )


@app.get("/api/courses/dblist")
@app.post("/api/courses/dblist", include_in_schema=False)
async def api_cart_list(
    status: str = Query(default="", max_length=16),
) -> list[CartCourse]:
    return [_cart_from_row(row) for row in cart_service.get_courses_by_status(status)]


@app.get("/api/courses/sorted")
async def api_cart_sorted() -> list[CartCourse]:
    return [_cart_from_row(row) for row in cart_service.get_all_sorted()]


@app.patch("/api/courses/{course_id}")
async def api_cart_preferences(course_id: str, payload: CartPreferencesRequest):
    values = payload.model_dump(exclude_unset=True)
    if not values or any(value is None for value in values.values()):
        return _api_error(
            400, "课程偏好无效或课程不存在", "INVALID_COURSE_PREFERENCE", retryable=False
        )
    result = update_cart_course_preferences(course_id, **values)
    error_code = str(result.get("error_code", ""))
    if not result["success"] and error_code.startswith("ENROLL_TASK_"):
        return _api_error(
            409,
            str(result["message"]),
            error_code,
            retryable=error_code == "ENROLL_TASK_PAUSE_PENDING",
        )
    if not result["success"]:
        return _api_error(
            400,
            str(result["message"]),
            error_code or "INVALID_COURSE_PREFERENCE",
            retryable=False,
        )
    return JSONResponse(content=result, headers=get_no_cache_headers())


@app.patch("/api/courses/priority/order")
async def api_cart_priorities(payload: dict):
    raw_updates = payload.get("updates") if isinstance(payload, dict) else None
    if not isinstance(raw_updates, list) or not (1 <= len(raw_updates) <= 100):
        return _api_error(
            400,
            "课程优先级列表无效",
            "INVALID_COURSE_PRIORITIES",
            retryable=False,
        )
    updates: list[tuple[str, int]] = []
    try:
        for item in raw_updates:
            if not isinstance(item, dict):
                raise ValueError
            course_id = str(item.get("id") or "").strip()
            rank = int(item.get("priority_rank"))
            if not course_id or len(course_id) > 128 or rank < 0 or rank > 100000:
                raise ValueError
            updates.append((course_id, rank))
    except (TypeError, ValueError):
        return _api_error(
            400,
            "课程优先级列表无效",
            "INVALID_COURSE_PRIORITIES",
            retryable=False,
        )
    result = update_cart_course_priorities(updates)
    error_code = str(result.get("error_code", ""))
    if not result["success"] and error_code.startswith("ENROLL_TASK_"):
        return _api_error(
            409,
            str(result["message"]),
            error_code,
            retryable=error_code == "ENROLL_TASK_PAUSE_PENDING",
        )
    if not result["success"]:
        return _api_error(
            400,
            str(result["message"]),
            error_code or "INVALID_COURSE_PRIORITIES",
            retryable=False,
        )
    return JSONResponse(content=result, headers=get_no_cache_headers())


@app.post("/api/enroll/courses")
async def api_start_enroll(
    request: EnrollmentStartRequest,
):
    """Start one guarded background worker without changing school request fields."""
    snapshot = get_session_snapshot()
    if not snapshot["logged_in"] or not snapshot["student_id"]:
        return _not_logged_in_response()

    try:
        await asyncio.to_thread(
            refresh_elective_batch,
            str(snapshot["student_id"]),
            config.token,
        )
    except logic.SchoolBatchSessionExpiredError:
        logger.info("Enrollment start detected expiry; starting OCR recovery")
        recovered, error = await asyncio.to_thread(
            attempt_automatic_relogin,
            config.ocr_relogin_max_attempts,
        )
        if not recovered:
            logger.warning("OCR session recovery failed before task start: %s", error)
            return _automatic_relogin_response(error)
    except Exception as exc:
        logger.warning("Could not verify enrollment phase before task start: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "message": "无法向学校确认当前选课批次，未启动抢课，请刷新后重试",
                "is_error": True,
            },
        )

    snapshot = get_session_snapshot()
    batch_name = str(snapshot["batch_name"]).strip()
    if not snapshot["batch_code"]:
        return JSONResponse(
            status_code=503,
            content={
                "message": "学校未返回有效选课批次，未启动抢课",
                "is_error": True,
            },
        )
    block_reason = config.automatic_enroll_block_reason(batch_name)
    if block_reason:
        return JSONResponse(
            status_code=409,
            content={
                "message": block_reason,
                "is_error": True,
            },
        )
    if not request.confirmed_phase:
        return JSONResponse(
            status_code=400,
            content={"message": "请确认当前为复选、正选或补选阶段", "is_error": True},
        )
    if not any(row.get("auto_enabled", 1) for row in cart_service.get_courses_by_status("PENDING")):
        return JSONResponse(
            status_code=400,
            content={
                "message": "购物车中没有启用自动抢课的待抢课程，请先在清单中开启“自动抢课”",
                "is_error": True,
                "error_code": "NO_ENABLED_PENDING_COURSE",
                "retryable": False,
            },
        )
    try:
        worker_started = start_enroll_worker()
    except Exception as exc:
        logger.exception("Could not start enrollment worker: %s", exc)
        return _api_error(
            500,
            "后台抢课任务启动失败，清单已保留，请稍后重试",
            "ENROLL_WORKER_START_FAILED",
            retryable=True,
        )
    if not worker_started:
        return JSONResponse(
            status_code=409,
            content={"message": "已有抢课任务正在运行", "is_error": True},
        )

    return ApiMessage(message="抢课任务已在后台启动", is_error=False)


@app.get("/api/enroll/status")
async def api_enroll_status():
    return JSONResponse(
        content=get_enroll_progress(),
        headers=get_no_cache_headers(),
    )


@app.get("/api/enroll/settings")
async def api_enroll_settings():
    return get_enroll_settings()


@app.patch("/api/enroll/settings")
async def api_update_enroll_settings(request: EnrollmentSettingsRequest):
    values = request.model_dump(exclude_unset=True)
    mode = values.pop("mode", None)
    for key in ("boost_interval_ms", "normal_interval_ms", "scan_interval_ms"):
        if values.get(key) is None:
            values.pop(key, None)
    try:
        settings = update_enroll_settings(**values)
        if mode:
            set_enroll_mode(mode)
        return {**settings, "mode": mode or get_enroll_settings()["mode"]}
    except (TypeError, ValueError):
        return _api_error(400, "抢课设置无效", "INVALID_ENROLL_SETTINGS", retryable=False)


@app.post("/api/enroll/mode")
async def api_enroll_mode(request: EnrollmentModeRequest):
    if not set_enroll_mode(request.mode):
        return _api_error(400, "抢课模式无效", "INVALID_ENROLL_MODE", retryable=False)
    settings = get_enroll_settings()
    return {
        "success": True,
        "mode": request.mode,
        "settings": settings,
        "message": "抢课模式已切换",
    }


@app.post("/api/enroll/pause")
async def api_pause_enroll():
    success, message = pause_enroll_task()
    if not success:
        task_state = get_enroll_task_state()
        error_code = "ENROLL_TASK_STOPPING" if task_state["stopping"] else "ENROLL_TASK_NOT_RUNNING"
        return _api_error(
            409,
            message,
            error_code,
            retryable=False,
        )
    progress = get_enroll_progress()
    if progress["paused"] and not progress["pause_acknowledged"]:
        message = "暂停请求已提交，正在等待当前学校请求结束"
    return JSONResponse(
        content={"message": message, "is_error": False, "progress": progress},
        headers=get_no_cache_headers(),
    )


@app.post("/api/enroll/resume")
async def api_resume_enroll():
    task_state = get_enroll_task_state()
    if not task_state["running"]:
        return _api_error(
            409,
            "当前没有可继续的抢课任务",
            "ENROLL_TASK_NOT_RUNNING",
            retryable=False,
        )
    if task_state["stopping"]:
        return _api_error(
            409,
            task_state["stopping_reason"] or "抢课任务正在结束，请稍候",
            "ENROLL_TASK_STOPPING",
            retryable=True,
        )

    snapshot = get_session_snapshot()
    if snapshot["relogin_in_progress"]:
        return _api_error(
            409,
            "正在自动重新登录，请等待恢复完成后再继续任务",
            "SESSION_RECOVERY_IN_PROGRESS",
            retryable=True,
        )
    if not snapshot["logged_in"]:
        return _api_error(
            409,
            "学校登录尚未恢复，请先完成手动登录，再返回清单继续任务",
            "LOGIN_REQUIRED_FOR_RESUME",
            retryable=False,
            requires_manual_login=True,
        )

    try:
        await asyncio.to_thread(
            refresh_elective_batch,
            str(snapshot["student_id"]),
            config.token,
        )
    except logic.SchoolBatchSessionExpiredError:
        recovered, error = await asyncio.to_thread(
            attempt_automatic_relogin,
            config.ocr_relogin_max_attempts,
        )
        if not recovered:
            logger.warning("OCR session recovery failed before task resume: %s", error)
            return _api_error(
                409,
                "学校登录已过期且自动恢复失败，任务仍保持暂停；请先手动登录",
                "LOGIN_REQUIRED_FOR_RESUME",
                retryable=False,
                requires_manual_login=True,
            )
    except logic.ElectiveBatchUnavailableError:
        clear_elective_batch()
        return _api_error(
            409,
            "学校当前未返回可用选课批次，任务仍保持暂停",
            "BATCH_UNAVAILABLE",
            retryable=True,
        )
    except requests.Timeout:
        return _api_error(
            504,
            "确认当前选课批次超时，任务仍保持暂停，请稍后再试",
            "SCHOOL_TIMEOUT",
            retryable=True,
        )
    except requests.RequestException:
        return _api_error(
            503,
            "暂时无法连接学校系统确认选课批次，任务仍保持暂停",
            "SCHOOL_NETWORK_ERROR",
            retryable=True,
        )
    except Exception as exc:
        logger.exception("Could not verify enrollment phase before task resume: %s", exc)
        return _api_error(
            503,
            "无法确认当前选课批次，任务仍保持暂停，请稍后再试",
            "PHASE_CHECK_FAILED",
            retryable=True,
        )

    refreshed = get_session_snapshot()
    if not refreshed["batch_code"]:
        return _api_error(
            409,
            "学校未返回有效选课批次，任务仍保持暂停",
            "BATCH_UNAVAILABLE",
            retryable=True,
        )
    block_reason = config.automatic_enroll_block_reason(str(refreshed["batch_name"]))
    if block_reason:
        return _api_error(
            409,
            f"{block_reason}；原任务仍保持暂停",
            "PHASE_NOT_ALLOWED",
            retryable=True,
        )

    success, message = resume_enroll_task()
    if not success:
        return _api_error(
            409,
            message,
            "ENROLL_TASK_NOT_RUNNING",
            retryable=False,
        )
    return JSONResponse(
        content={"message": message, "is_error": False, "progress": get_enroll_progress()},
        headers=get_no_cache_headers(),
    )


@app.post("/api/enroll/stop")
async def api_stop_enroll():
    """Request the running enrollment worker to stop gracefully.

    Returns 404 when no task is running (nothing to stop), 202 when the stop
    flag was set.  The worker observes the flag at the next checkpoint and
    exits through its normal ``finally`` block.
    """
    if not is_enroll_task_running():
        return JSONResponse(
            status_code=404,
            content={"message": "当前没有正在运行的抢课任务", "is_error": True},
        )
    if not stop_enroll_task():
        return JSONResponse(
            status_code=409,
            content={"message": "抢课任务状态已变化，请重试", "is_error": True},
        )
    return JSONResponse(
        status_code=202,
        content={"message": "已请求停止抢课任务，等待当前轮次结束", "is_error": False},
    )


@app.get("/api/health")
async def api_health():
    return {
        "status": "healthy",
        "timestamp": datetime.now().astimezone().isoformat(),
        "version": app.version,
    }


def _safe_static_file(relative_path: str) -> Path | None:
    try:
        candidate = (static_dir / relative_path).resolve()
        candidate.relative_to(static_dir)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


@app.post("/api/school/open")
async def api_open_official_school_page():
    """Open the public school entry without exposing local session secrets."""
    try:
        opened = await asyncio.to_thread(open_external_url, OFFICIAL_SCHOOL_HOME_URL)
    except Exception as exc:
        logger.warning("Unable to open official school page: %s", exc)
        opened = False
    if not opened:
        return _api_error(
            503,
            "外部打开命令未能执行，请手动访问深圳大学本科选课系统",
            "BROWSER_OPEN_FAILED",
            retryable=True,
            url=OFFICIAL_SCHOOL_HOME_URL,
        )
    return JSONResponse(
        content={
            "message": "已请求系统浏览器打开学校官方选课页面",
            "is_error": False,
            "url": OFFICIAL_SCHOOL_HOME_URL,
        },
        headers=get_no_cache_headers(),
    )


@app.api_route(
    "/proxy/{school_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def disabled_school_proxy(school_path: str):
    """Make the rejected same-origin proxy design explicitly unreachable."""
    return _api_error(
        410,
        "学校同源代理已停用；请使用只读 API 或在系统浏览器打开学校官网",
        "SCHOOL_PROXY_DISABLED",
        retryable=False,
    )


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    no_cache = get_no_cache_headers()
    candidates = []
    if full_path:
        candidates.extend((full_path, f"{full_path}.html", f"{full_path}/index.html"))
    else:
        candidates.append("index.html")

    for relative_path in candidates:
        target = _safe_static_file(relative_path)
        if target:
            return FileResponse(target, headers=no_cache)

    root = _safe_static_file("index.html")
    if root:
        return FileResponse(root, headers=no_cache)
    raise HTTPException(status_code=404, detail="前端文件未找到")


def _external_opener_commands(url: str) -> list[list[str]]:
    """Return Linux opener argv candidates in fallback order (no shell)."""
    return [["xdg-open", url], ["gio", "open", url]]


def open_external_url(url: str) -> bool:
    """Open a public URL, isolating Linux children from bundled libraries.

    Windows and macOS keep the stdlib browser lookup. On Linux the opener is
    executed as an argv list — never a shell — with the sanitized environment
    from :func:`project_paths.external_process_env`, so the bundled OpenSSL
    copies in the release folder cannot shadow system libraries for
    ``xdg-open``/``gio``/``kde-open``. A zero exit code only means the opener
    command was created successfully; callers must still surface the URL.
    """
    if sys.platform in {"win32", "darwin"}:
        try:
            return bool(webbrowser.open_new_tab(url))
        except Exception as exc:  # pragma: no cover - platform browser quirks
            logger.warning("Unable to open %s via system browser: %s", url, exc)
            return False
    for command in _external_opener_commands(url):
        executable = shutil.which(command[0])
        if not executable:
            continue
        try:
            completed = subprocess.run(
                [executable, *command[1:]],
                env=external_process_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("External opener %s failed for %s: %s", command[0], url, exc)
            continue
        if completed.returncode == 0:
            return True
        logger.warning(
            "External opener %s exited with %s for %s", command[0], completed.returncode, url
        )
    return False


def open_browser() -> None:
    if not open_external_url(get_login_url()):
        logger.warning("Unable to auto-open %s; please visit it manually", get_login_url())


KEEP_ALIVE_INTERVAL_SECONDS = 60


def _keep_alive_once() -> None:
    """Refresh the batch through the single session coordinator."""
    snapshot = get_session_snapshot()
    if not snapshot["logged_in"] or not snapshot["student_id"]:
        if automatic_relogin_available():
            logger.info("Keep-alive: no active school session; starting OCR recovery")
            recovered, error = attempt_automatic_relogin(config.ocr_relogin_max_attempts)
            if not recovered:
                logger.warning("Keep-alive OCR recovery failed: %s", error)
        return
    student_id = str(snapshot["student_id"])
    token = str(config.token)
    try:
        refresh_elective_batch(student_id, token)
        logger.info("Keep-alive: school session refreshed")
    except logic.SchoolBatchSessionExpiredError:
        logger.info("Keep-alive: session expired; starting OCR recovery")
        recovered, error = attempt_automatic_relogin(config.ocr_relogin_max_attempts)
        if not recovered:
            logger.warning("Keep-alive OCR recovery failed: %s", error)
    except logic.ElectiveBatchUnavailableError as exc:
        logger.info("Keep-alive: school session alive but no batch: %s", exc)
    except (requests.Timeout, requests.RequestException) as exc:
        logger.info("Keep-alive network issue (session unaffected): %s", exc)
    except Exception as exc:
        logger.warning("Keep-alive unexpected failure: %s", exc)


def start_server() -> None:
    import uvicorn

    configure_logging()
    # The reload supervisor owns the listening socket before child imports app.
    # Preserve its selected port so the child does not probe the next port.
    os.environ[RUNTIME_PORT_ENV] = str(SERVER_PORT)
    if os.getenv("COURSE_SELECT_NO_BROWSER", "").strip() != "1":
        timer = threading.Timer(1.2, open_browser)
        timer.daemon = True
        timer.start()
    reload_enabled = os.getenv("COURSE_SELECT_DEV", "").strip() == "1"
    if reload_enabled:
        logger.info("Backend development reload enabled")
    uvicorn.run(
        "app:app" if reload_enabled else app,
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=reload_enabled,
        reload_dirs=[str(Path(__file__).resolve().parent)] if reload_enabled else None,
    )


if __name__ == "__main__":
    start_server()
