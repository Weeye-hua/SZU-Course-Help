"""Graduate protocol tests. Every school response is synthetic; no live writes."""

from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

import pytest
import requests
from PIL import Image
from starlette.testclient import TestClient

import app
import choose_course
import config
import database
import logic
import main
import project_paths
from services import auth_service, cart_service, enroll_service
from services import graduate_service as graduate
from study_program import GRADUATE_BASE_URL, PROGRAM_ENV

SID = "2600000001"
CLASS_ID = "20261-01000000-0101010-1000000000000"


def response(payload=None, status=200, text=None, cookies=None):
    result = requests.Response()
    result.status_code = status
    result.encoding = "utf-8"
    result._content = (
        text if text is not None else json.dumps(payload or {}, ensure_ascii=False)
    ).encode()
    result.cookies = requests.cookies.cookiejar_from_dict(cookies or {})
    return result


def window(**overrides):
    data = {
        "lcxx": {
            "WID": "round-1",
            "MC": "研究生选课",
            "XNXQDM": "20261",
            "XKCL": 0,
            "KFKSSJ": "2026-09-01 12:00:00",
            "KFJSSJ": "2026-09-30 12:00:00",
        },
        "xksfkf": 1,
        "dqsj": "2026-09-08 12:00:00",
    }
    for key, value in overrides.items():
        if key in {"lcxx", "xksfkf", "dqsj"}:
            data[key] = value
        else:
            data["lcxx"][key] = value
    return graduate.parse_window(data)


@pytest.fixture(autouse=True)
def graduate_mode(monkeypatch, tmp_path):
    monkeypatch.setenv(PROGRAM_ENV, "graduate")
    monkeypatch.setenv("COURSE_SELECT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cart_service, "db", database.DatabaseManager(tmp_path / "cart.db"))
    monkeypatch.setattr(cart_service, "_graduate_account", "")
    for field, value in {
        "student_id": SID,
        "password": "test-only-password",
        "token": "local-generation",
        "combined_cookie": "route=r; sid=c",
        "elective_batch_code": "round-1",
        "elective_batch_name": "研究生选课",
    }.items():
        monkeypatch.setattr(config, field, value)
    monkeypatch.setattr(graduate, "_window", window())
    monkeypatch.setattr(graduate, "pace_catalog_request", lambda: None)
    enroll_service._release_enroll_task()
    yield
    enroll_service._release_enroll_task()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, config.PHASE_AUTOMATIC),
        ({"XKCL": 1}, config.PHASE_AUTOMATIC),
        ({"XKCL": 2}, config.PHASE_CLOSED),
        ({"XKCL": True}, config.PHASE_UNKNOWN),
        ({"xksfkf": 0}, config.PHASE_CLOSED),
        ({"dqsj": "2026-09-01 11:59:59"}, config.PHASE_CLOSED),
        ({"dqsj": "2026-09-01 12:00:00"}, config.PHASE_AUTOMATIC),
        ({"dqsj": "2026-09-30 12:00:00"}, config.PHASE_CLOSED),
        ({"KFJSSJ": "invalid"}, config.PHASE_UNKNOWN),
        ({"KFKSSJ": "2027-01-01 00:00:00"}, config.PHASE_UNKNOWN),
        ({"lcxx": None}, config.PHASE_UNKNOWN),
    ],
)
def test_open_window(changes, expected):
    assert window(**changes).status()[0] == expected


def test_window_uses_server_time_and_elapsed(monkeypatch):
    value = window(dqsj="2026-09-30 11:59:59")
    monkeypatch.setattr(graduate.time, "monotonic", lambda: value.received_at + 2)
    assert value.status()[0] == config.PHASE_CLOSED


@pytest.mark.parametrize("code", ["AbCd", "4aB9", " ABCD "])
def test_text_captcha(code):
    assert graduate.validate_text_captcha(code) == code.strip()


