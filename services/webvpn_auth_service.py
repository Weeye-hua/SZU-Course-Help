"""Ephemeral controlled-browser authentication for read-only WebVPN fallback."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PureWindowsPath
from typing import Any

from project_paths import external_process_env
from services import auth_service, backend_service

logger = logging.getLogger(__name__)

AUTH_TIMEOUT_SECONDS = 300
CDP_STARTUP_TIMEOUT_SECONDS = 8
CDP_POLL_INTERVAL_SECONDS = 1


class ControlledBrowserUnavailableError(RuntimeError):
    """Raised when no supported local browser can be started."""


def build_auth_url(target_path: str = "/xsxkapp/sys/xsxkapp/*default/index.do") -> str:
    """Build the real AuthServer URL used by the WebVPN CAS flow."""
    normalized_path = "/" + str(target_path or "").lstrip("/")
    target = f"https://{backend_service.WEBVPN_HOST}{normalized_path}"
    callback = (
        f"https://{backend_service.WEBVPN_ROOT_HOST}/users/auth/cas/callback?"
        f"url={urllib.parse.quote(target, safe='')}"
    )
    return (
        f"https://{backend_service.AUTHSERVER_HOST}/authserver/login?"
        f"{urllib.parse.urlencode({'service': callback})}"
    )


def _known_browser_paths(
    platform_name: str | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Return conventional Chromium-family install locations for one platform."""
    current_platform = platform_name or sys.platform
    env = environment or os.environ
    if current_platform == "darwin":
        return (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        )
    if current_platform.startswith("win"):
        roots = tuple(
            value
            for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
            if (value := str(env.get(key, "")).strip())
        )
        suffixes = (
            PureWindowsPath("Google/Chrome/Application/chrome.exe"),
            PureWindowsPath("Microsoft/Edge/Application/msedge.exe"),
            PureWindowsPath("Chromium/Application/chrome.exe"),
        )
        return tuple(str(PureWindowsPath(root) / suffix) for root in roots for suffix in suffixes)
    return ()


