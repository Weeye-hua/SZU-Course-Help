"""Post-build smoke test: packaged children must inherit a clean linker path.

Runs the freshly built Linux binary with a stubbed ``xdg-open`` placed first
on ``PATH``, waits for the startup auto-open to fire, and asserts the stub
received an environment without current-directory entries or the release
directory in ``LD_LIBRARY_PATH``/``LD_PRELOAD``. The app's own startup
performs the real ddddocr/OpenCV/ONNX Runtime initialization, so reaching
the auto-open also proves lazy native imports still work after isolation.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STUB_WAIT_SECONDS = 120.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _resolve_stage(raw: Path) -> Path:
    """Accept either the stage folder or a release folder containing it."""
    if (raw / "SZU-Course-Help").is_file():
        return raw
    candidates = sorted(
        path
        for path in raw.glob("SZU-Course-Help-v*")
        if path.is_dir() and (path / "SZU-Course-Help").is_file()
    )
    if len(candidates) != 1:
        print(
            "Child-env smoke test failed: expected exactly one staged package in "
            f"{raw}, found {[str(path) for path in candidates]}"
        )
        raise SystemExit(2)
    return candidates[0]


def main() -> int:
    if not sys.platform.startswith("linux"):
        print("Child-env smoke test only applies to Linux builds; skipping.")
        return 0
    if len(sys.argv) != 2:
        print("usage: check_release_child_env.py <stage-or-release-dir>")
        return 2
    stage = _resolve_stage(Path(sys.argv[1]).resolve())
    binary = stage / "SZU-Course-Help"

    with tempfile.TemporaryDirectory(prefix="szu-child-env-") as tmp:
        tmp_path = Path(tmp)
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        stub_log = tmp_path / "xdg-open-env.log"
        stub = stub_dir / "xdg-open"
        stub.write_text(f"#!/bin/sh\nenv > '{stub_log}'\nexit 0\n", encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

        env = dict(os.environ)
        env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
        env["COURSE_SELECT_PORT"] = str(_free_port())
        # Keep the smoke test's runtime data inside the temporary directory.
        env["COURSE_SELECT_DATA_DIR"] = str(tmp_path / "data")
        env["COURSE_SELECT_KEY_DIR"] = str(tmp_path / "data" / "keys")
        # Exercise the interactive program selector before the Card Key prompt.
        env.pop("COURSE_SELECT_PROGRAM", None)
        stdin_feed = b"1\n23010001\nY\n"

        process = subprocess.Popen(
            [str(binary)],
            cwd=stage,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            if process.stdin is None:  # pragma: no cover - Popen guarantees PIPE here
                print("Child-env smoke test failed: stdin pipe unavailable")
                return 1
            process.stdin.write(stdin_feed)
            process.stdin.flush()
            deadline = time.monotonic() + STUB_WAIT_SECONDS
            while time.monotonic() < deadline:
                if stub_log.is_file():
                    break
                if process.poll() is not None:
                    print("Child-env smoke test failed: binary exited before auto-open")
                    return 1
                time.sleep(0.5)
            else:
                print("Child-env smoke test failed: auto-open did not fire in time")
                return 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

        captured = stub_log.read_text(encoding="utf-8")
        failures = []
        for name in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
            value = ""
            for line in captured.splitlines():
                if line.startswith(f"{name}="):
                    value = line[len(name) + 1 :]
                    break
            if not value:
                continue  # absent or emptied entirely: safe
            for entry in value.split(os.pathsep):
                if not entry or entry == ".":
                    failures.append(f"{name} keeps a current-directory entry: {value!r}")
                elif Path(entry).resolve() == stage:
                    failures.append(f"{name} keeps the release directory: {value!r}")
        if failures:
            print("Child-env smoke test FAILED:")
            for failure in failures:
                print(f"  - {failure}")
            print(captured)
            return 1
        print("Child-env smoke test passed: packaged children inherit a clean linker path.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