@pytest.mark.parametrize(
    "code", ["123", "12345", "一二三四", [[1, 2]] * 4, None, "a bC", "ＡＢＣＤ"]
)
def test_invalid_text_captcha(code):
    assert not graduate.validate_text_captcha(code)


def test_login_wire_cookie_rotation_and_profile(monkeypatch):
    calls = []
    replies = iter(
        [
            response({"code": 1}, cookies={"route": "new"}),
            response({"XH": SID, "CODE": 1}, cookies={"auth": "valid"}),
        ]
    )

    def fake(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return next(replies)

    monkeypatch.setattr(graduate, "_request", fake)
    result = graduate.login(SID, "captcha-token", "encrypted", "AbCd", "route=old; sid=xyz")
    assert result["success"]
    assert result["cookie"] == "route=new; sid=xyz; auth=valid"
    assert calls[0][2]["data"] == {
        "loginName": SID,
        "loginPwd": "encrypted",
        "verifyCode": "AbCd",
        "vtoken": "captcha-token",
    }
    assert result["token"] not in str(calls)


def test_login_requires_exact_account(monkeypatch):
    replies = iter([response({"code": 1}), response({"XH": "2600000002", "CODE": 1})])
    monkeypatch.setattr(graduate, "_request", lambda *a, **k: next(replies))
    assert not graduate.login(SID, "v", "p", "ABCD", "sid=c")["success"]


def test_request_stays_on_https_origin(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "request", lambda *a, **k: (calls.append((a, k)), response())[1])
    graduate._request("GET", "xsxkHome/loadStdInfo.do", cookie="sid=c")
    args, kwargs = calls[0]
    assert args[1] == GRADUATE_BASE_URL + "xsxkHome/loadStdInfo.do"
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["Cookie"] == "sid=c"
    assert "token" not in kwargs["headers"]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (302, ""),
        (403, ""),
        (200, '<html><input name="loginPwd"></html>'),
        (200, '{"msg":"请重新登录"}'),
    ],
)
def test_expiry_detection(status, body):
    with pytest.raises(graduate.GraduateSessionExpiredError):
        graduate._json(response(status=status, text=body))


def captcha_data():
    buffer = io.BytesIO()
    Image.new("RGB", (120, 40), "white").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def test_ocr_predict_api(monkeypatch):
    monkeypatch.setattr(
        logic, "_ddddocr_engines", lambda: (None, SimpleNamespace(predict=lambda raw: "AbC2"))
    )
    assert graduate.recognize_captcha(captcha_data()) == "AbC2"


def test_automatic_login_retries_50_captchas(monkeypatch):
    calls = []
    monkeypatch.setattr(graduate.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        graduate, "fetch_captcha", lambda: {"vtoken": "v", "cookie": "sid=c", "imageUrl": "image"}
    )
    monkeypatch.setattr(graduate, "recognize_captcha", lambda _: "ABCD")

    def login(*args):
        calls.append(args)
        return {"success": len(calls) == 50, "error_code": "3"}

    monkeypatch.setattr(graduate, "login", login)
    result, _ = graduate.automatic_login(SID, "test", 50)
    assert result["success"] and len(calls) == 50


def test_wrong_password_does_not_retry_50_times(monkeypatch):
    calls = []
    monkeypatch.setattr(
        graduate, "fetch_captcha", lambda: {"vtoken": "v", "cookie": "sid=c", "imageUrl": "image"}
    )
    monkeypatch.setattr(graduate, "recognize_captcha", lambda _: "ABCD")
    monkeypatch.setattr(
        graduate, "login", lambda *a: (calls.append(a), {"success": False, "error_code": "2"})[1]
    )
    assert not graduate.automatic_login(SID, "test", 50)[0]["success"]
    assert len(calls) == 1