def find_browser() -> str | None:
    """Return a Chromium-family executable, honoring an explicit override."""
    configured = str(os.getenv("COURSE_SELECT_BROWSER", "")).strip()
    candidates = [configured] if configured else []
    candidates.extend(
        (
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
            "microsoft-edge",
            "microsoft-edge-stable",
        )
    )
    candidates.extend(_known_browser_paths())
    for candidate in candidates:
        if not candidate:
            continue
        executable = shutil.which(candidate)
        if executable:
            return executable
        if (
            os.path.isabs(candidate)
            and Path(candidate).is_file()
            and (os.name == "nt" or os.access(candidate, os.X_OK))
        ):
            return candidate
    return None


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _read_http_json(url: str, timeout: float = 2.0) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _open_devtools_page(port: int, url: str) -> dict[str, Any]:
    """Create a new tab through the browser's DevTools HTTP endpoint."""
    # ``/json/new`` consumes the URL directly after ``?``. Preserve the URL
    # separators and the school entry-point wildcard; encode only characters
    # that could terminate or corrupt the DevTools request target.
    encoded_url = urllib.parse.quote(url, safe=":/?=&%*")
    last_error: Exception | None = None
    # Chromium versions differ: newer builds require PUT, while older builds
    # accept GET. Try the documented method first and retain compatibility.
    for method in ("PUT", "GET"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?{encoded_url}",
            method=method,
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return dict(json.loads(response.read().decode("utf-8")))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = exc
    raise ControlledBrowserUnavailableError("无法通过 DevTools 打开受控浏览器页面") from last_error


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("DevTools websocket closed")
        chunks.extend(chunk)
    return bytes(chunks)


def _websocket_frame(payload: bytes, opcode: int = 1) -> bytes:
    mask = secrets.token_bytes(4)
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
    elif length <= 0xFFFF:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return header + mask + masked


def _read_websocket_frame(sock: socket.socket) -> tuple[int, bytes]:
    first, second = _recv_exact(sock, 2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if second & 0x80:
        mask = _recv_exact(sock, 4)
        payload = bytes(
            value ^ mask[index % 4] for index, value in enumerate(_recv_exact(sock, length))
        )
    else:
        payload = _recv_exact(sock, length)
    return opcode, payload


def _cdp_command(websocket_url: str, method: str, params: dict[str, Any] | None = None) -> dict:
    """Call one CDP method using a tiny stdlib-only WebSocket client."""
    parsed = urllib.parse.urlsplit(websocket_url)
    if parsed.scheme != "ws" or not parsed.hostname or not parsed.port:
        raise ValueError("unsupported DevTools websocket URL")
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    host_header = f"{parsed.hostname}:{parsed.port}"
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    expected_accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode("ascii")
    command_id = secrets.randbelow(2**31 - 1) + 1
    command = json.dumps(
        {"id": command_id, "method": method, "params": params or {}},
        separators=(",", ":"),
    ).encode("utf-8")
    with socket.create_connection((parsed.hostname, parsed.port), timeout=3) as sock:
        sock.sendall(request)
        response_headers = bytearray()
        while b"\r\n\r\n" not in response_headers:
            response_headers.extend(sock.recv(4096))
            if len(response_headers) > 65536:
                raise ConnectionError("invalid DevTools websocket handshake")
        header_text = bytes(response_headers).split(b"\r\n\r\n", 1)[0].decode("latin1")
        if (
            " 101 " not in header_text
            or f"Sec-WebSocket-Accept: {expected_accept}" not in header_text
        ):
            raise ConnectionError("DevTools websocket handshake rejected")
        sock.sendall(_websocket_frame(command))
        while True:
            opcode, payload = _read_websocket_frame(sock)
            if opcode == 0x9:
                sock.sendall(_websocket_frame(payload, opcode=0xA))
                continue
            if opcode == 0x8:
                raise ConnectionError("DevTools websocket closed before response")
            if opcode != 0x1:
                continue
            message = json.loads(payload.decode("utf-8"))
            if message.get("id") == command_id:
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return dict(message.get("result") or {})


def extract_webvpn_cookie_header(cookies: list[dict[str, Any]]) -> str:
    """Extract only required cookies from CDP's cookie records."""
    values: dict[str, str] = {}
    allowed = set(backend_service.WEBVPN_COOKIE_NAMES)
    for cookie in cookies:
        name = str(cookie.get("name", ""))
        domain = str(cookie.get("domain", "")).lstrip(".").lower()
        value = str(cookie.get("value", ""))
        expires = cookie.get("expires", -1)
        if isinstance(expires, (int, float)) and 0 < expires <= time.time():
            continue
        if name in allowed and (domain == "szu.edu.cn" or domain.endswith(".szu.edu.cn")) and value:
            values[name] = value
    if not all(name in values for name in backend_service.WEBVPN_COOKIE_NAMES):
        return ""
    return "; ".join(f"{name}={values[name]}" for name in backend_service.WEBVPN_COOKIE_NAMES)


class ControlledBrowserManager:
    """Own one isolated browser process and import its WebVPN cookies."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._profile_dir = ""
        self._debug_port = 0
        self._auth_url = ""
        self._state = "idle"
        self._message = ""
        self._started_at = 0.0
        self._worker: threading.Thread | None = None

    def start(self, target_path: str = "") -> dict[str, Any]:
        with self._lock:
            if backend_service.has_webvpn_cookies():
                self._state = "authenticated"
                self._message = "WebVPN 已认证"
                return self.status()
            if self._state in {"starting", "pending"}:
                return self.status()
            if self._browser_alive():
                try:
                    self._auth_url = self._auth_url or build_auth_url(
                        target_path or "/xsxkapp/sys/xsxkapp/*default/index.do"
                    )
                    _open_devtools_page(self._debug_port, self._auth_url)
                    self._state = "starting"
                    self._message = "请在受控浏览器中完成企业微信扫码或统一认证"
                    self._start_watcher()
                    return self.status()
                except ControlledBrowserUnavailableError:
                    self._forget_dead_browser()
            executable = find_browser()
            if not executable:
                self._state = "error"
                self._message = "未找到 Chromium、Chrome 或 Edge，请安装浏览器后重试"
                raise ControlledBrowserUnavailableError(self._message)
            self._profile_dir = tempfile.mkdtemp(prefix="szu-course-webvpn-")
            self._debug_port = _free_local_port()
            self._auth_url = build_auth_url(target_path or "/xsxkapp/sys/xsxkapp/*default/index.do")
            args = self._browser_args(executable, self._auth_url)
            try:
                self._process = subprocess.Popen(
                    args,
                    env=external_process_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, ValueError) as exc:
                self.stop_browser()
                self._state = "error"
                self._message = f"受控浏览器启动失败：{exc}"
                raise ControlledBrowserUnavailableError(self._message) from exc
            self._state = "starting"
            self._message = "正在启动受控浏览器，请在新窗口中完成扫码或统一认证"
            self._started_at = time.monotonic()
            self._start_watcher()
            return self.status()

    def _browser_args(self, executable: str, url: str) -> list[str]:
        return [
            executable,
            f"--user-data-dir={self._profile_dir}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self._debug_port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--new-window",
            url,
        ]

    def _start_watcher(self) -> None:
        self._worker = threading.Thread(
            target=self._watch,
            name="webvpn-auth-browser",
            daemon=True,
        )
        self._worker.start()

    def _browser_alive(self) -> bool:
        process = self._process
        if process is None or process.poll() is not None or not self._debug_port:
            return False
        try:
            _read_http_json(f"http://127.0.0.1:{self._debug_port}/json/version")
            return True
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def _forget_dead_browser(self) -> None:
        self._process = None
        self._debug_port = 0

    def _watch(self) -> None:
        deadline = time.monotonic() + AUTH_TIMEOUT_SECONDS
        ready_deadline = time.monotonic() + CDP_STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            with self._lock:
                process = self._process
                if self._state not in {"starting", "pending"}:
                    return
            if process is not None and process.poll() is not None:
                self._set_error("受控浏览器已关闭，WebVPN 认证未完成")
                return
            try:
                targets = _read_http_json(f"http://127.0.0.1:{self._debug_port}/json/list")
                with self._lock:
                    self._state = "pending"
                    self._message = "请在受控浏览器中完成企业微信扫码或统一认证"
                cookies = self._read_cookies(targets)
                cookie_header = extract_webvpn_cookie_header(cookies)
                if cookie_header:
                    auth_service.merge_backend_cookies([cookie_header], backend_service.WEBVPN_HOST)
                    self._finish_authenticated()
                    return
            except (OSError, ValueError, KeyError, RuntimeError, urllib.error.URLError) as exc:
                if time.monotonic() >= ready_deadline:
                    logger.debug("Waiting for WebVPN DevTools session: %s", exc)
            time.sleep(CDP_POLL_INTERVAL_SECONDS)
        self._set_error("WebVPN 认证等待超时，请重新点击认证按钮")

    @staticmethod
    def _read_cookies(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for target in targets:
            websocket_url = str(target.get("webSocketDebuggerUrl", ""))
            if target.get("type") == "page" and websocket_url:
                result = _cdp_command(websocket_url, "Network.getAllCookies")
                return list(result.get("cookies") or [])
        return []

    def _finish_authenticated(self) -> None:
        with self._lock:
            self._state = "authenticated"
            self._message = "WebVPN 认证完成，Cookie 已安全导入"
        # Close the authentication window and erase its temporary profile.
        self.stop_browser()

    def _set_error(self, message: str) -> None:
        with self._lock:
            if self._state not in {"authenticated", "idle"}:
                self._state = "error"
                self._message = message
        self.stop_browser()

    def stop_browser(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            self._debug_port = 0
            profile_dir, self._profile_dir = self._profile_dir, ""
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                with contextlib.suppress(OSError):
                    process.kill()
        if profile_dir:
            for attempt in range(4):
                try:
                    shutil.rmtree(profile_dir)
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    if attempt == 3:
                        logger.warning("Could not erase temporary WebVPN browser profile")
                    else:
                        time.sleep(0.15 * (attempt + 1))

    def close(self) -> dict[str, Any]:
        self.stop_browser()
        with self._lock:
            if self._state not in {"authenticated", "error"}:
                self._state = "idle"
                self._message = ""
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._state
            message = self._message
            auth_url = self._auth_url
            started_at = self._started_at
        authenticated = backend_service.has_webvpn_cookies()
        if authenticated and state not in {"error"}:
            state = "authenticated"
        elif not authenticated and state == "authenticated":
            state = "expired"
            message = backend_service.WEBVPN_AUTH_MESSAGE
        return {
            "state": state,
            "authenticated": authenticated,
            "message": message or ("WebVPN 已认证" if authenticated else ""),
            "auth_url": auth_url,
            "elapsed_seconds": round(max(0.0, time.monotonic() - started_at), 1)
            if started_at
            else 0,
        }


manager = ControlledBrowserManager()


def start_auth(target_path: str = "") -> dict[str, Any]:
    return manager.start(target_path)


def get_status() -> dict[str, Any]:
    return manager.status()


def close_auth() -> dict[str, Any]:
    return manager.close()
