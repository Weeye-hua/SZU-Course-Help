"""Shared school backend profiles, headers, cookies, and failover policy."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

PRIMARY_HOST = "bkxk.szu.edu.cn"
WEBVPN_HOST = "bkxk.webvpn.szu.edu.cn"
AUTHSERVER_HOST = "authserver-443.webvpn.szu.edu.cn"
WEBVPN_ROOT_HOST = "webvpn.szu.edu.cn"

WEBVPN_COOKIE_NAMES = (
    "_webvpn_key",
    "webvpn_username",
    "webvpn_username_NS_Sig",
)


@dataclass(frozen=True, slots=True)
class BackendProfile:
    key: str
    label: str
    scheme: str
    host: str
    base_url: str
    entry_path: str = "/xsxkapp/sys/xsxkapp/*default/index.do"

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}"

    @property
    def referer(self) -> str:
        return f"{self.origin}{self.entry_path}"


class WebVPNAuthenticationRequiredError(RuntimeError):
    """Raised when a WebVPN request has no authenticated WebVPN cookie set."""


BACKENDS: dict[str, BackendProfile] = {
    config.BACKEND_PRIMARY: BackendProfile(
        key=config.BACKEND_PRIMARY,
        label="主站",
        scheme="http",
        host=PRIMARY_HOST,
        base_url=f"http://{PRIMARY_HOST}/xsxkapp/sys/xsxkapp/",
    ),
    config.BACKEND_WEBVPN: BackendProfile(
        key=config.BACKEND_WEBVPN,
        label="WebVPN 备用",
        scheme="https",
        host=WEBVPN_HOST,
        base_url=f"https://{WEBVPN_HOST}/xsxkapp/sys/xsxkapp/",
    ),
}

TRANSIENT_STATUS_CODES = frozenset({502, 503, 504})
PRIMARY_COOLDOWN_SECONDS = 5 * 60
_primary_cooldown_until = 0.0

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 "
    "Safari/537.36 Edg/139.0.0.0"
)


def normalize_preference(value: str | None) -> str:
    value = str(value or config.BACKEND_AUTO).strip().lower()
    return value if value in BACKENDS or value == config.BACKEND_AUTO else config.BACKEND_AUTO


def set_preference(value: str | None) -> str:
    normalized = normalize_preference(value)
    config.backend_preference = normalized
    return normalized


def get_preference() -> str:
    return normalize_preference(getattr(config, "backend_preference", config.BACKEND_AUTO))


def get_profile(key: str) -> BackendProfile:
    return BACKENDS[normalize_preference(key)]


def candidate_profiles(
    preference: str | None = None, *, allow_failover: bool = False
) -> list[BackendProfile]:
    """Return eligible profiles without ever dropping the only usable primary."""
    normalized = normalize_preference(preference or get_preference())
    if normalized == config.BACKEND_AUTO:
        primary = BACKENDS[config.BACKEND_PRIMARY]
        if not allow_failover or not has_webvpn_cookies():
            return [primary]
        webvpn = BACKENDS[config.BACKEND_WEBVPN]
        return [webvpn, primary] if primary_cooldown_active() else [primary, webvpn]
    return [BACKENDS[normalized]]


def primary_cooldown_active() -> bool:
    return time.monotonic() < _primary_cooldown_until


def primary_cooldown_remaining() -> float:
    return round(max(0.0, _primary_cooldown_until - time.monotonic()), 3)


def mark_primary_failure() -> None:
    global _primary_cooldown_until
    _primary_cooldown_until = time.monotonic() + PRIMARY_COOLDOWN_SECONDS


def clear_primary_cooldown() -> None:
    global _primary_cooldown_until
    _primary_cooldown_until = 0.0


def active_profile() -> BackendProfile:
    return BACKENDS.get(
        getattr(config, "active_backend", config.BACKEND_PRIMARY),
        BACKENDS[config.BACKEND_PRIMARY],
    )


def cookie_header(profile: BackendProfile, *, authserver: bool = False) -> str:
    if authserver:
        return str(getattr(config, "authserver_cookie", "") or "")
    values = [str(getattr(config, "combined_cookie", "") or "").strip()]
    if profile.key == config.BACKEND_WEBVPN:
        values.append(str(getattr(config, "webvpn_cookie", "") or "").strip())
    return "; ".join(value for value in values if value)


def _cookie_for_profile(profile: BackendProfile, supplied: str | None) -> str:
    if supplied is None:
        return cookie_header(profile)
    values = [str(supplied).strip()]
    if profile.key == config.BACKEND_WEBVPN:
        webvpn = str(getattr(config, "webvpn_cookie", "") or "").strip()
        if webvpn and webvpn not in values[0]:
            values.append(webvpn)
    return "; ".join(value for value in values if value)


def _gateway_cookie_for_omit(profile: BackendProfile) -> str:
    """Cookie kept when a caller omits the school session cookie."""
    if profile.key == config.BACKEND_WEBVPN:
        return str(getattr(config, "webvpn_cookie", "") or "").strip()
    return ""


def rewrite_referer_for_profile(referer: str | None, profile: BackendProfile) -> str | None:
    if not referer:
        return None
    value = str(referer)
    for host in (PRIMARY_HOST, WEBVPN_HOST):
        value = value.replace(f"http://{host}", profile.origin)
        value = value.replace(f"https://{host}", profile.origin)
    return value


def build_headers(
    profile: BackendProfile,
    *,
    token: str = "",
    content_type: str | None = None,
    accept: str = "*/*",
    cookie: str | None = None,
    omit_cookie: bool = False,
    referer: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    headers = {
        "Accept": accept,
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,zh-TW;q=0.5",
        "Host": profile.host,
        "Origin": profile.origin,
        "Referer": referer or profile.referer,
        "User-Agent": _UA,
        "X-Requested-With": "XMLHttpRequest",
    }
    if not omit_cookie:
        headers["Cookie"] = cookie if cookie is not None else cookie_header(profile)
    if content_type:
        headers["Content-Type"] = content_type
    if token:
        headers["token"] = token
    if extra:
        for name, value in extra.items():
            lowered = name.lower()
            if lowered in {"host", "origin", "referer", "cookie", "user-agent"}:
                continue
            headers[name] = value
    return headers


def mark_success(profile: BackendProfile) -> None:
    config.active_backend = profile.key
    if profile.key == config.BACKEND_PRIMARY:
        clear_primary_cooldown()


def mark_failure(profile: BackendProfile) -> None:
    if profile.key == config.BACKEND_PRIMARY and has_webvpn_cookies():
        mark_primary_failure()


def should_fail_over(response: Any) -> bool:
    return int(getattr(response, "status_code", 0) or 0) in TRANSIENT_STATUS_CODES


def request_with_failover(
    method: str,
    path: str,
    *,
    sender: Callable[..., Any] = requests.request,
    timeout: Any = None,
    data: Any = None,
    params: Any = None,
    token: str = "",
    content_type: str | None = None,
    accept: str = "*/*",
    cookie: str | None = None,
    omit_cookie: bool = False,
    referer: str | None = None,
    extra_headers: dict[str, str] | None = None,
    preference: str | None = None,
    read_only: bool = False,
) -> Any:
    """Send one request; cross-backend retry is opt-in and read-only only.

    School query endpoints sometimes use HTTP POST, so safety is expressed by
    the caller's ``read_only`` contract rather than by the verb alone. Mutating
    enrollment and withdrawal callers never set this flag.
    """
    profiles = candidate_profiles(preference, allow_failover=read_only)
    last_error: Exception | None = None
    normalized_path = str(path).lstrip("/")
    for index, profile in enumerate(profiles):
        if profile.key == config.BACKEND_WEBVPN and not has_webvpn_cookies():
            raise WebVPNAuthenticationRequiredError("WebVPN authentication is required")
        try:
            # A school-cookie omission must not drop the WebVPN gateway cookie:
            # it authenticates the gateway itself, not the school session
            # (issue #9). On the primary profile the header is dropped whole.
            gateway_cookie = _gateway_cookie_for_omit(profile) if omit_cookie else ""
            response = sender(
                url=f"{profile.base_url}{normalized_path}",
                method=method.upper(),
                data=data,
                params=params,
                headers=build_headers(
                    profile,
                    token=token,
                    content_type=content_type,
                    accept=accept,
                    cookie=gateway_cookie
                    or (None if omit_cookie else _cookie_for_profile(profile, cookie)),
                    omit_cookie=omit_cookie and not gateway_cookie,
                    referer=rewrite_referer_for_profile(referer, profile),
                    extra=extra_headers,
                ),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_error = exc
            mark_failure(profile)
            if read_only and index + 1 < len(profiles):
                logger.warning("Backend %s unavailable; trying fallback", profile.label)
                continue
            raise
        transient_failure = should_fail_over(response)
        if transient_failure:
            mark_failure(profile)
        if read_only and transient_failure and index + 1 < len(profiles):
            logger.warning(
                "Backend %s returned %s; trying fallback", profile.label, response.status_code
            )
            continue
        if not transient_failure:
            mark_success(profile)
        return response
    if last_error:
        raise last_error
    raise RuntimeError("no school backend available")


def backend_payload() -> dict[str, Any]:
    from study_program import is_graduate

    if is_graduate():
        return {
            "preference": "primary",
            "preference_label": "研究生选课系统",
            "active_backend": "primary",
            "active_backend_label": "研究生主站",
            "active_host": "ehall.szu.edu.cn",
            "webvpn_authenticated": False,
            "auto_fallback_active": False,
            "primary_cooldown_remaining": 0,
            "primary_cooldown_until": None,
        }
    profile = active_profile()
    preference = get_preference()
    remaining = primary_cooldown_remaining()
    return {
        "preference": preference,
        "preference_label": "自动（优先主站）"
        if preference == config.BACKEND_AUTO
        else get_profile(preference).label,
        "active_backend": profile.key,
        "active_backend_label": profile.label,
        "active_host": profile.host,
        "webvpn_authenticated": has_webvpn_cookies(),
        "auto_fallback_active": preference == config.BACKEND_AUTO and remaining > 0,
        "primary_cooldown_remaining": remaining,
        "primary_cooldown_until": round(time.time() + remaining, 3) if remaining else None,
    }


def has_webvpn_cookies() -> bool:
    cookies = parse_cookie_pairs(getattr(config, "webvpn_cookie", ""))
    return all(name in cookies and cookies[name] for name in WEBVPN_COOKIE_NAMES)


def parse_cookie_pairs(value: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for segment in str(value or "").split(";"):
        name, separator, cookie_value = segment.strip().partition("=")
        if separator and name and cookie_value:
            pairs[name.strip()] = cookie_value.strip()
    return pairs


def combine_cookie_values(existing: str, additions: str, allowed_names: tuple[str, ...]) -> str:
    values = parse_cookie_pairs(existing)
    allowed = set(allowed_names)
    for name, value in parse_cookie_pairs(additions).items():
        if name in allowed:
            values[name] = value
    return "; ".join(f"{name}={value}" for name, value in values.items())


def combine_cookie_values_excluding(
    existing: str,
    additions: str,
    excluded_names: tuple[str, ...],
) -> str:
    """Merge every cookie except the explicitly isolated cookie names."""
    values = parse_cookie_pairs(existing)
    excluded = set(excluded_names)
    for name, value in parse_cookie_pairs(additions).items():
        if name not in excluded:
            values[name] = value
    return "; ".join(f"{name}={value}" for name, value in values.items())


def merge_set_cookie(header_values: list[str], host: str) -> bool:
    """Merge only cookies belonging to the upstream host into runtime state."""
    additions = "; ".join(
        f"{name}={value}"
        for header in header_values
        for name, value in _iter_set_cookie_pairs(header)
    )
    if not additions:
        return False
    if host in (WEBVPN_HOST, WEBVPN_ROOT_HOST):
        changed = False
        school_additions = combine_cookie_values_excluding("", additions, WEBVPN_COOKIE_NAMES)
        if school_additions:
            config.combined_cookie = combine_cookie_values_excluding(
                getattr(config, "combined_cookie", ""),
                school_additions,
                WEBVPN_COOKIE_NAMES,
            )
            changed = True
        webvpn_additions = combine_cookie_values("", additions, WEBVPN_COOKIE_NAMES)
        if webvpn_additions:
            config.webvpn_cookie = combine_cookie_values(
                getattr(config, "webvpn_cookie", ""),
                webvpn_additions,
                WEBVPN_COOKIE_NAMES,
            )
            changed = True
        return changed
    if host == AUTHSERVER_HOST:
        config.authserver_cookie = combine_cookie_values(
            getattr(config, "authserver_cookie", ""),
            additions,
            (
                "route",
                "insert_cookie",
                "JSESSIONID",
                "CASTGC",
                "session",
                "rememberMe",
            ),
        )
        return True
    return False


def _iter_set_cookie_pairs(header: str):
    import re

    # A response may contain cookies not known to this client yet. Parse the
    # first name/value pair of every Set-Cookie segment, while excluding
    # attributes such as Path/Domain/SameSite that also use ``name=value``.
    attributes = {
        "Path",
        "Domain",
        "Expires",
        "Max-Age",
        "SameSite",
        "Secure",
        "HttpOnly",
        "Priority",
        "Partitioned",
    }
    pattern = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
    for match in re.finditer(rf"(?:^|[,;]\s*)({pattern})=([^;,]+)", str(header or "")):
        if match.group(1) in attributes:
            continue
        yield match.group(1), match.group(2).strip()


__all__ = [
    "AUTHSERVER_HOST",
    "BACKENDS",
    "BackendProfile",
    "WebVPNAuthenticationRequiredError",
    "PRIMARY_HOST",
    "TRANSIENT_STATUS_CODES",
    "WEBVPN_COOKIE_NAMES",
    "WEBVPN_HOST",
    "active_profile",
    "backend_payload",
    "build_headers",
    "candidate_profiles",
    "combine_cookie_values",
    "combine_cookie_values_excluding",
    "cookie_header",
    "get_preference",
    "get_profile",
    "has_webvpn_cookies",
    "mark_success",
    "mark_failure",
    "clear_primary_cooldown",
    "primary_cooldown_active",
    "primary_cooldown_remaining",
    "merge_set_cookie",
    "normalize_preference",
    "request_with_failover",
    "rewrite_referer_for_profile",
    "set_preference",
]
