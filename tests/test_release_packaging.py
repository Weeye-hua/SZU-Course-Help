from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import check_release_startup, package_source


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_source_files_only_returns_git_tracked_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")

    tracked = repo / "README.md"
    tracked.write_text("tracked\n", encoding="utf-8")
    (repo / "debug.txt").write_text("must not ship\n", encoding="utf-8")
    _git(repo, "add", "README.md")

    assert package_source.source_files(repo) == [tracked]


def test_source_files_rejects_missing_tracked_entry(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")

    tracked = repo / "README.md"
    tracked.write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    tracked.unlink()

    try:
        package_source.source_files(repo)
    except RuntimeError as exc:
        assert "README.md" in str(exc)
    else:
        raise AssertionError("missing tracked files must fail source packaging")


def test_native_smoke_resolves_only_one_package(tmp_path: Path) -> None:
    import os

    name = "SZU-Course-Help.exe" if os.name == "nt" else "SZU-Course-Help"
    with pytest.raises(RuntimeError, match="one native"):
        check_release_startup.resolve_binary(tmp_path)
    stage = tmp_path / "SZU-Course-Help-v3.7.0-test"
    stage.mkdir()
    binary = stage / name
    binary.touch()
    assert check_release_startup.resolve_binary(tmp_path) == binary
    other = tmp_path / "SZU-Course-Help-v3.6.4-test"
    other.mkdir()
    (other / name).touch()
    with pytest.raises(RuntimeError, match="one native"):
        check_release_startup.resolve_binary(tmp_path)


@pytest.mark.parametrize(
    "program,choice,kind", [("undergraduate", "1", "click"), ("graduate", "2", "text")]
)
@pytest.mark.parametrize("invalid_bootstrap", [False, True])
def test_native_smoke_checks_modes_without_school_requests(
    monkeypatch, tmp_path: Path, program, choice, kind, invalid_bootstrap
) -> None:
    import io
    import json
    from types import SimpleNamespace

    captured = {}
    urls = []

    class Input(io.BytesIO):
        def close(self):
            captured["input"] = self.getvalue()
            super().close()

    class Process:
        def __init__(self, command, **kwargs):
            captured.update(kwargs)
            self.stdin = Input()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            captured["terminated"] = True
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

    def open_local(url, **kwargs):
        urls.append(url)
        assert url.startswith("http://127.0.0.1:18013/")
        if url.endswith("/api/bootstrap"):
            return io.BytesIO(
                json.dumps(
                    {"program": "wrong" if invalid_bootstrap else program, "captcha_kind": kind}
                ).encode()
            )
        if url.endswith("/login"):
            return io.BytesIO(b"<!doctype html>")
        assert url.endswith("/course-app.js")
        return io.BytesIO(b"graduate")

    monkeypatch.setenv("COURSE_SELECT_PROGRAM", "graduate")
    monkeypatch.setenv("COURSE_SELECT_DB_PATH", "must-not-use.db")
    monkeypatch.setattr(check_release_startup, "free_port", lambda: 18013)
    monkeypatch.setattr(check_release_startup.subprocess, "Popen", Process)
    monkeypatch.setattr(
        check_release_startup, "build_opener", lambda *_: SimpleNamespace(open=open_local)
    )
    binary = tmp_path / "SZU-Course-Help"
    if invalid_bootstrap:
        with pytest.raises(RuntimeError, match="Unexpected program"):
            check_release_startup.check_program(binary, program, choice)
    else:
        check_release_startup.check_program(binary, program, choice)
        assert len(urls) == 3
    assert captured["input"] == f"{choice}\n23010001\nY\n".encode()
    assert captured["terminated"] is True
    assert "COURSE_SELECT_DB_PATH" not in captured["env"]
    assert "COURSE_SELECT_PROGRAM" not in captured["env"]
    assert captured["env"]["COURSE_SELECT_NO_BROWSER"] == "1"