def test_account_queues_are_separate_and_undergraduate_untouched(monkeypatch, tmp_path):
    cart_service.bind_graduate_account(SID)
    first_path = cart_service.db.db_path
    course = SimpleNamespace(id=CLASS_ID, type="GPN", name="测试课程", campus_code="")
    assert cart_service.add_course(course)["success"]
    cart_service.bind_graduate_account("2600000002")
    assert cart_service.db.get_all_courses() == []
    cart_service.bind_graduate_account(SID)
    assert cart_service.db.db_path == first_path
    assert len(cart_service.db.get_all_courses()) == 1
    assert "graduate" in project_paths.data_dir().parts
    monkeypatch.setenv(PROGRAM_ENV, "undergraduate")
    assert project_paths.data_dir() == tmp_path


def test_query_catalog_maps_class_not_course_id(monkeypatch):
    calls = []
    row = {
        "BJDM": CLASS_ID,
        "KCDM": "0101010",
        "KCMC": "测试课",
        "KXRS": 20,
        "DQRS": 20,
        "IS_CONFLICT": 1,
        "PKSJDDMS": "3-14周 星期五[3-4节]教学楼101",
    }

    def fake(method, path, **kwargs):
        calls.append((path, kwargs))
        return response({"datas": [row], "total": 11, "pageIndex": 2, "pageSize": 10})

    monkeypatch.setattr(graduate, "_request", fake)
    monkeypatch.setattr(graduate, "selected_rows", lambda _: [{"BJDM": CLASS_ID}])
    result = (
        graduate.query_catalog("GCROSS", 1, keyword="计算", hide_full=True)
        .to_course_list_response()
        .to_api_dict()
    )
    item = result["courses"][0]["tcList"][0]
    assert item["teaching_class_id"] == CLASS_ID
    assert item["is_choose"] == item["is_conflict"] == item["is_full"] == "1"
    assert "[" not in item["teaching_place"]
    assert calls[0][0].endswith("loadWzyCourseInfo.do")
    assert calls[0][1]["data"]["query_keyword"] == "计算"
    assert calls[0][1]["data"]["query_sfym"] == "0"


def test_valid_empty_catalog(monkeypatch):
    monkeypatch.setattr(
        graduate,
        "_request",
        lambda *a, **k: response({"datas": [], "total": 0, "pageIndex": 1, "pageSize": 10}),
    )
    monkeypatch.setattr(graduate, "selected_rows", lambda _: [])
    assert graduate.query_catalog("GCROSS", 0).total_count == 0


@pytest.mark.parametrize(
    "payload", [{"results": None}, {"results": [{}]}, {"results": [{"BJDM": ""}]}, {"code": 1}]
)
def test_malformed_selected_is_not_empty_success(monkeypatch, payload):
    monkeypatch.setattr(graduate, "_request", lambda *a, **k: response(payload))
    with pytest.raises(graduate.GraduateResponseError):
        graduate.query_selected("sid=c")


@pytest.mark.parametrize(("course_type", "lx"), [("GPN", "2"), ("GCROSS", "6")])
def test_submit_uses_exact_grad_type_and_no_campus(monkeypatch, course_type, lx):
    calls = []
    monkeypatch.setattr(
        graduate, "_request", lambda *a, **k: (calls.append((a, k)), response({"code": 1}))[1]
    )
    choose_course.submit_course_selection(CLASS_ID, course_type, "02")
    args, kwargs = calls[0]
    assert args == ("POST", "xsxkCourse/choiceCourse.do")
    assert kwargs["data"] == {"bjdm": CLASS_ID, "lx": lx}


def test_query_only_cannot_enqueue_or_submit():
    assert not cart_service.add_course(SimpleNamespace(id=CLASS_ID, type="GALL", name="查询课程"))[
        "success"
    ]
    with pytest.raises(ValueError):
        graduate.submit_selection(CLASS_ID, "GALL")


