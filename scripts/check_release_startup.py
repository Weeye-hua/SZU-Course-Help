"""Smoke-test both study programs in the native package without school requests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


def resolve_binary(release_dir: Path) -> Path:
    name = "SZU-Course-Help.exe" if os.name == "nt" else "SZU-Course-Help"
    candidates = [path for path in release_dir.glob(f"SZU-Course-Help-v*/{name}") if path.is_file()]
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one native executable in {release_dir}")
    return candidates[0]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def check_program(binary: Path, program: str, choice: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"szu-{program}-smoke-") as temporary:
        root = Path(temporary)
        port = free_port()
        env = {
            key: value for key, value in os.environ.items() if not key.startswith("COURSE_SELECT_")
        }
        env.update(
            COURSE_SELECT_PORT=str(port),
            COURSE_SELECT_DATA_DIR=str(root / "data"),
            COURSE_SELECT_NO_BROWSER="1",
            PYTHONIOENCODING="utf-8",
        )
        # Only the local bootstrap and static pages are read. No login/captcha
        # or enrollment endpoints are called, and all data is temporary.
        opener = build_opener(ProxyHandler({}))
        with (root / "startup.log").open("w+b") as log:
            process = subprocess.Popen(
                [str(binary)],
                cwd=binary.parent,
                env=env,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                if process.stdin is None:
                    raise RuntimeError("Startup stdin is unavailable")
                process.stdin.write(f"{choice}\n23010001\nY\n".encode())
                process.stdin.flush()
                process.stdin.close()
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"{program} exited before startup")
                    try:
                        with opener.open(
                            f"http://127.0.0.1:{port}/api/bootstrap", timeout=2
                        ) as response:
                            bootstrap = json.load(response)
                        break
                    except (URLError, TimeoutError, OSError):
                        time.sleep(0.5)
                else:
                    raise RuntimeError(f"{program} startup timed out")
                if bootstrap.get("program") != program:
                    raise RuntimeError(f"Unexpected program: {bootstrap.get('program')}")
                expected_captcha = "text" if program == "graduate" else "click"
                if bootstrap.get("captcha_kind") != expected_captcha:
                    raise RuntimeError(f"{program} loaded the wrong captcha mode")
                for path, expected in (
                    ("/login", b"<!doctype html>"),
                    ("/course-app.js", b"graduate"),
                ):
                    with opener.open(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
                        if expected not in response.read():
                            raise RuntimeError(f"{program} package is missing {path}")
            except Exception:
                log.seek(0)
                print(log.read().decode("utf-8", errors="replace"))
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        print(f"Native startup passed: {program} (local HTTP only)")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: check_release_startup.py <release-directory>")
    binary = resolve_binary(Path(sys.argv[1]).resolve())
    for program, choice in (("undergraduate", "1"), ("graduate", "2")):
        check_program(binary, program, choice)


if __name__ == "__main__":
    main()
