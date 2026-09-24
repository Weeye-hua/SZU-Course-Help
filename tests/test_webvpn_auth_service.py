from __future__ import annotations

import os

import pytest

import config
from services import webvpn_auth_service


def test_expired_gateway_status_does_not_remain_authenticated(monkeypatch):
    monkeypatch.setattr(config, "webvpn_cookie", "")
    manager = webvpn_auth_service.ControlledBrowserManager()
    manager._state = "authenticated"
    status = manager.status()
    assert status["state"] == "expired"
    assert status["authenticated"] is False


def test_expired_or_lookalike_domain_cookies_are_not_imported():
    valid = [
        {"name": name, "value": "valid", "domain": ".webvpn.szu.edu.cn"}
        for name in webvpn_auth_service.backend_service.WEBVPN_COOKIE_NAMES
    ]
    invalid = [
        {"name": "_webvpn_key", "value": "wrong", "domain": "evilszu.edu.cn"},
        {"name": "_webvpn_key", "value": "expired", "domain": "webvpn.szu.edu.cn", "expires": 1},
    ]
    assert "_webvpn_key=valid" in webvpn_auth_service.extract_webvpn_cookie_header(valid + invalid)


def test_build_auth_url_uses_real_authserver_and_webvpn_callback():
    url = webvpn_auth_service.build_auth_url()

    assert url.startswith("https://authserver-443.webvpn.szu.edu.cn/authserver/login?")
    assert "webvpn.szu.edu.cn%2Fusers%2Fauth%2Fcas%2Fcallback" in url
    assert "bkxk.webvpn.szu.edu.cn" in url


def test_extract_webvpn_cookie_header_filters_names_and_domains():
    cookies = [
        {"name": "_webvpn_key", "value": "key", "domain": ".webvpn.szu.edu.cn"},
        {"name": "webvpn_username", "value": "user", "domain": "bkxk.webvpn.szu.edu.cn"},
        {
            "name": "webvpn_username_NS_Sig",
            "value": "signature",
            "domain": "webvpn.szu.edu.cn",
        },
        {"name": "JSESSIONID", "value": "wrong-host", "domain": "evil.example"},
        {"name": "_webvpn_key", "value": "wrong-host", "domain": "evil.example"},
    ]

    assert webvpn_auth_service.extract_webvpn_cookie_header(cookies) == (
        "_webvpn_key=key; webvpn_username=user; webvpn_username_NS_Sig=signature"
    )


def test_extract_webvpn_cookie_header_requires_all_three_cookies():
    assert (
        webvpn_auth_service.extract_webvpn_cookie_header(
            [{"name": "_webvpn_key", "value": "key", "domain": "webvpn.szu.edu.cn"}]
        )
        == ""
    )


def test_known_browser_paths_cover_macos_and_windows_install_locations():
    macos = webvpn_auth_service._known_browser_paths("darwin", {})
    windows = webvpn_auth_service._known_browser_paths(
        "win32",
        {"PROGRAMFILES": "C:/Program Files", "LOCALAPPDATA": "C:/Users/test/AppData/Local"},
    )

    assert any("Google Chrome.app" in path for path in macos)
    assert "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" in windows
    assert any("Microsoft\\Edge\\Application\\msedge.exe" in path for path in windows)


def test_find_browser_uses_known_absolute_path_when_not_on_path(monkeypatch, tmp_path):
    executable = tmp_path / "chrome"
    executable.write_text("browser", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.delenv("COURSE_SELECT_BROWSER", raising=False)
    monkeypatch.setattr(webvpn_auth_service.shutil, "which", lambda _candidate: None)
    monkeypatch.setattr(
        webvpn_auth_service,
        "_known_browser_paths",
        lambda: (str(executable),),
    )

    assert webvpn_auth_service.find_browser() == str(executable)


def _prepare_fake_browser(monkeypatch, tmp_path):
    profile = tmp_path / "ephemeral-webvpn-profile"

    def fake_mkdtemp(*, prefix):
        assert prefix == "szu-course-webvpn-"
        profile.mkdir()
        return str(profile)

    class FakeProcess:
        def __init__(self):
            self.dead = False

        def poll(self):
            return 0 if self.dead else None

        def terminate(self):
            self.dead = True

        def wait(self, timeout):
            return None

        def kill(self):
            self.dead = True

    process = FakeProcess()
    monkeypatch.setattr(webvpn_auth_service.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(webvpn_auth_service, "find_browser", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(webvpn_auth_service, "_free_local_port", lambda: 43123)
    monkeypatch.setattr(webvpn_auth_service.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(webvpn_auth_service.backend_service, "has_webvpn_cookies", lambda: False)
    return profile, process


def test_controlled_browser_profile_is_ephemeral(monkeypatch, tmp_path):
    manager = webvpn_auth_service.ControlledBrowserManager()
    profile, process = _prepare_fake_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(manager, "_start_watcher", lambda: None)

    status = manager.start()

    assert status["state"] == "starting"
    assert profile.is_dir()
    args = manager._browser_args("/usr/bin/chromium", "https://example.invalid")
    assert f"--user-data-dir={profile}" in args
    assert "--disable-background-mode" in args
    manager.stop_browser()
    assert process.dead is True
    assert not profile.exists()


def test_browser_launch_failure_erases_ephemeral_profile(monkeypatch, tmp_path):
    manager = webvpn_auth_service.ControlledBrowserManager()
    profile, _process = _prepare_fake_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(
        webvpn_auth_service.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("launch failed")),
    )

    with pytest.raises(webvpn_auth_service.ControlledBrowserUnavailableError):
        manager.start()

    assert not profile.exists()
    assert manager.status()["state"] == "error"
    assert os.path.basename(str(profile)) not in manager.status()["message"]