def test_closed_window_never_submits(monkeypatch):
    monkeypatch.setattr(graduate, "_window", window(xksfkf=0))
    result = graduate.submit_selection(CLASS_ID, "GPN")
    assert enroll_service._classify_response(result) == "window_closed"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"code": 1}, "success"),
        ({"code": 2, "msg": "与已选课程时间冲突"}, "terminal"),
        ({"code": 2, "msg": "容量已满"}, "retry"),
        ({"code": 2, "msg": "已选该课程"}, "already_selected"),
    ],
)
def test_graduate_response_classification(payload, expected):
    assert enroll_service._classify_response(response(payload)) == expected


def test_success_confirmation_requires_exact_class(monkeypatch):
    course = enroll_service.EnrollmentCourse(CLASS_ID, "GPN", "测试课程")
    monkeypatch.setattr(enroll_service, "ENROLLMENT_CONFIRM_RETRY_SECONDS", 0)
    monkeypatch.setattr(
        graduate,
        "selected_rows",
        lambda *a, **k: [{"BJDM": CLASS_ID + "-other", "KCDM": "0101010"}],
    )
    assert (
        enroll_service._confirm_course_enrolled(course)
        == enroll_service.EnrollmentConfirmation.ABSENT
    )
    monkeypatch.setattr(graduate, "selected_rows", lambda *a, **k: [{"BJDM": CLASS_ID}])
    assert (
        enroll_service._confirm_course_enrolled(course)
        == enroll_service.EnrollmentConfirmation.CONFIRMED
    )


def test_timetable_uses_structured_periods_and_keeps_unscheduled(monkeypatch):
    monkeypatch.setattr(
        graduate,
        "query_selected",
        lambda _: [
            {"teachingClassID": CLASS_ID, "courseName": "实验课"},
            {"teachingClassID": "no-time", "courseName": "实践课"},
        ],
    )
    monkeypatch.setattr(
        graduate,
        "_request",
        lambda *a, **k: response(
            {
                "results": [
                    {
                        "BJDM": CLASS_ID,
                        "XQ": 3,
                        "KSJCDM": 1,
                        "JSJCDM": 2,
                        "ZCMC": "3-13周(单)",
                        "JASMC": "教学楼101",
                    },
                    {"BJDM": CLASS_ID, "XQ": 5, "KSJCDM": 13, "JSJCDM": 14, "ZCMC": "2-14周(双)"},
                    {"BJDM": "stale-class", "XQ": 1, "KSJCDM": 1, "JSJCDM": 2},
                ]
            }
        ),
    )
    result = graduate.query_timetable("sid=c")
    assert result["total_count"] == 2
    assert len(result["timetable"]["entries"]) == 2
    assert result["timetable"]["unscheduled_count"] == 1


def test_program_api_and_webvpn_guards(monkeypatch):
    client = TestClient(app.app)
    assert client.get("/api/bootstrap").json()["captcha_kind"] == "text"
    session = client.get("/api/session").json()
    assert session["campus_options"] == [] and session["campus_code"] == ""
    assert session["automatic_enroll_allowed"]
    assert client.post("/api/backend/select", json={"backend": "webvpn"}).status_code == 400
    assert client.post("/api/webvpn/auth/start").status_code == 400
    assert client.post("/api/session/campus", json={"campus_code": "02"}).status_code == 400
    monkeypatch.setattr(graduate, "recognize_captcha", lambda _: "ABCD")
    assert client.post("/api/captcha/solve", json={"imageUrl": "test"}).json()["text"] == "ABCD"


def test_terminal_program_selection(monkeypatch):
    monkeypatch.delenv(PROGRAM_ENV)
    answers = iter(["invalid", "2"])
    monkeypatch.setattr(main, "safe_input", lambda _: next(answers))
    assert main.select_study_program() == "graduate"


def test_undergraduate_rejects_text_captcha(monkeypatch):
    monkeypatch.setenv(PROGRAM_ENV, "undergraduate")
    assert auth_service.validate_login_params(SID, "p", "key", "ABCD", "v", "sid=c")


