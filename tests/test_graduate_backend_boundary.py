"""Graduate startup and request isolation from the undergraduate WebVPN default."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from starlette.testclient import TestClient

import app
import config
from services import graduate_service, webvpn_auth_service
from study_program import GRADUATE, GRADUATE_BASE_URL, PROGRAM_ENV

GATEWAY_COOKIE = (
    "_webvpn_key=test-gateway-secret; webvpn_username=test-gateway-user; "
    "webvpn_username_NS_Sig=test-gateway-signature"
)


def test_fresh_graduate_process_uses_primary_login_defaults(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import requests

def blocked(*args, **kwargs):
    raise AssertionError('Graduate startup must not contact the school')

requests.sessions.Session.request = blocked

from starlette.testclient import TestClient
import app
import config
from study_program import GRADUATE_BASE_URL

assert config.DEFAULT_BACKEND == 'webvpn'
client = TestClient(app.app)
bootstrap = client.get('/api/bootstrap')
assert bootstrap.status_code == 200
payload = bootstrap.json()
assert payload['program'] == 'graduate'
assert payload['captcha_kind'] == 'text'
assert payload['supports_webvpn'] is False
assert payload['preference'] == payload['active_backend'] == 'primary'
assert payload['active_host'] == 'ehall.szu.edu.cn'
assert payload['webvpn_authenticated'] is False
assert payload['auto_fallback_active'] is False
assert payload['school_url'] == GRADUATE_BASE_URL + '*default/index.do'
request = app.LoginRequest(
    student_id='2600000001', password='test-password', card_key='test-key',
    vtoken='test-vtoken', verifyCode='AbC1', cookie='graduate_sid=test-session',
)
assert request.backend == 'primary'
assert request.verify_code == 'AbC1'
""",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        env={
            **os.environ,
            PROGRAM_ENV: GRADUATE,
            "COURSE_SELECT_DATA_DIR": str(tmp_path / "data"),
            "COURSE_SELECT_KEY_DIR": str(tmp_path / "keys"),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("gateway_cookie", ["", GATEWAY_COOKIE])
@pytest.mark.parametrize(
    ("endpoint", "body"),
    [
        ("/api/backend/select", {"backend": "webvpn"}),
        ("/api/webvpn/auth/start", None),
        (
            "/api/login",
            {
                "student_id": "2600000001",
                "password": "test-password",
                "card_key": "test-key",
                "vtoken": "test-vtoken",
                "verifyCode": "AbC1",
                "cookie": "graduate_sid=test-session",
                "backend": "webvpn",
            },
        ),
    ],
)
def test_graduate_rejects_webvpn_before_any_external_action(
    monkeypatch, gateway_cookie, endpoint, body
):
    monkeypatch.setenv(PROGRAM_ENV, GRADUATE)
    monkeypatch.setattr(config, "backend_preference", config.BACKEND_WEBVPN)
    monkeypatch.setattr(config, "active_backend", config.BACKEND_WEBVPN)
    monkeypatch.setattr(config, "webvpn_cookie", gateway_cookie)
    monkeypatch.setattr(app, "is_enroll_task_running", lambda: False)
    network = Mock(side_effect=AssertionError("No school or gateway HTTP request is allowed"))
    start_auth = Mock(side_effect=AssertionError("No gateway browser may be launched"))
    monkeypatch.setattr(requests.sessions.Session, "request", network)
    monkeypatch.setattr(webvpn_auth_service, "start_auth", start_auth)

    result = TestClient(app.app).post(endpoint, json=body)

    assert result.status_code == 400
    assert result.json()["error_code"] == "UNSUPPORTED_BACKEND"
    assert result.json()["retryable"] is False
    network.assert_not_called()
    start_auth.assert_not_called()


@pytest.mark.parametrize("graduate_cookie", ["", "graduate_sid=test-graduate-session"])
def test_graduate_request_ignores_undergraduate_gateway_and_credentials(
    monkeypatch, graduate_cookie
):
    monkeypatch.setenv(PROGRAM_ENV, GRADUATE)
    monkeypatch.setattr(config, "backend_preference", config.DEFAULT_BACKEND)
    monkeypatch.setattr(config, "active_backend", config.BACKEND_WEBVPN)
    monkeypatch.setattr(config, "webvpn_cookie", GATEWAY_COOKIE)
    monkeypatch.setattr(config, "authserver_cookie", "CASTGC=test-cas-secret")
    monkeypatch.setattr(config, "combined_cookie", "JSESSIONID=test-undergraduate-secret")
    monkeypatch.setattr(config, "token", "test-undergraduate-token")
    response = requests.Response()
    response.status_code = 200
    response._content = b"{}"
    network = Mock(return_value=response)
    monkeypatch.setattr(requests.sessions.Session, "request", network)

    graduate_service._request("GET", "xsxkHome/loadStdInfo.do", cookie=graduate_cookie)

    network.assert_called_once()
    sent = network.call_args.kwargs
    assert sent["method"] == "GET"
    assert sent["url"] == GRADUATE_BASE_URL + "xsxkHome/loadStdInfo.do"
    assert sent["allow_redirects"] is False
    assert sent["headers"]["Origin"] == "https://ehall.szu.edu.cn"
    assert sent["headers"]["Referer"] == GRADUATE_BASE_URL + "*default/index.do"
    if graduate_cookie:
        assert sent["headers"]["Cookie"] == graduate_cookie
    else:
        assert "Cookie" not in sent["headers"]
    assert "token" not in {name.lower() for name in sent["headers"]}
    for secret in (
        "test-gateway-secret",
        "test-gateway-user",
        "test-gateway-signature",
        "test-cas-secret",
        "test-undergraduate-secret",
        "test-undergraduate-token",
    ):
        assert secret not in repr(network.call_args)
