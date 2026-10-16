"""A fresh off-campus launch must require WebVPN authentication, not use primary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import config
from services import auth_service, backend_service
from study_program import GRADUATE, PROGRAM_ENV, UNDERGRADUATE


def test_fresh_process_defaults_to_webvpn_without_sending_school_requests(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import requests

def blocked(*args, **kwargs):
    raise AssertionError('Fresh startup must not contact the school before gateway authentication')

requests.sessions.Session.request = blocked

from starlette.testclient import TestClient
import app
import config

client = TestClient(app.app)
bootstrap = client.get('/api/bootstrap').json()
assert bootstrap['preference'] == 'webvpn'
assert bootstrap['active_host'] == 'bkxk.webvpn.szu.edu.cn'
assert bootstrap['webvpn_authenticated'] is False
assert bootstrap['school_url'].startswith('https://bkxk.webvpn.szu.edu.cn/')
captcha = client.get('/api/captcha')
assert captcha.status_code == 409
assert captcha.json()['error_code'] == 'WEBVPN_AUTH_REQUIRED'
login = client.post('/api/login', json={
    'student_id': '2024110122', 'password': 'test-password', 'card_key': 'test-key',
    'vtoken': 'test', 'verifyCode': [[1, 2], [3, 4], [5, 6], [7, 8]],
    'cookie': 'route=test; insert_cookie=test',
})
assert login.status_code == 409
assert login.json()['error_code'] == 'WEBVPN_AUTH_REQUIRED'
assert config.backend_preference == 'webvpn'
""",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        env={
            **os.environ,
            PROGRAM_ENV: UNDERGRADUATE,
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


@pytest.mark.parametrize(
    ("program", "expected"),
    [(UNDERGRADUATE, config.BACKEND_WEBVPN), (GRADUATE, config.BACKEND_PRIMARY)],
)
def test_logout_restores_the_program_default(monkeypatch, program, expected):
    monkeypatch.setenv(PROGRAM_ENV, program)
    for name in (
        "combined_cookie",
        "webvpn_cookie",
        "authserver_cookie",
        "token",
        "student_id",
        "password",
        "elective_batch_code",
        "elective_batch_name",
        "campus_code",
        "campus_name",
    ):
        monkeypatch.setattr(config, name, getattr(config, name))
    monkeypatch.setattr(auth_service, "_session_generation", auth_service._session_generation)

    auth_service.clear_login_state()

    assert config.backend_preference == expected
    assert config.active_backend == expected
    assert backend_service.backend_payload()["preference"] == expected