def test_relogin_restores_graduate_session_and_progress(monkeypatch):
    calls = []

    def fake(sid, password, attempts, progress=None):
        calls.append(attempts)
        progress(2, attempts)
        assert auth_service.get_session_snapshot()["relogin_in_progress"]
        assert "2/50" in auth_service.get_session_snapshot()["relogin_message"]
        return {"success": True, "cookie": "sid=new", "token": "new-local-generation"}, "route=abc"

    monkeypatch.setattr(graduate, "automatic_login", fake)
    monkeypatch.setattr(logic, "fetch_elective_batch", lambda *a: window())
    assert auth_service.attempt_automatic_relogin(50) == (True, "")
    assert calls == [50]
    assert config.combined_cookie == "route=abc; sid=new"
    assert auth_service.get_session_snapshot()["relogin_status"] == "success"


def test_api_read_discards_changed_account(monkeypatch):
    def read(_cookie):
        config.token = "new-generation"
        return {"courses": []}

    monkeypatch.setattr(graduate, "query_timetable", read)
    assert TestClient(app.app).get("/api/school/enrolled").status_code == 409


def test_captcha_fetch_preserves_image_cookie(monkeypatch):
    monkeypatch.setattr(graduate, "refresh_window", lambda: window())
    image = response(cookies={"route": "new", "image-session": "yes"})
    image._content = base64.b64decode(captcha_data().split(",", 1)[1])
    replies = iter([response({"code": 1, "data": {"token": "v"}}, cookies={"route": "old"}), image])
    monkeypatch.setattr(graduate, "_request", lambda *a, **k: next(replies))
    result = graduate.fetch_captcha()
    assert result["cookie"] == "route=new; image-session=yes"
    assert result["captcha_kind"] == "text"


def test_closed_school_has_no_captcha(monkeypatch):
    monkeypatch.setattr(graduate, "refresh_window", lambda: window(xksfkf=0))
    with pytest.raises(logic.CaptchaUnavailableError):
        graduate.fetch_captcha()


def test_malformed_login_contract_aborts_after_three(monkeypatch):
    calls = []
    monkeypatch.setattr(graduate.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        graduate, "fetch_captcha", lambda: {"vtoken": "v", "cookie": "sid=c", "imageUrl": "image"}
    )
    monkeypatch.setattr(graduate, "recognize_captcha", lambda _: "ABCD")

    def bad_login(*args):
        calls.append(args)
        raise graduate.GraduateResponseError("invalid login response")

    monkeypatch.setattr(graduate, "login", bad_login)
    with pytest.raises(graduate.GraduateResponseError):
        graduate.automatic_login(SID, "test", 50)
    assert len(calls) == 3


def test_timetable_failure_preserves_selected_rows(monkeypatch):
    monkeypatch.setattr(
        graduate,
        "query_selected",
        lambda _: [
            {
                "teachingClassID": CLASS_ID,
                "courseName": "示例课",
                "teachingPlace": "2-14周 星期一 3-4节 教学楼",
            }
        ],
    )
    monkeypatch.setattr(graduate, "_request", lambda *a, **k: response(status=503))
    result = graduate.query_timetable("sid=c")
    assert result["total_count"] == 1
    assert result["timetable"]["scheduled_count"] == 1
    assert result["timetable_warning"]


def test_catalog_cache_separates_filters_and_accounts(monkeypatch):
    calls = []

    def query(course_type, page, **kwargs):
        calls.append(kwargs)
        return graduate.CoursesResponse.from_dict(
            {
                "code": "1",
                "totalCount": 1,
                "dataList": [
                    {
                        "courseNumber": "CS1",
                        "courseName": kwargs["keyword"] or "unfiltered",
                        "tcList": [{"teachingClassID": CLASS_ID}],
                    }
                ],
            }
        )

    monkeypatch.setattr(graduate, "query_catalog", query)
    client = TestClient(app.app)
    assert client.get("/api/school/courses?type=GPN&keyword=alpha").status_code == 200
    assert client.get("/api/school/courses?type=GPN&keyword=beta").status_code == 200
    cached = client.get("/api/school/courses?type=GPN&keyword=alpha&cache_mode=true")
    assert cached.json()["courses"][0]["course_name"] == "alpha"
    assert len(calls) == 2
    assert client.get("/api/school/courses?type=GPN&cache_mode=true").status_code == 404
    monkeypatch.setattr(config, "student_id", "2600000002")
    assert (
        client.get("/api/school/courses?type=GPN&keyword=alpha&cache_mode=true").status_code == 404
    )


