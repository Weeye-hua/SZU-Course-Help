from __future__ import annotations

import pytest
import requests

import config
import logic
from school_password import encrypt_school_password
from services import auth_service


class DummyCaptchaResponse:
    def __init__(self, payload, *, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def test_login_parameter_validation_requires_exact_four_valid_points():
    valid = [[10, 30], [40, 40], [80, 50], [120, 60]]
    args = ("2024110122", "password", "SZU3.payload.signature")

    assert auth_service.validate_login_params(*args, valid, "token", "route=x") is None
    assert auth_service.validate_login_params(*args, valid[:3], "token", "route=x")
    assert auth_service.validate_login_params(*args, valid + [[1, 1]], "token", "route=x")
    assert auth_service.validate_login_params(*args, [[-1, 2], *valid[1:]], "token", "route=x")
    assert auth_service.validate_login_params("abc", "password", args[2], valid, "token", "route=x")


def test_cookie_parsing_handles_expires_commas():
    raw = (
        "route=abc; Path=/, insert_cookie=xyz; Expires=Wed, 21 Oct 2030 07:28:00 GMT; Path=/, "
        "JSESSIONID=session; Path=/, _WEU=weu-token; Path=/"
    )
    assert logic.parse_cookie(raw) == "route=abc; insert_cookie=xyz"
    assert logic.parse_login_cookie(raw) == "JSESSIONID=session; _WEU=weu-token"


def test_coordinate_serialization_rejects_invalid_values():
    serialize = logic.serialize_captcha_coordinates
    assert serialize([[1, 2], [3, 4], [5, 6], [7, 8]]) == "1-2,3-4,5-6,7-8"
    assert serialize([[1, 2]]) == ""
    assert serialize([[1, 2], [3, 4], [5, 6], [999, 8]]) == ""


def test_ocr_retry_is_bounded(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    monkeypatch.setattr(logic, "get_new_image", lambda **_: ("token", "route=a; insert_cookie=b"))
    monkeypatch.setattr(logic, "recognize_captcha_centers", lambda: [])
    monkeypatch.setattr(logic.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="连续 2 次"):
        logic.verify_vcode(max_attempts=2)


def test_ocr_relogin_default_attempt_limit_is_fifty():
    assert config.ocr_relogin_max_attempts == 50
    assert config.relogin_max_retries == 5
    assert config.relogin_retry_interval_seconds == 60
    assert logic.verify_vcode.__defaults__ == (50,)
    assert auth_service.attempt_ocr_relogin.__defaults__ == (50,)
    assert auth_service.attempt_automatic_relogin.__defaults__ == (50,)


def test_automatic_relogin_uses_login_page_ocr_flow(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "browser-secret")
    observed = []
    monkeypatch.setattr(
        logic,
        "verify_vcode_login_flow",
        lambda max_attempts=50: (
            observed.append(
                (max_attempts, config.student_id, config.password, config.backend_preference)
            )
            or ("vtoken", "route=a", "encrypted", "1-2,3-4,5-6,7-8")
        ),
    )

    result = auth_service.attempt_ocr_relogin(
        max_attempts=7,
        student_id="2024110122",
        password="browser-secret",
        backend="primary",
    )

    assert result == ("vtoken", "route=a", "encrypted", "1-2,3-4,5-6,7-8")
    assert observed == [(7, "2024110122", "browser-secret", config.BACKEND_PRIMARY)]


def test_school_password_protocol_matches_known_vectors():
    assert encrypt_school_password("school-password") == (
        "ODFBNjdGNENFMDkyOUNGMTI3OTkxOTFBRjU4NUI1M0RFNENCNDAwMTdCQjJBNkMwOTNDMjk4RjMxNzQyRjY2Nw=="
    )
    assert encrypt_school_password("P@ssw0rd!") == (
        "NkQ0MEQ5MUMwN0IwRjJFQTkxNkQzRUVFMzAwMERFNTg4MDlCQzU2QjU3Q0Y5QzMx"
    )


def test_ocr_retries_transient_exception_before_success(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    calls = []

    def fake_image(**_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary malformed image")
        return "token", "route=a; insert_cookie=b"

    monkeypatch.setattr(logic, "get_new_image", fake_image)
    monkeypatch.setattr(logic.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        logic,
        "recognize_captcha_centers",
        lambda: [[1, 2], [3, 4], [5, 6], [7, 8]],
    )

    result = logic.verify_vcode(max_attempts=2)
    assert len(calls) == 2
    assert result[0] == "token"
    assert result[3] == "1-2,3-4,5-6,7-8"


def test_batch_refresh_discards_result_from_replaced_session(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "token", "old-token")
    monkeypatch.setattr(config, "combined_cookie", "old-cookie")
    monkeypatch.setattr(config, "elective_batch_code", "")
    monkeypatch.setattr(config, "elective_batch_name", "")

    def replace_session(*_args):
        config.token = "new-token"
        config.combined_cookie = "new-cookie"
        return "stale-code", "复选阶段"

    monkeypatch.setattr(logic, "fetch_elective_batch", replace_session)

    with pytest.raises(RuntimeError, match="丢弃过期批次结果"):
        auth_service.refresh_elective_batch("2024110122", "old-token")

    assert config.elective_batch_code == ""
    assert config.elective_batch_name == ""


def test_captcha_fetch_retries_transient_failure(monkeypatch):
    calls = []

    def fake_fetch(**_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("empty image")
        return {"vtoken": "token", "cookie": "route=a", "imageUrl": "data:image/jpeg;base64,x"}

    monkeypatch.setattr(logic, "_fetch_vtoken_and_image_once", fake_fetch)
    monkeypatch.setattr(logic.time, "sleep", lambda *_: None)

    result = logic.fetch_vtoken_and_image(max_attempts=3)
    assert result["vtoken"] == "token"
    assert len(calls) == 2


def test_captcha_token_response_classifies_closed_window():
    response = DummyCaptchaResponse(
        {"code": "0", "msg": "当前非选课时间，验证码接口尚未开放", "data": {}},
    )

    with pytest.raises(logic.CaptchaUnavailableError):
        logic._parse_captcha_token_response(response)


def test_captcha_token_response_rejects_malformed_success_payload():
    response = DummyCaptchaResponse({"code": "1", "data": {"token": ""}})

    with pytest.raises(logic.CaptchaResponseError, match="missing or invalid"):
        logic._parse_captcha_token_response(response)


@pytest.mark.parametrize("existing_cookie", ["route=expired; JSESSIONID=stale", ""])
@pytest.mark.parametrize("fetcher", ["legacy", "current"])
def test_captcha_fetch_completely_omits_cookie_header(
    monkeypatch,
    tmp_path,
    existing_cookie,
    fetcher,
):
    monkeypatch.setattr(config, "combined_cookie", existing_cookie)
    monkeypatch.setattr(logic, "_captcha_image_path", lambda: tmp_path / "captcha.jpg")
    captured_headers = []

    token_response = DummyCaptchaResponse(
        {"data": {"token": "vtoken"}},
        text='{"data":{"token":"vtoken"}}',
    )
    image_response = DummyCaptchaResponse({})
    image_response.content = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    image_response.headers = {
        "Content-Type": "image/jpeg",
        "Set-Cookie": "route=fresh; Path=/, insert_cookie=node; Path=/",
    }

    def fake_post(**kwargs):
        captured_headers.append(dict(kwargs["headers"]))
        return token_response

    def fake_get(**kwargs):
        captured_headers.append(dict(kwargs["headers"]))
        return image_response

    monkeypatch.setattr(logic.requests, "post", fake_post)
    monkeypatch.setattr(logic.requests, "get", fake_get)

    if fetcher == "legacy":
        vtoken, cookie = logic.get_new_image()
        assert vtoken == "vtoken"
        assert logic.parse_cookie(cookie) == "route=fresh; insert_cookie=node"
    else:
        result = logic._fetch_vtoken_and_image_once()
        assert result["vtoken"] == "vtoken"
        assert result["imageUrl"].startswith("data:image/jpeg;base64,")

    assert len(captured_headers) == 2
    assert all("Cookie" not in headers for headers in captured_headers)


def test_captcha_unavailable_is_not_retried(monkeypatch):
    calls = []

    def unavailable(**_kwargs):
        calls.append(1)
        raise logic.CaptchaUnavailableError("closed")

    monkeypatch.setattr(logic, "_fetch_vtoken_and_image_once", unavailable)
    monkeypatch.setattr(logic.time, "sleep", lambda *_: None)

    with pytest.raises(logic.CaptchaUnavailableError):
        logic.fetch_vtoken_and_image(max_attempts=50)
    assert len(calls) == 1


def test_captcha_fetch_preserves_exhausted_transient_failure(monkeypatch):
    calls = []

    def malformed(**_kwargs):
        calls.append(1)
        raise logic.CaptchaResponseError("bad image")

    monkeypatch.setattr(logic, "_fetch_vtoken_and_image_once", malformed)
    monkeypatch.setattr(logic.time, "sleep", lambda *_: None)

    with pytest.raises(logic.CaptchaResponseError, match="bad image"):
        logic.fetch_vtoken_and_image(max_attempts=2)
    assert len(calls) == 2


def test_automatic_relogin_updates_runtime_state(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    monkeypatch.setattr(
        auth_service,
        "attempt_ocr_relogin",
        lambda max_attempts=config.ocr_relogin_max_attempts: (
            "vtoken",
            "route=a",
            "encrypted",
            "1-2,3-4,5-6,7-8",
        ),
    )
    monkeypatch.setattr(
        auth_service,
        "perform_school_login",
        lambda *args: {
            "success": True,
            "cookie": "JSESSIONID=s",
            "name": "Tester",
            "token": "new-token",
        },
    )
    monkeypatch.setattr(auth_service, "refresh_elective_batch", lambda *args: None)

    success, error = auth_service.attempt_automatic_relogin(max_attempts=1)
    assert success and error == ""
    assert config.combined_cookie == "JSESSIONID=s; route=a"
    snapshot = auth_service.get_session_snapshot()
    assert snapshot["relogin_status"] == "success"
    assert snapshot["relogin_in_progress"] is False
    assert snapshot["relogin_max_attempts"] == 1
    assert "password" not in snapshot


def test_automatic_relogin_accepts_login_page_backend_and_credentials(monkeypatch):
    monkeypatch.setattr(config, "student_id", "old-student")
    monkeypatch.setattr(config, "password", "old-secret")
    contexts = []

    def fake_ocr(max_attempts, *, student_id=None, password=None, backend=None):
        contexts.append((student_id, password, backend))
        assert config.student_id == "2024110122"
        assert config.password == "browser-secret"
        assert config.backend_preference == config.BACKEND_PRIMARY
        return "vtoken", "route=a", "encrypted", "1-2,3-4,5-6,7-8"

    monkeypatch.setattr(auth_service, "attempt_ocr_relogin", fake_ocr)
    monkeypatch.setattr(
        auth_service,
        "perform_school_login",
        lambda *_args: {
            "success": True,
            "cookie": "JSESSIONID=browser",
            "name": "Tester",
            "token": "browser-token",
        },
    )
    monkeypatch.setattr(auth_service, "refresh_elective_batch", lambda *_args: None)

    success, error = auth_service.attempt_automatic_relogin(
        max_attempts=1,
        student_id="2024110122",
        password="browser-secret",
        backend="primary",
    )

    assert success is True
    assert error == ""
    assert contexts == [(None, None, None)]


def test_failed_automatic_relogin_invalidates_school_session(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    monkeypatch.setattr(config, "token", "expired-token")
    monkeypatch.setattr(config, "combined_cookie", "expired-cookie")
    monkeypatch.setattr(
        auth_service,
        "attempt_ocr_relogin",
        lambda max_attempts=config.ocr_relogin_max_attempts: (_ for _ in ()).throw(
            RuntimeError("ocr failed")
        ),
    )

    success, error = auth_service.attempt_automatic_relogin(max_attempts=1)
    assert not success and "ocr failed" in error
    assert config.token == ""
    assert config.combined_cookie == ""
    assert config.student_id == "2024110122"
    assert config.password == "secret"
    snapshot = auth_service.get_session_snapshot()
    assert snapshot["relogin_status"] == "failed"
    assert snapshot["relogin_in_progress"] is False
    assert "ocr failed" in snapshot["relogin_message"]


def test_automatic_relogin_stops_after_five_failures(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    monkeypatch.setattr(config, "relogin_retry_interval_seconds", 0)
    calls = []

    def fail_ocr(max_attempts=config.ocr_relogin_max_attempts):
        calls.append(max_attempts)
        raise RuntimeError("ocr failed")

    monkeypatch.setattr(auth_service, "attempt_ocr_relogin", fail_ocr)

    for _ in range(config.relogin_max_retries):
        success, _ = auth_service.attempt_automatic_relogin(max_attempts=1)
        assert success is False

    success, error = auth_service.attempt_automatic_relogin(max_attempts=1)

    assert success is False
    assert "停止后台尝试" in error
    assert len(calls) == config.relogin_max_retries
    snapshot = auth_service.get_session_snapshot()
    assert snapshot["relogin_failure_count"] == config.relogin_max_retries
    assert snapshot["relogin_retry_after"] == 0


def test_automatic_relogin_respects_sixty_second_failure_cooldown(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    calls = []
    monkeypatch.setattr(
        auth_service,
        "attempt_ocr_relogin",
        lambda max_attempts=config.ocr_relogin_max_attempts: (
            calls.append(max_attempts),
            (_ for _ in ()).throw(RuntimeError("ocr failed")),
        )[1],
    )

    first_success, _ = auth_service.attempt_automatic_relogin(max_attempts=1)
    second_success, second_error = auth_service.attempt_automatic_relogin(max_attempts=1)

    assert first_success is False
    assert second_success is False
    assert "秒后重试" in second_error
    assert calls == [1]
    assert 0 < auth_service.get_session_snapshot()["relogin_retry_after"] <= 60


def test_automatic_relogin_exposes_running_state(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    observed = []

    def inspect_running_state(max_attempts=config.ocr_relogin_max_attempts):
        observed.append(auth_service.get_session_snapshot())
        raise RuntimeError("simulated OCR failure")

    monkeypatch.setattr(auth_service, "attempt_ocr_relogin", inspect_running_state)

    success, _ = auth_service.attempt_automatic_relogin(max_attempts=7)

    assert success is False
    assert observed[0]["relogin_in_progress"] is True
    assert observed[0]["relogin_status"] == "running"
    assert observed[0]["relogin_max_attempts"] == 7


def test_manual_login_wins_over_in_flight_automatic_relogin(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "old-secret")
    monkeypatch.setattr(config, "token", "expired-token")
    monkeypatch.setattr(config, "combined_cookie", "expired-cookie")
    monkeypatch.setattr(
        auth_service,
        "attempt_ocr_relogin",
        lambda max_attempts=config.ocr_relogin_max_attempts: (
            "vtoken",
            "route=automatic",
            "encrypted",
            "1-2,3-4,5-6,7-8",
        ),
    )

    def finish_manual_login(*_args):
        auth_service.save_login_state(
            "JSESSIONID=manual",
            "route=manual",
            "2024110122",
            "new-secret",
            "manual-token",
        )
        return {
            "success": True,
            "cookie": "JSESSIONID=automatic",
            "token": "automatic-token",
        }

    monkeypatch.setattr(auth_service, "perform_school_login", finish_manual_login)

    success, error = auth_service.attempt_automatic_relogin(max_attempts=1)

    assert success and error == ""
    assert config.token == "manual-token"
    assert config.combined_cookie == "JSESSIONID=manual; route=manual"
    assert config.password == "new-secret"
    assert auth_service.get_session_snapshot()["relogin_status"] == "success"


def test_batch_refresh_failure_keeps_restored_school_session(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    monkeypatch.setattr(
        auth_service,
        "attempt_ocr_relogin",
        lambda max_attempts=config.ocr_relogin_max_attempts: (
            "vtoken",
            "route=a",
            "encrypted",
            "1-2,3-4,5-6,7-8",
        ),
    )
    monkeypatch.setattr(
        auth_service,
        "perform_school_login",
        lambda *_args: {
            "success": True,
            "cookie": "JSESSIONID=restored",
            "token": "restored-token",
        },
    )
    monkeypatch.setattr(
        auth_service,
        "refresh_elective_batch",
        lambda *_args: (_ for _ in ()).throw(requests.Timeout("batch timeout")),
    )

    success, error = auth_service.attempt_automatic_relogin(max_attempts=1)

    assert success and error == ""
    assert config.token == "restored-token"
    assert config.combined_cookie == "JSESSIONID=restored; route=a"
    snapshot = auth_service.get_session_snapshot()
    assert snapshot["logged_in"] is True
    assert snapshot["relogin_status"] == "success"
    assert "批次暂未刷新" in snapshot["relogin_message"]


def test_login_state_remains_in_memory_and_writes_no_credential_file(monkeypatch, tmp_path):
    monkeypatch.setenv("COURSE_SELECT_DATA_DIR", str(tmp_path))
    auth_service.save_login_state(
        "JSESSIONID=school",
        "route=captcha",
        "2024110122",
        "secret",
        "token",
    )

    assert config.password == "secret"
    assert config.token == "token"
    assert config.combined_cookie == "JSESSIONID=school; route=captcha"
    assert list(tmp_path.glob("session_state*")) == []
    assert "password" not in auth_service.get_session_snapshot()


def test_clear_login_state_wipes_memory_without_deleting_unrelated_files(monkeypatch, tmp_path):
    monkeypatch.setenv("COURSE_SELECT_DATA_DIR", str(tmp_path))
    marker = tmp_path / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    monkeypatch.setattr(config, "token", "token")
    monkeypatch.setattr(config, "combined_cookie", "cookie")
    auth_service.clear_login_state()

    assert config.student_id == ""
    assert config.password == ""
    assert config.token == ""
    assert config.combined_cookie == ""
    assert marker.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.glob("session_state*")) == []


def test_structural_captcha_failure_terminates_login_page_ocr_loop_early(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    calls = []

    def contract_failure(*_args, **_kwargs):
        calls.append(1)
        raise logic.CaptchaResponseError("验证码图片响应缺少必要 Cookie")

    monkeypatch.setattr(logic, "fetch_vtoken_and_image", contract_failure)
    monkeypatch.setattr(logic.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="结构性异常"):
        logic.verify_vcode_login_flow(max_attempts=50)

    assert len(calls) == logic.STRUCTURAL_CAPTCHA_ABORT_AFTER


def test_structural_captcha_failure_terminates_terminal_ocr_loop_early(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    calls = []

    def contract_failure(**_kwargs):
        calls.append(1)
        raise logic.CaptchaResponseError("验证码图片响应缺少必要 Cookie")

    monkeypatch.setattr(logic, "get_new_image", contract_failure)
    monkeypatch.setattr(logic.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="结构性异常"):
        logic.verify_vcode(max_attempts=50)

    assert len(calls) == logic.STRUCTURAL_CAPTCHA_ABORT_AFTER


def test_non_structural_failure_resets_the_structural_captcha_counter(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    calls = []

    def flaky(**_kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("temporary network glitch")
        raise logic.CaptchaResponseError("验证码图片响应缺少必要 Cookie")

    monkeypatch.setattr(logic, "get_new_image", flaky)
    monkeypatch.setattr(logic.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="结构性异常"):
        logic.verify_vcode(max_attempts=50)

    assert len(calls) == 2 * logic.STRUCTURAL_CAPTCHA_ABORT_AFTER - 1


def test_unsolved_captcha_resets_the_structural_failure_streak(monkeypatch):
    monkeypatch.setattr(config, "student_id", "2024110122")
    monkeypatch.setattr(config, "password", "secret")
    calls = []

    def captcha_sequence(**_kwargs):
        calls.append(1)
        if len(calls) == 2:
            return "vtoken", "route=fresh; insert_cookie=node"
        raise logic.CaptchaResponseError("验证码图片响应缺少必要 Cookie")

    monkeypatch.setattr(logic, "get_new_image", captcha_sequence)
    monkeypatch.setattr(logic, "recognize_captcha_centers", lambda: [])
    monkeypatch.setattr(logic.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="结构性异常"):
        logic.verify_vcode(max_attempts=50)

    assert len(calls) == logic.STRUCTURAL_CAPTCHA_ABORT_AFTER + 2
