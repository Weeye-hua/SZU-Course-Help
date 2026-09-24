from __future__ import annotations

import asyncio
import threading
import time

import requests
from starlette.testclient import TestClient

import app
import config
import database
import logic
from database import DatabaseManager
from security import key_manager
from services import cart_service, course_cache_service, enroll_service

client = TestClient(app.app)


def test_runtime_registers_startup_and_shutdown_hooks():
    assert app.app.router.lifespan_context is app.app_lifespan


def test_startup_hook_starts_one_session_coordinator_without_disk_restore(monkeypatch):
    started = []
    monkeypatch.setattr(app, "_runtime_started", False)
    monkeypatch.setattr(
        type(app.session_manager),
        "keepalive_running",
        property(lambda _manager: bool(started)),
    )
    monkeypatch.setattr(
        app.session_manager,
        "start_keepalive",
        lambda callback, should_run, *, interval_seconds: (
            started.append((callback, should_run, interval_seconds)) or True
        ),
    )

    asyncio.run(app.startup_runtime_services())
    asyncio.run(app.startup_runtime_services())

    assert started == [(app._keep_alive_once, app._has_session_work, 60)]
    assert app._runtime_started is True


def set_logged_session(monkeypatch, *, batch_code="batch", batch_name="预选阶段"):
    monkeypatch.setattr(config, "token", "token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "elective_batch_code", batch_code)
    monkeypatch.setattr(config, "elective_batch_name", batch_name)
    monkeypatch.setattr(config, "campus_code", "01")
    monkeypatch.setattr(config, "campus_name", "粤海校区")


def test_health_and_static_login_page():
    assert client.get("/api/health").status_code == 200
    assert app.get_login_url() == f"{app.get_server_url()}/login"
    response = client.get("/login")
    assert response.status_code == 200
    assert "进入抢课工作台" in response.text
    offline = client.get("/offline")
    assert offline.status_code == 200
    assert "本地缓存" in offline.text

    bootstrap = client.get("/api/bootstrap").json()
    assert "ui_cache_token" not in bootstrap
    assert bootstrap["ui_asset_build"] == app.UI_ASSET_BUILD


def test_api_allows_local_origins_and_missing_origin():
    local = client.get("/api/health", headers={"Origin": app.LOCAL_ORIGINS[0]})
    assert local.status_code == 200
    assert local.headers["access-control-allow-origin"] == app.LOCAL_ORIGINS[0]

    localhost = client.get("/api/health", headers={"Origin": app.LOCAL_ORIGINS[1]})
    assert localhost.status_code == 200
    assert localhost.headers["access-control-allow-origin"] == app.LOCAL_ORIGINS[1]

    direct = client.get("/api/health")
    assert direct.status_code == 200


def test_api_rejects_non_local_and_lookalike_origins(monkeypatch):
    monkeypatch.setattr(
        app,
        "get_session_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("请求不应进入业务处理")),
    )

    for origin in ("https://evil.example", f"{app.LOCAL_ORIGINS[0]}.evil.example"):
        response = client.get("/api/session", headers={"Origin": origin})
        assert response.status_code == 403
        assert response.json()["error_code"] == "INVALID_ORIGIN"


def test_api_accepts_origin_for_the_actual_loopback_request_port():
    local_client = TestClient(app.app, base_url="http://127.0.0.1:8000")

    response = local_client.get(
        "/api/health",
        headers={"Origin": "http://127.0.0.1:8000"},
    )

    assert response.status_code == 200

    preflight = local_client.options(
        "/api/captcha/solve",
        headers={
            "Origin": "http://127.0.0.1:8000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:8000"


def test_api_preflight_rejects_non_local_origin_and_allows_local_origin():
    local = client.options(
        "/api/session",
        headers={
            "Origin": app.LOCAL_ORIGINS[0],
            "Access-Control-Request-Method": "GET",
        },
    )
    assert local.status_code == 200
    assert local.headers["access-control-allow-origin"] == app.LOCAL_ORIGINS[0]

    external = client.options(
        "/api/session",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert external.status_code == 403
    assert external.json()["error_code"] == "INVALID_ORIGIN"


def test_backend_selection_endpoint_updates_preference(monkeypatch):
    monkeypatch.setattr(config, "backend_preference", config.BACKEND_AUTO)
    monkeypatch.setattr(config, "webvpn_cookie", "")

    response = client.post("/api/backend/select", json={"backend": "primary"})

    assert response.status_code == 200
    assert response.json()["preference"] == config.BACKEND_PRIMARY
    assert config.backend_preference == config.BACKEND_PRIMARY


def test_backend_selection_requires_webvpn_auth(monkeypatch):
    monkeypatch.setattr(config, "webvpn_cookie", "")

    response = client.post("/api/backend/select", json={"backend": "webvpn"})

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "WEBVPN_AUTH_REQUIRED"
    assert body["preference"] == config.BACKEND_WEBVPN


def test_webvpn_auth_start_and_status_routes(monkeypatch):
    monkeypatch.setattr(
        app.webvpn_auth_service,
        "start_auth",
        lambda: {"state": "pending", "authenticated": False, "message": "请完成认证"},
    )
    monkeypatch.setattr(
        app.webvpn_auth_service,
        "get_status",
        lambda: {"state": "pending", "authenticated": False, "message": "请完成认证"},
    )

    started = client.post("/api/webvpn/auth/start")
    assert started.status_code == 200
    assert started.json()["state"] == "pending"

    current = client.get("/api/webvpn/auth/status")
    assert current.status_code == 200
    assert current.json() == {
        "state": "pending",
        "authenticated": False,
        "message": "请完成认证",
    }


def test_school_proxy_route_is_explicitly_disabled():
    response = client.get("/proxy/bkxk.szu.edu.cn/xsxkapp/sys/xsxkapp/%2Adefault/index.do")
    assert response.status_code == 410
    assert response.json()["error_code"] == "SCHOOL_PROXY_DISABLED"

    mutating = client.post("/proxy/bkxk.szu.edu.cn/enroll", content=b"unsafe")
    assert mutating.status_code == 410
    assert mutating.json()["error_code"] == "SCHOOL_PROXY_DISABLED"


def test_card_key_is_terminal_prefilled_and_has_no_web_issuance_endpoint():
    app.configure_runtime_prefill("2024110122", "SZU3.terminal-only")
    try:
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["student_id"] == "2024110122"
        assert bootstrap["card_key"] == "SZU3.terminal-only"
        assert client.post("/api/card_key", json={"student_id": "2024110122"}).status_code == 405
    finally:
        app.configure_runtime_prefill("", "")


def test_captcha_api_applies_requested_webvpn_backend(monkeypatch):
    monkeypatch.setattr(config, "backend_preference", config.BACKEND_PRIMARY)
    monkeypatch.setattr(
        config,
        "webvpn_cookie",
        "_webvpn_key=key; webvpn_username=user; webvpn_username_NS_Sig=sig",
    )
    observed = {}

    def fake_fetch(max_attempts):
        observed["max_attempts"] = max_attempts
        observed["preference"] = config.backend_preference
        return {
            "vtoken": "vtoken",
            "cookie": "route=fresh; insert_cookie=fresh",
            "imageUrl": "data:image/jpeg;base64,/9j/",
        }

    monkeypatch.setattr(app.logic, "fetch_vtoken_and_image", fake_fetch)

    response = client.get("/api/captcha?backend=webvpn")

    assert response.status_code == 200
    assert observed == {"max_attempts": 1, "preference": config.BACKEND_WEBVPN}


def test_captcha_api_reports_closed_window_without_generic_502(monkeypatch):
    monkeypatch.setattr(
        app.logic,
        "fetch_vtoken_and_image",
        lambda *_: (_ for _ in ()).throw(logic.CaptchaUnavailableError("closed")),
    )

    response = client.get("/api/captcha")

    assert response.status_code == 409
    assert response.json()["error_code"] == "CAPTCHA_UNAVAILABLE"
    assert response.json()["retryable"] is True
    assert "当前未提供登录验证码" in response.json()["message"]


def test_captcha_api_reports_finite_timeout(monkeypatch):
    monkeypatch.setattr(
        app.logic,
        "fetch_vtoken_and_image",
        lambda *_: (_ for _ in ()).throw(requests.Timeout("slow")),
    )

    response = client.get("/api/captcha")

    assert response.status_code == 504
    assert response.json()["error_code"] == "CAPTCHA_TIMEOUT"
    assert "本次加载已停止" in response.json()["message"]


def test_captcha_api_reports_network_failure(monkeypatch):
    monkeypatch.setattr(
        app.logic,
        "fetch_vtoken_and_image",
        lambda *_: (_ for _ in ()).throw(requests.ConnectionError("offline")),
    )

    response = client.get("/api/captcha")

    assert response.status_code == 503
    assert response.json()["error_code"] == "CAPTCHA_NETWORK_ERROR"
    assert response.json()["retryable"] is True


def test_captcha_api_reports_malformed_school_response(monkeypatch):
    monkeypatch.setattr(
        app.logic,
        "fetch_vtoken_and_image",
        lambda *_: (_ for _ in ()).throw(logic.CaptchaResponseError("bad payload")),
    )

    response = client.get("/api/captcha")

    assert response.status_code == 502
    assert response.json()["error_code"] == "CAPTCHA_INVALID_RESPONSE"
    assert "本次加载已停止" in response.json()["message"]


def test_courses_require_login(monkeypatch):
    monkeypatch.setattr(config, "token", "")
    monkeypatch.setattr(config, "combined_cookie", "")
    response = client.get("/api/school/courses?type=TJKC&page=1")
    assert response.status_code == 401


def test_courses_report_background_session_recovery_without_manual_login_prompt(monkeypatch):
    monkeypatch.setattr(config, "token", "")
    monkeypatch.setattr(config, "combined_cookie", "")
    monkeypatch.setattr(
        app,
        "get_session_snapshot",
        lambda: {
            "relogin_in_progress": True,
            "relogin_status": "running",
        },
    )
    monkeypatch.setattr(
        app,
        "get_enroll_task_state",
        lambda: {"running": True, "paused": False},
    )

    response = client.get("/api/school/courses?type=TJKC&page=1")

    assert response.status_code == 409
    assert response.json()["error_code"] == "SESSION_RECOVERY_IN_PROGRESS"
    assert response.json()["requires_manual_login"] is False


def test_session_uses_backend_phase_classification(monkeypatch):
    monkeypatch.setattr(config, "token", "token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "elective_batch_code", "batch")
    monkeypatch.setattr(config, "elective_batch_name", "预选阶段")

    body = client.get("/api/session").json()
    assert body["phase"] == config.PHASE_PRESELECTION
    assert body["automatic_enroll_allowed"] is False

    monkeypatch.setattr(config, "elective_batch_name", "补选阶段")
    body = client.get("/api/session").json()
    assert body["phase"] == config.PHASE_AUTOMATIC
    assert body["automatic_enroll_allowed"] is True

    monkeypatch.setattr(config, "elective_batch_name", "补选已结束")
    body = client.get("/api/session").json()
    assert body["phase"] == config.PHASE_CLOSED
    assert body["automatic_enroll_allowed"] is False


def test_session_snapshot_is_memory_only_and_does_not_probe_school(monkeypatch):
    set_logged_session(monkeypatch)
    monkeypatch.setattr(
        app,
        "refresh_elective_batch",
        lambda *_args: (_ for _ in ()).throw(AssertionError("session GET must stay local")),
    )

    response = client.get("/api/session")

    assert response.status_code == 200
    assert response.json()["logged_in"] is True


def test_course_api_converts_ui_pages_to_school_zero_based_pages(monkeypatch):
    observed_pages = []

    def fake_query(course_type, school_page, context):
        observed_pages.append((course_type, school_page))
        assert context.student_id == "2024110122"
        return (
            True,
            {
                "total_count": 0,
                "courses": [],
                "msg": "",
                "is_error": False,
            },
            "本班课程(推荐)",
        )

    set_logged_session(monkeypatch)
    monkeypatch.setattr(app, "query_courses", fake_query)

    first_page = client.get("/api/school/courses?type=TJKC&page=1&page_size=10")
    second_page = client.get("/api/school/courses?type=TJKC&page=2&page_size=10")

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert observed_pages == [("TJKC", 0), ("TJKC", 1)]


def test_live_course_response_updates_persistent_cache(monkeypatch, tmp_path):
    set_logged_session(monkeypatch)
    monkeypatch.setattr(course_cache_service, "_path", tmp_path / "courses.json")
    payload = {
        "total_count": 1,
        "courses": [{"course_name": "实时课程"}],
        "msg": "",
        "is_error": False,
    }
    monkeypatch.setattr(app, "query_courses", lambda *_args: (True, payload, "课程"))

    response = client.get("/api/school/courses?type=TJKC&page=1&page_size=10")

    assert response.status_code == 200
    body = response.json()
    assert body["cached"] is False
    assert body["has_cache"] is True
    assert body["cached_at"] > 0
    assert body["cache_schema_version"] == course_cache_service.CACHE_SCHEMA_VERSION
    scope = course_cache_service.scope_from_snapshot(app.get_session_snapshot())
    assert course_cache_service.get_page(scope, "TJKC", 1, 10)["courses"] == payload["courses"]


def test_cache_mode_returns_cached_course_without_school_request(monkeypatch, tmp_path):
    set_logged_session(monkeypatch, batch_name="补选已结束")
    monkeypatch.setattr(course_cache_service, "_path", tmp_path / "courses.json")
    payload = {
        "total_count": 1,
        "courses": [{"course_name": "缓存课程"}],
        "msg": "",
        "is_error": False,
    }
    scope = course_cache_service.scope_from_snapshot(app.get_session_snapshot())
    course_cache_service.put_page(scope, "TJKC", 1, 10, payload)
    monkeypatch.setattr(
        app,
        "query_courses",
        lambda *_args: (_ for _ in ()).throw(AssertionError("cache hit must not query school")),
    )

    response = client.get("/api/school/courses?type=TJKC&page=1&page_size=10&cache_mode=true")

    assert response.status_code == 200
    assert response.json()["cached"] is True
    assert response.json()["courses"] == payload["courses"]


def test_cache_mode_reads_only_terminal_accounts_latest_scoped_catalog(monkeypatch, tmp_path):
    monkeypatch.setattr(course_cache_service, "_path", tmp_path / "courses.json")
    payload = {
        "total_count": 2,
        "courses": [{"course_name": "离线课程一"}, {"course_name": "离线课程二"}],
        "msg": "",
        "is_error": False,
    }
    scope = course_cache_service.CatalogCacheScope("2024110122", "batch-a", "01")
    course_cache_service.put_full(scope, "FANKC", payload)
    monkeypatch.setattr(config, "token", "")
    monkeypatch.setattr(config, "combined_cookie", "")
    monkeypatch.setattr(config, "student_id", "")
    app.configure_runtime_prefill("2024110122", "SZU3.terminal")
    monkeypatch.setattr(
        app,
        "query_courses",
        lambda *_args: (_ for _ in ()).throw(AssertionError("offline cache must not query school")),
    )

    try:
        response = client.get("/api/school/courses?type=FANKC&page=99&page_size=10&cache_mode=true")
        assert response.status_code == 200
        assert response.json()["full_catalog"] is True
        assert response.json()["courses"] == payload["courses"]
        assert response.json()["cache_scope"] == {
            "batch_code": "batch-a",
            "campus_code": "01",
        }
    finally:
        app.configure_runtime_prefill("", "")


def test_full_catalog_refresh_collects_all_pages(monkeypatch, tmp_path):
    set_logged_session(monkeypatch)
    monkeypatch.setattr(course_cache_service, "_path", tmp_path / "courses.json")
    first_page = {
        "total_count": 11,
        "courses": [{"course_name": f"课程{i}"} for i in range(10)],
        "msg": "",
        "is_error": False,
    }
    last_page = {
        "total_count": 11,
        "courses": [{"course_name": "课程10"}],
        "msg": "",
        "is_error": False,
    }
    monkeypatch.setattr(
        app,
        "query_courses",
        lambda course_type, page, context: (
            (True, last_page, "方案内课程")
            if (course_type, page) == ("FANKC", 1)
            else (_ for _ in ()).throw(AssertionError("unexpected page"))
        ),
    )

    scope, context = app._catalog_context()
    app._cache_full_catalog(scope, context, "FANKC", 1, first_page)

    cached = course_cache_service.get_full(scope, "FANKC")
    assert cached["total_count"] == 11
    assert [item["course_name"] for item in cached["courses"]] == [f"课程{i}" for i in range(11)]


def test_full_catalog_refresh_discards_response_when_scope_changes_mid_request(
    monkeypatch,
    tmp_path,
):
    set_logged_session(monkeypatch)
    monkeypatch.setattr(course_cache_service, "_path", tmp_path / "courses.json")
    first_page = {
        "total_count": 11,
        "courses": [{"course_name": f"课程{i}"} for i in range(10)],
        "msg": "",
        "is_error": False,
    }
    last_page = {
        "total_count": 11,
        "courses": [{"course_name": "课程10"}],
        "msg": "",
        "is_error": False,
    }
    scope, context = app._catalog_context()

    def switch_scope_during_query(course_type, page, request_context):
        assert (course_type, page, request_context) == ("FANKC", 1, context)
        config.campus_code = "02"
        return True, last_page, "方案内课程"

    monkeypatch.setattr(app, "query_courses", switch_scope_during_query)

    app._cache_full_catalog(scope, context, "FANKC", 1, first_page)

    assert course_cache_service.get_page(scope, "FANKC", 1, 10) is not None
    assert course_cache_service.get_page(scope, "FANKC", 2, 10) is None
    assert course_cache_service.get_full(scope, "FANKC") is None


def test_cache_mode_miss_never_falls_back_to_school(monkeypatch, tmp_path):
    set_logged_session(monkeypatch)
    monkeypatch.setattr(course_cache_service, "_path", tmp_path / "courses.json")
    payload = {
        "total_count": 1,
        "courses": [{"course_name": "实时回退课程"}],
        "msg": "",
        "is_error": False,
    }
    observed = []
    monkeypatch.setattr(
        app,
        "query_courses",
        lambda *args: observed.append(args) or (True, payload, "课程"),
    )

    response = client.get("/api/school/courses?type=TJKC&page=1&page_size=10&cache_mode=true")

    assert response.status_code == 404
    assert response.json()["error_code"] == "COURSE_CACHE_MISS"
    assert observed == []


def test_live_refresh_failure_keeps_existing_course_cache(monkeypatch, tmp_path):
    set_logged_session(monkeypatch)
    monkeypatch.setattr(course_cache_service, "_path", tmp_path / "courses.json")
    original = {
        "total_count": 1,
        "courses": [{"course_name": "保留课程"}],
        "msg": "",
        "is_error": False,
    }
    scope = course_cache_service.scope_from_snapshot(app.get_session_snapshot())
    course_cache_service.put_page(scope, "TJKC", 1, 10, original)
    monkeypatch.setattr(
        app,
        "query_courses",
        lambda *_args: (_ for _ in ()).throw(requests.Timeout("slow")),
    )

    response = client.get("/api/school/courses?type=TJKC&page=1&page_size=10")

    assert response.status_code == 504
    assert course_cache_service.get_page(scope, "TJKC", 1, 10)["courses"] == original["courses"]


def test_closed_phase_does_not_query_school_course_endpoint(monkeypatch):
    set_logged_session(monkeypatch, batch_name="补选已结束")
    monkeypatch.setattr(
        app,
        "query_courses",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not query school")),
    )

    response = client.get("/api/school/courses?type=TJKC&page=1&page_size=10")

    assert response.status_code == 409
    assert response.json()["error_code"] == "COURSE_WINDOW_CLOSED"
    assert response.json()["retryable"] is True


def test_missing_batch_does_not_query_school_course_endpoint(monkeypatch):
    set_logged_session(monkeypatch, batch_code="", batch_name="")
    monkeypatch.setattr(
        app,
        "query_courses",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not query school")),
    )

    response = client.get("/api/school/courses?type=TJKC&page=1&page_size=10")

    assert response.status_code == 409
    assert response.json()["error_code"] == "BATCH_UNAVAILABLE"


def test_session_refresh_updates_phase_from_school_batch(monkeypatch):
    set_logged_session(monkeypatch, batch_name="预选阶段")

    def refresh_to_automatic(*_args):
        config.elective_batch_code = "fresh-batch"
        config.elective_batch_name = "补选阶段"
        return config.elective_batch_name

    monkeypatch.setattr(app, "refresh_elective_batch", refresh_to_automatic)

    response = client.post("/api/session/refresh")

    assert response.status_code == 200
    assert response.json()["phase"] == config.PHASE_AUTOMATIC
    assert response.json()["automatic_enroll_allowed"] is True


def test_session_refresh_clears_stale_batch_when_none_is_available(monkeypatch):
    set_logged_session(monkeypatch, batch_code="stale", batch_name="预选阶段")
    monkeypatch.setattr(
        app,
        "refresh_elective_batch",
        lambda *_args: (_ for _ in ()).throw(logic.ElectiveBatchUnavailableError("当前没有批次")),
    )

    response = client.post("/api/session/refresh")

    assert response.status_code == 409
    assert response.json()["error_code"] == "BATCH_UNAVAILABLE"
    assert response.json()["session"]["batch_code"] == ""
    assert config.elective_batch_code == ""
    assert config.elective_batch_name == ""


def test_session_refresh_reports_retryable_school_network_failure(monkeypatch):
    set_logged_session(monkeypatch)
    monkeypatch.setattr(
        app,
        "refresh_elective_batch",
        lambda *_args: (_ for _ in ()).throw(requests.ConnectionError("offline")),
    )

    response = client.post("/api/session/refresh")

    assert response.status_code == 503
    assert response.json()["error_code"] == "SCHOOL_NETWORK_ERROR"
    assert response.json()["retryable"] is True


def test_course_api_reports_retryable_school_timeout(monkeypatch):
    set_logged_session(monkeypatch)
    monkeypatch.setattr(
        app,
        "query_courses",
        lambda *_args: (_ for _ in ()).throw(requests.Timeout("slow")),
    )

    response = client.get("/api/school/courses?type=TJKC&page=1&page_size=10")

    assert response.status_code == 504
    assert response.json()["error_code"] == "SCHOOL_TIMEOUT"
    assert response.json()["retryable"] is True


def test_course_api_rejects_zero_ui_page():
    response = client.get("/api/school/courses?type=TJKC&page=0&page_size=10")
    assert response.status_code == 422


def test_login_route_validates_real_card_key_and_saves_mocked_school_session(tmp_path, monkeypatch):
    monkeypatch.setenv("COURSE_SELECT_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setattr(config, "student_id", "")
    monkeypatch.setattr(config, "password", "")
    monkeypatch.setattr(config, "combined_cookie", "")
    monkeypatch.setattr(config, "token", "mock-token")
    card_key = key_manager.generate_card_key("2024110122")
    monkeypatch.setattr(
        app,
        "perform_school_login",
        lambda *args: {
            "success": True,
            "cookie": "JSESSIONID=session; _WEU=weu",
            "name": "测试用户",
            "token": "mock-token",
        },
    )
    monkeypatch.setattr(app, "refresh_elective_batch", lambda *args: None)

    response = client.post(
        "/api/login",
        json={
            "student_id": "2024110122",
            "password": "school-password",
            "card_key": card_key,
            "vtoken": "vtoken",
            "verifyCode": [[10, 30], [40, 40], [80, 50], [120, 60]],
            "cookie": "route=route-value; Path=/, insert_cookie=insert-value; Path=/",
        },
    )

    assert response.status_code == 200
    assert response.json()["is_error"] is False
    assert config.student_id == "2024110122"
    assert config.password == "school-password"
    assert "JSESSIONID=session" in config.combined_cookie
    assert "route=route-value" in config.combined_cookie


def test_conflicting_course_is_rejected_by_api(tmp_path, monkeypatch):
    monkeypatch.setattr(cart_service, "db", DatabaseManager(str(tmp_path / "api-cart.db")))
    monkeypatch.setattr(app, "is_enroll_task_running", lambda: False)
    response = client.post(
        "/api/courses/add",
        json={
            "id": "conflict-1",
            "type": "FANKC",
            "name": "冲突课程",
            "is_conflict": "1",
        },
    )
    assert response.status_code == 200
    assert response.json()["is_error"] is True
    assert cart_service.get_courses_by_status("") == []


def test_preselection_cannot_start_enrollment(tmp_path, monkeypatch):
    monkeypatch.setattr(cart_service, "db", DatabaseManager(str(tmp_path / "api-enroll.db")))
    cart_service.add_course(type("Course", (), {"id": "c1", "type": "FANKC", "name": "课程"})())
    monkeypatch.setattr(config, "token", "token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "elective_batch_code", "batch")
    monkeypatch.setattr(config, "elective_batch_name", "复选阶段")

    def refresh_to_preselection(*_args):
        config.elective_batch_code = "fresh-batch"
        config.elective_batch_name = "预选阶段"
        return config.elective_batch_name

    monkeypatch.setattr(app, "refresh_elective_batch", refresh_to_preselection)
    monkeypatch.setattr(
        app,
        "start_enroll_worker",
        lambda: (_ for _ in ()).throw(AssertionError("must not reserve")),
    )

    response = client.post(
        "/api/enroll/courses",
        json={"confirmed_phase": True},
    )
    assert response.status_code == 409
    assert "预选" in response.json()["message"]


def test_enrollment_does_not_start_when_phase_cannot_be_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(cart_service, "db", DatabaseManager(str(tmp_path / "phase-error.db")))
    cart_service.add_course(type("Course", (), {"id": "c1", "type": "FANKC", "name": "课程"})())
    monkeypatch.setattr(config, "token", "token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "elective_batch_code", "cached-batch")
    monkeypatch.setattr(config, "elective_batch_name", "复选阶段")
    monkeypatch.setattr(
        app,
        "refresh_elective_batch",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("network unavailable")),
    )
    monkeypatch.setattr(
        app,
        "start_enroll_worker",
        lambda: (_ for _ in ()).throw(AssertionError("must not reserve")),
    )

    response = client.post("/api/enroll/courses", json={"confirmed_phase": True})

    assert response.status_code == 503
    assert "未启动" in response.json()["message"]


def test_closed_batch_name_cannot_start_enrollment(tmp_path, monkeypatch):
    monkeypatch.setattr(cart_service, "db", DatabaseManager(str(tmp_path / "closed-phase.db")))
    cart_service.add_course(type("Course", (), {"id": "c1", "type": "FANKC", "name": "课程"})())
    monkeypatch.setattr(config, "token", "token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "elective_batch_code", "cached-batch")
    monkeypatch.setattr(config, "elective_batch_name", "补选阶段")

    def refresh_to_closed_phase(*_args):
        config.elective_batch_code = "fresh-batch"
        config.elective_batch_name = "补选已结束"
        return config.elective_batch_name

    monkeypatch.setattr(app, "refresh_elective_batch", refresh_to_closed_phase)
    monkeypatch.setattr(
        app,
        "start_enroll_worker",
        lambda: (_ for _ in ()).throw(AssertionError("must not reserve")),
    )

    response = client.post("/api/enroll/courses", json={"confirmed_phase": True})

    assert response.status_code == 409
    assert "未开放或已结束" in response.json()["message"]


def test_automatic_phase_starts_detached_enrollment_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(cart_service, "db", DatabaseManager(str(tmp_path / "start-worker.db")))
    cart_service.add_course(type("Course", (), {"id": "c1", "type": "FANKC", "name": "课程"})())
    monkeypatch.setattr(config, "token", "token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")

    def refresh_to_automatic(*_args):
        config.elective_batch_code = "fresh-batch"
        config.elective_batch_name = "正选"
        return config.elective_batch_name

    starts = []
    monkeypatch.setattr(app, "refresh_elective_batch", refresh_to_automatic)
    monkeypatch.setattr(app, "start_enroll_worker", lambda: starts.append(1) or True)

    response = client.post("/api/enroll/courses", json={"confirmed_phase": True})

    assert response.status_code == 200
    assert response.json()["is_error"] is False
    assert starts == [1]


def test_enrollment_start_explains_when_all_pending_courses_are_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(cart_service, "db", DatabaseManager(str(tmp_path / "disabled-only.db")))
    cart_service.add_course(
        type(
            "Course",
            (),
            {"id": "disabled", "type": "FANKC", "name": "已关闭自动抢课"},
        )()
    )
    assert cart_service.update_course_preferences("disabled", auto_enabled=False)
    set_logged_session(monkeypatch, batch_name="正选")
    monkeypatch.setattr(app, "refresh_elective_batch", lambda *_args: "正选")
    monkeypatch.setattr(
        app,
        "start_enroll_worker",
        lambda: (_ for _ in ()).throw(AssertionError("disabled course must not start worker")),
    )

    response = client.post("/api/enroll/courses", json={"confirmed_phase": True})

    assert response.status_code == 400
    body = response.json()
    assert body["error_code"] == "NO_ENABLED_PENDING_COURSE"
    assert "自动抢课" in body["message"]


def test_cart_preferences_rejects_coerced_boolean_and_unknown_fields():
    coerced = client.patch("/api/courses/course-1", json={"auto_enabled": "false"})
    unknown = client.patch("/api/courses/course-1", json={"auto_enabled": False, "other": 1})

    assert coerced.status_code == 422
    assert unknown.status_code == 422


def test_cart_preferences_rejects_null_or_empty_payload():
    null_value = client.patch("/api/courses/course-1", json={"auto_enabled": None})
    empty = client.patch("/api/courses/course-1", json={})

    assert null_value.status_code == 400
    assert null_value.json()["error_code"] == "INVALID_COURSE_PREFERENCE"
    assert empty.status_code == 400
    assert empty.json()["error_code"] == "INVALID_COURSE_PREFERENCE"


def test_worker_start_failure_preserves_pending_cart(tmp_path, monkeypatch):
    db = DatabaseManager(str(tmp_path / "worker-start-error.db"))
    monkeypatch.setattr(cart_service, "db", db)
    cart_service.add_course(type("Course", (), {"id": "c1", "type": "FANKC", "name": "课程"})())
    monkeypatch.setattr(config, "token", "token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")

    def refresh_to_automatic(*_args):
        config.elective_batch_code = "fresh-batch"
        config.elective_batch_name = "正选"
        return config.elective_batch_name

    monkeypatch.setattr(app, "refresh_elective_batch", refresh_to_automatic)
    monkeypatch.setattr(
        app,
        "start_enroll_worker",
        lambda: (_ for _ in ()).throw(RuntimeError("thread unavailable")),
    )

    response = client.post("/api/enroll/courses", json={"confirmed_phase": True})

    assert response.status_code == 500
    assert response.json()["error_code"] == "ENROLL_WORKER_START_FAILED"
    assert db.get_courses_by_status(database.STATUS_NOT_STARTED)[0]["id"] == "c1"


def test_pause_and_resume_enrollment_api(monkeypatch):
    monkeypatch.setattr(config, "token", "token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "elective_batch_code", "batch")
    monkeypatch.setattr(config, "elective_batch_name", "正选")
    monkeypatch.setattr(app, "refresh_elective_batch", lambda *_args: "正选")

    assert enroll_service.reserve_enroll_task()
    try:
        paused = client.post("/api/enroll/pause")
        assert paused.status_code == 200
        assert paused.json()["progress"]["paused"] is True

        resumed = client.post("/api/enroll/resume")
        assert resumed.status_code == 200
        assert resumed.json()["progress"]["paused"] is False
    finally:
        enroll_service._release_enroll_task()


def test_stop_enrollment_api_requests_graceful_shutdown():
    assert enroll_service.reserve_enroll_task()
    try:
        response = client.post("/api/enroll/stop")
        assert response.status_code == 202
        assert response.json()["is_error"] is False
        assert enroll_service.get_enroll_task_state()["stopping"] is True
    finally:
        enroll_service._release_enroll_task()


def test_resume_keeps_task_paused_when_school_phase_is_no_longer_automatic(monkeypatch):
    monkeypatch.setattr(config, "token", "token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "elective_batch_code", "batch")
    monkeypatch.setattr(config, "elective_batch_name", "正选")

    def refresh_to_preselection(*_args):
        config.elective_batch_code = "new-batch"
        config.elective_batch_name = "预选阶段"
        return config.elective_batch_name

    monkeypatch.setattr(app, "refresh_elective_batch", refresh_to_preselection)

    assert enroll_service.reserve_enroll_task()
    try:
        assert client.post("/api/enroll/pause").status_code == 200
        response = client.post("/api/enroll/resume")

        assert response.status_code == 409
        assert response.json()["error_code"] == "PHASE_NOT_ALLOWED"
        assert enroll_service.get_enroll_task_state()["paused"] is True
    finally:
        enroll_service._release_enroll_task()


def test_resume_requires_restored_school_login(monkeypatch):
    monkeypatch.setattr(config, "token", "")
    monkeypatch.setattr(config, "combined_cookie", "")
    monkeypatch.setattr(config, "student_id", "2024110122")

    assert enroll_service.reserve_enroll_task()
    try:
        assert client.post("/api/enroll/pause").status_code == 200
        response = client.post("/api/enroll/resume")
        assert response.status_code == 409
        assert response.json()["error_code"] == "LOGIN_REQUIRED_FOR_RESUME"
    finally:
        enroll_service._release_enroll_task()


def test_failed_cart_course_can_be_requeued(tmp_path, monkeypatch):
    db = DatabaseManager(str(tmp_path / "retry-course.db"))
    monkeypatch.setattr(cart_service, "db", db)
    course = type("Course", (), {"id": "retry-1", "type": "FANKC", "name": "重试课程"})()
    assert cart_service.add_course(course)["success"]
    assert cart_service.update_status(course.id, database.STATUS_FAILED)

    response = client.post("/api/courses/retry?id=retry-1")

    assert response.status_code == 200
    assert response.json()["is_error"] is False
    assert db.get_courses_by_status(database.STATUS_NOT_STARTED)[0]["id"] == "retry-1"


def test_failed_cart_course_can_be_removed_after_task_finishes(tmp_path, monkeypatch):
    db = DatabaseManager(str(tmp_path / "remove-failed.db"))
    monkeypatch.setattr(cart_service, "db", db)
    course = type(
        "Course",
        (),
        {"id": "failed-1", "type": "FANKC", "name": "已停止课程"},
    )()
    assert cart_service.add_course(course)["success"]
    assert cart_service.update_status(course.id, database.STATUS_FAILED)
    enroll_service._reset_progress([course])
    enroll_service._update_course_progress(course.id, status=database.STATUS_FAILED)

    try:
        response = client.post("/api/courses/delete?id=failed-1")

        assert response.status_code == 200
        assert response.json()["is_error"] is False
        assert response.json()["progress"]["courses"] == []
        assert db.get_courses_by_status("") == []
    finally:
        enroll_service._set_progress_finished()


def test_delete_api_waits_for_safe_pause_boundary(tmp_path, monkeypatch):
    db = DatabaseManager(str(tmp_path / "remove-paused-api.db"))
    monkeypatch.setattr(cart_service, "db", db)
    course = type(
        "Course",
        (),
        {"id": "paused-1", "type": "FANKC", "name": "暂停课程"},
    )()
    assert cart_service.add_course(course)["success"]
    assert cart_service.update_status(course.id, database.STATUS_IN_PROGRESS)
    enroll_service._reset_progress([course])

    assert enroll_service.reserve_enroll_task()
    waiter = None
    try:
        assert enroll_service.pause_enroll_task()[0]
        blocked = client.post("/api/courses/delete?id=paused-1")
        assert blocked.status_code == 409
        assert blocked.json()["error_code"] == "ENROLL_TASK_PAUSE_PENDING"

        waiter = threading.Thread(target=enroll_service._wait_until_resumed)
        waiter.start()
        deadline = time.monotonic() + 2
        while (
            not enroll_service.get_enroll_task_state()["pause_acknowledged"]
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        removed = client.post("/api/courses/delete?id=paused-1")
        assert removed.status_code == 200
        assert removed.json()["task_stopping"] is True
        assert removed.json()["progress"]["courses"] == []
        assert db.get_courses_by_status("") == []
    finally:
        enroll_service._release_enroll_task()
        enroll_service._set_progress_finished()
        if waiter is not None:
            waiter.join(timeout=2)


def test_keep_alive_skips_when_not_logged_in(monkeypatch):
    monkeypatch.setattr(config, "token", "")
    monkeypatch.setattr(config, "combined_cookie", "")
    called = []
    monkeypatch.setattr(app, "refresh_elective_batch", lambda *args: called.append(args))

    app._keep_alive_once()

    assert called == []


def test_keep_alive_starts_automatic_relogin_without_active_session(monkeypatch):
    monkeypatch.setattr(config, "token", "")
    monkeypatch.setattr(config, "combined_cookie", "")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    monkeypatch.setattr(app, "automatic_relogin_available", lambda: True)
    relogin_called = []
    monkeypatch.setattr(
        app,
        "attempt_automatic_relogin",
        lambda *args, **kwargs: relogin_called.append(args) or (True, ""),
    )

    app._keep_alive_once()

    assert relogin_called == [(config.ocr_relogin_max_attempts,)]


def test_click_relogin_endpoint_rejects_browser_credentials(monkeypatch):
    monkeypatch.setattr(config, "token", "")
    monkeypatch.setattr(config, "combined_cookie", "")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    called = []
    monkeypatch.setattr(
        app,
        "start_automatic_relogin",
        lambda **kwargs: called.append(kwargs) or (True, "正在自动重新登录，请稍候"),
    )

    response = client.post(
        "/api/session/recover",
        json={"student_id": "2024110122", "password": "secret", "backend": "webvpn"},
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "SESSION_RECOVERY_BODY_NOT_ALLOWED"
    assert called == []
    assert response.json()["message"] == "自动重登录只使用当前进程内存中的登录凭据"


def test_relogin_endpoint_accepts_bodyless_local_request(monkeypatch):
    monkeypatch.setattr(
        app, "start_automatic_relogin", lambda: (False, "没有可用于自动重登录的内存凭据")
    )

    response = client.post("/api/session/recover")

    assert response.status_code == 409
    assert response.json()["error_code"] == "SESSION_RECOVERY_UNAVAILABLE"
    assert response.json()["message"] == "没有可用于自动重登录的内存凭据"


def test_keep_alive_refreshes_session_when_logged_in(monkeypatch):
    monkeypatch.setattr(config, "token", "active-token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")
    called = []
    monkeypatch.setattr(app, "refresh_elective_batch", lambda *args: called.append(args))

    app._keep_alive_once()

    assert len(called) == 1
    assert called[0][0] == "2024110122"


def test_keep_alive_triggers_ocr_recovery_on_expiry(monkeypatch):
    monkeypatch.setattr(config, "token", "expired-token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(
        app,
        "refresh_elective_batch",
        lambda *args: (_ for _ in ()).throw(logic.SchoolBatchSessionExpiredError("expired")),
    )
    relogin_called = []
    monkeypatch.setattr(
        app,
        "attempt_automatic_relogin",
        lambda *args, **kwargs: relogin_called.append(args) or (True, ""),
    )

    app._keep_alive_once()

    assert len(relogin_called) == 1


def test_keep_alive_recovers_missing_session_from_memory_credentials(monkeypatch):
    monkeypatch.setattr(config, "token", "")
    monkeypatch.setattr(config, "combined_cookie", "")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "memory-only-secret")
    relogin_called = []
    monkeypatch.setattr(
        app,
        "attempt_automatic_relogin",
        lambda *args, **kwargs: relogin_called.append(args) or (True, ""),
    )

    app._keep_alive_once()

    assert relogin_called == [(config.ocr_relogin_max_attempts,)]


def test_keep_alive_uses_only_the_canonical_batch_probe(monkeypatch):
    monkeypatch.setattr(config, "token", "active-token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")
    enrolled_called = []
    batch_called = []
    monkeypatch.setattr(app, "get_enrolled_courses", lambda: enrolled_called.append(True))
    monkeypatch.setattr(app, "refresh_elective_batch", lambda *args: batch_called.append(args))

    app._keep_alive_once()

    assert len(batch_called) == 1
    assert enrolled_called == []


def test_keep_alive_network_failure_does_not_invalidate_or_start_relogin(monkeypatch):
    monkeypatch.setattr(config, "token", "active-token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(
        app,
        "refresh_elective_batch",
        lambda *args: (_ for _ in ()).throw(requests.ConnectionError("offline")),
    )
    relogin_called = []
    monkeypatch.setattr(
        app,
        "attempt_automatic_relogin",
        lambda *args, **kwargs: relogin_called.append(args) or (True, ""),
    )

    app._keep_alive_once()

    assert relogin_called == []
    assert config.token == "active-token"
    assert config.combined_cookie == "cookie"


def test_captcha_solve_rejects_missing_image():
    response = client.post("/api/captcha/solve", json={})
    assert response.status_code == 400
    assert response.json()["is_error"] is True


def test_captcha_solve_returns_points_when_ocr_succeeds(monkeypatch, tmp_path):
    import base64

    monkeypatch.setattr(logic, "_captcha_image_path", lambda: tmp_path / "image.jpg")
    monkeypatch.setattr(
        logic,
        "recognize_captcha_centers",
        lambda: [[10, 30], [40, 40], [80, 50], [120, 60]],
    )

    tiny_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    image_url = f"data:image/jpeg;base64,{base64.b64encode(tiny_jpeg).decode()}"

    response = client.post("/api/captcha/solve", json={"imageUrl": image_url})

    assert response.status_code == 200
    body = response.json()
    assert len(body["points"]) == 4
    assert body["points"][0] == [10, 30]


def test_captcha_solve_returns_empty_when_ocr_fails(monkeypatch, tmp_path):
    import base64

    monkeypatch.setattr(logic, "_captcha_image_path", lambda: tmp_path / "image.jpg")
    monkeypatch.setattr(logic, "recognize_captcha_centers", lambda: [])

    tiny_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    image_url = f"data:image/jpeg;base64,{base64.b64encode(tiny_jpeg).decode()}"

    response = client.post("/api/captcha/solve", json={"imageUrl": image_url})

    assert response.status_code == 200
    body = response.json()
    assert body["points"] == []
    assert "OCR" in body["message"]


def test_school_open_success_response_keeps_public_url(monkeypatch):
    monkeypatch.setattr(app, "open_external_url", lambda url: True)

    response = client.post("/api/school/open")

    assert response.status_code == 200
    body = response.json()
    assert body["is_error"] is False
    assert body["url"] == app.OFFICIAL_SCHOOL_HOME_URL
    assert body["message"] == "已请求系统浏览器打开学校官方选课页面"


def test_school_open_failure_response_keeps_public_url(monkeypatch):
    monkeypatch.setattr(app, "open_external_url", lambda url: False)

    response = client.post("/api/school/open")

    assert response.status_code == 503
    body = response.json()
    assert body["is_error"] is True
    assert body["error_code"] == "BROWSER_OPEN_FAILED"
    assert body["url"] == app.OFFICIAL_SCHOOL_HOME_URL