def test_expired_catalog_recovers_once(monkeypatch):
    calls = []

    def read(cookie):
        calls.append(cookie)
        if len(calls) == 1:
            raise graduate.GraduateSessionExpiredError()
        return {"courses": [], "total_count": 0}

    monkeypatch.setattr(graduate, "query_timetable", read)
    monkeypatch.setattr(app, "attempt_automatic_relogin", lambda *_: (True, ""))
    assert TestClient(app.app).get("/api/school/enrolled").status_code == 200
    assert len(calls) == 2


def test_scan_expiry_reaches_recovery(monkeypatch):
    from services.course_service import SESSION_EXPIRED

    monkeypatch.setattr(
        enroll_service, "query_courses", lambda *a: (False, SESSION_EXPIRED, "方案内课程")
    )
    with pytest.raises(choose_course.SchoolSessionExpiredError):
        enroll_service._scan_course_available(
            enroll_service.EnrollmentCourse(CLASS_ID, "GPN", "测试课")
        )


def test_manual_login_accepts_text_and_saves_cookie_session(monkeypatch):
    monkeypatch.setattr(app, "verify_card_key", lambda *a: True)
    monkeypatch.setattr(
        graduate, "login", lambda *a: {"success": True, "cookie": "sid=new", "token": "local-new"}
    )
    monkeypatch.setattr(logic, "fetch_elective_batch", lambda *a: window())
    result = TestClient(app.app).post(
        "/api/login",
        json={
            "student_id": SID,
            "password": "test",
            "card_key": "test-key",
            "vtoken": "v",
            "verifyCode": "AbCd",
            "cookie": "route=c",
        },
    )
    assert result.status_code == 200
    assert config.combined_cookie == "route=c; sid=new"
    assert config.token == "local-new"


def test_manual_login_rejects_undergraduate_points(monkeypatch):
    result = TestClient(app.app).post(
        "/api/login",
        json={
            "student_id": SID,
            "password": "test",
            "card_key": "test-key",
            "vtoken": "v",
            "verifyCode": [[1, 2]] * 4,
            "cookie": "route=c",
        },
    )
    assert result.status_code == 400


def test_undergraduate_database_override_is_not_reused(monkeypatch, tmp_path):
    monkeypatch.setenv("COURSE_SELECT_DB_PATH", str(tmp_path / "undergraduate.db"))
    assert database._default_db_path() != tmp_path / "undergraduate.db"


def test_graduate_full_capacity_keeps_pending_and_never_reports_success(monkeypatch):
    course = SimpleNamespace(id=CLASS_ID, type="GPN", name="测试课程", campus_code="")
    assert cart_service.add_course(course)["success"]
    cart_service.update_status(CLASS_ID, database.STATUS_IN_PROGRESS)
    monkeypatch.setattr(config, "count", 3)
    monkeypatch.setattr(config, "delay", 0)
    monkeypatch.setattr(
        graduate, "_request", lambda *a, **k: response({"code": 2, "msg": "课程容量已满"})
    )
    monkeypatch.setattr(enroll_service, "_wait_between_requests", lambda _: True)
    assert enroll_service.grab_courses([course]) == enroll_service.GrabOutcome.CONTINUE
    assert not cart_service.db.get_courses_by_status(database.STATUS_SUCCESS)
