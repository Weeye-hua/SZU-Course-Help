"""Expired gateway sessions must return to authentication without replaying writes."""

from types import SimpleNamespace

import pytest
import requests

import config
from services import backend_service

SCHOOL_URL = "https://bkxk.webvpn.szu.edu.cn/xsxkapp/sys/xsxkapp/elective/volunteer.do"
AUTH_URL = "https://authserver-443.webvpn.szu.edu.cn/authserver/login?service=example"
GATEWAY_COOKIE = "_webvpn_key=key; webvpn_username=user; webvpn_username_NS_Sig=sig"


@pytest.fixture(autouse=True)
def gateway_session(monkeypatch):
    monkeypatch.setattr(config, "webvpn_cookie", GATEWAY_COOKIE)
    monkeypatch.setattr(config, "combined_cookie", "route=school; JSESSIONID=school")
    monkeypatch.setattr(config, "token", "school-token")
    monkeypatch.setattr(config, "backend_preference", config.BACKEND_WEBVPN)
    monkeypatch.setattr(config, "active_backend", config.BACKEND_PRIMARY)
    backend_service.clear_primary_cooldown()
    yield
    backend_service.clear_primary_cooldown()


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(status_code=200, url=AUTH_URL),
        SimpleNamespace(status_code=302, headers={"Location": AUTH_URL}),
        SimpleNamespace(status_code=307, url=SCHOOL_URL, headers={"Location": "/users/auth/cas"}),
        SimpleNamespace(status_code=302, headers={"location": "//webvpn.szu.edu.cn/users/sign_in"}),
        SimpleNamespace(
            status_code=200,
            url=SCHOOL_URL,
            history=[
                SimpleNamespace(
                    status_code=302,
                    url="https://webvpn.szu.edu.cn/users/expired",
                    headers={"Location": "auth/cas"},
                )
            ],
        ),
        SimpleNamespace(
            status_code=200,
            url=SCHOOL_URL,
            history=[SimpleNamespace(status_code=200, url=AUTH_URL)],
        ),
        SimpleNamespace(
            status_code=302,
            url=SCHOOL_URL,
            headers={"Location": "https://webvpn.szu.edu.cn/users/auth/cas/callback?url=example"},
        ),
    ],
)
def test_authentication_redirect_clears_gateway_cookie_without_replaying_write(response):
    calls = []

    def sender(**kwargs):
        calls.append(kwargs)
        return response

    with pytest.raises(backend_service.WebVPNAuthenticationRequiredError, match="认证已过期"):
        backend_service.request_with_failover(
            "POST", "elective/volunteer.do", sender=sender, data={"addParam": "selection"}
        )

    assert len(calls) == 1
    assert calls[0]["url"] == SCHOOL_URL
    assert config.webvpn_cookie == ""
    assert not backend_service.has_webvpn_cookies()
    assert config.combined_cookie == "route=school; JSESSIONID=school"
    assert config.token == "school-token"
    assert config.active_backend == config.BACKEND_PRIMARY


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(status_code=200),
        SimpleNamespace(status_code=200, url=None, history=None, headers=None),
        SimpleNamespace(status_code=200, url=SCHOOL_URL, text=f'{{"help":"{AUTH_URL}"}}'),
        SimpleNamespace(status_code=200, url=SCHOOL_URL, headers={"Location": AUTH_URL}),
        SimpleNamespace(
            status_code=302,
            url=SCHOOL_URL,
            headers={"Location": "/xsxkapp/sys/xsxkapp/*default/index.do"},
        ),
        SimpleNamespace(
            status_code=302,
            headers={"Location": "https://webvpn.szu.edu.cn.example.org/users/sign_in"},
        ),
        SimpleNamespace(
            status_code=302,
            headers={"Location": "https://webvpn.szu.edu.cn/help?next=/users/sign_in"},
        ),
        SimpleNamespace(status_code=200, url="https://authserver-443.webvpn.szu.edu.cn/help"),
    ],
)
def test_business_responses_do_not_invalidate_gateway_session(response):
    if 300 <= response.status_code < 400:
        with pytest.raises(requests.HTTPError) as caught:
            backend_service.request_with_failover(
                "POST", "elective/volunteer.do", sender=lambda **kwargs: response
            )
        assert caught.value.response is response
        assert config.webvpn_cookie == GATEWAY_COOKIE
        assert config.active_backend == config.BACKEND_PRIMARY
        return
    actual = backend_service.request_with_failover(
        "POST", "elective/volunteer.do", sender=lambda **kwargs: response
    )

    assert actual is response
    assert config.webvpn_cookie == GATEWAY_COOKIE
    assert config.active_backend == config.BACKEND_WEBVPN


def test_expired_gateway_read_does_not_fall_back_to_primary(monkeypatch):
    monkeypatch.setattr(config, "backend_preference", config.BACKEND_AUTO)
    backend_service.mark_primary_failure()
    calls = []

    def sender(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, url=AUTH_URL)

    with pytest.raises(backend_service.WebVPNAuthenticationRequiredError):
        backend_service.request_with_failover(
            "POST", "elective/courseResult.do", sender=sender, read_only=True
        )

    assert len(calls) == 1
    assert calls[0]["url"].startswith("https://bkxk.webvpn.szu.edu.cn/")


def test_primary_response_does_not_clear_gateway_cookie(monkeypatch):
    monkeypatch.setattr(config, "backend_preference", config.BACKEND_PRIMARY)
    response = SimpleNamespace(status_code=302, headers={"Location": AUTH_URL})

    actual = backend_service.request_with_failover(
        "POST", "student/check/login.do", sender=lambda **kwargs: response
    )

    assert actual is response
    assert config.webvpn_cookie == GATEWAY_COOKIE


@pytest.mark.parametrize("redirect_status", [302, 307, 308])
@pytest.mark.parametrize("path", ["student/check/login.do", "elective/volunteer.do"])
def test_requests_transport_never_follows_gateway_auth_redirects(path, redirect_status):
    """Exercise requests' real redirect machinery with an entirely fake adapter."""
    calls = []

    class GatewayAdapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            calls.append(request)
            response = requests.Response()
            response.request = request
            response.url = request.url
            response.status_code = redirect_status if len(calls) == 1 else 200
            response._content = b""
            if len(calls) == 1:
                response.headers["Location"] = AUTH_URL
            return response

        def close(self):
            pass

    with requests.Session() as session:
        # Keep conftest's Session.request network guard intact. Session.send
        # exercises redirect handling, and both schemes use the fake adapter.
        adapter = GatewayAdapter()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.trust_env = False

        def sender(**kwargs):
            request = requests.Request(
                method=kwargs["method"],
                url=kwargs["url"],
                headers=kwargs["headers"],
                data=kwargs["data"],
                params=kwargs["params"],
            )
            return session.send(
                session.prepare_request(request),
                timeout=kwargs["timeout"],
                allow_redirects=kwargs.get("allow_redirects", True),
            )

        with pytest.raises(backend_service.WebVPNAuthenticationRequiredError):
            backend_service.request_with_failover(
                "POST", path, sender=sender, data={"test": "fake-body"}, token="fake-token"
            )

    assert len(calls) == 1, "Authentication redirects must not replay school requests"
    assert calls[0].method == "POST"
    assert calls[0].body == "test=fake-body"
    assert calls[0].headers["token"] == "fake-token"
    assert config.webvpn_cookie == ""


@pytest.mark.parametrize("preference", [config.BACKEND_PRIMARY, config.BACKEND_WEBVPN])
def test_redirect_policy_preserves_primary_sender_contract(monkeypatch, preference):
    monkeypatch.setattr(config, "backend_preference", preference)
    captured = {}

    def sender(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status_code=200)

    backend_service.request_with_failover(
        "GET", "elective/status.do", sender=sender, read_only=True
    )

    if preference == config.BACKEND_WEBVPN:
        assert captured["allow_redirects"] is False
    else:
        assert "allow_redirects" not in captured
