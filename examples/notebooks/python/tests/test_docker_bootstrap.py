"""Unit tests for the Docker launcher helpers in _setup.py.

Mocked at the subprocess + filesystem boundary only — `in_docker()` runs real
code, `start_docker()` exercises every early-return branch with real Path
objects and a fake `subprocess.run`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_docker_env(monkeypatch, tmp_path):
    monkeypatch.delenv("PATTER_NOTEBOOKS_IN_DOCKER", raising=False)
    monkeypatch.delenv("PATTER_NOTEBOOKS_NO_TOKEN", raising=False)
    monkeypatch.delenv("JUPYTER_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    yield


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "On"])
def test_in_docker_truthy_env_values(monkeypatch, value):
    import _setup

    monkeypatch.setenv("PATTER_NOTEBOOKS_IN_DOCKER", value)
    with patch.object(Path, "exists", return_value=False):
        assert _setup.in_docker() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "anything"])
def test_in_docker_falsy_env_values(monkeypatch, value):
    import _setup

    monkeypatch.setenv("PATTER_NOTEBOOKS_IN_DOCKER", value)
    with patch.object(Path, "exists", return_value=False):
        assert _setup.in_docker() is False


def test_in_docker_dockerenv_marker(monkeypatch):
    import _setup

    monkeypatch.delenv("PATTER_NOTEBOOKS_IN_DOCKER", raising=False)
    with patch.object(Path, "exists", lambda self: str(self) == "/.dockerenv"):
        assert _setup.in_docker() is True


def test_start_docker_returns_true_when_already_in_container(monkeypatch, capsys):
    import _setup

    monkeypatch.setenv("PATTER_NOTEBOOKS_IN_DOCKER", "1")
    assert _setup.start_docker() is True
    assert "already running" in capsys.readouterr().out


def test_start_docker_rejects_detach_false(capsys):
    import _setup

    assert _setup.start_docker(detach=False) is False
    assert "would block the kernel" in capsys.readouterr().out


def test_start_docker_returns_false_when_docker_missing(monkeypatch, capsys):
    import _setup

    monkeypatch.setattr(_setup.shutil, "which", lambda _name: None)
    assert _setup.start_docker() is False
    assert "docker CLI not found" in capsys.readouterr().out


def test_start_docker_returns_false_when_compose_failed(monkeypatch, capsys):
    import _setup

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "error: bind: address already in use\n"

    monkeypatch.setattr(_setup.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(_setup.subprocess, "run", lambda *a, **kw: _Result())
    assert _setup.start_docker(build=False) is False
    out = capsys.readouterr().out
    assert "exited with code 1" in out
    assert "address already in use" in out


def test_start_docker_returns_true_on_success(monkeypatch, capsys):
    import _setup

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(_setup.shutil, "which", lambda _name: "/usr/bin/docker")
    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _Result()

    monkeypatch.setattr(_setup.subprocess, "run", _fake_run)
    assert _setup.start_docker(build=True) is True
    out = capsys.readouterr().out
    assert "Docker stack up" in out
    assert "127.0.0.1:8888" in out
    assert "--build" in captured["cmd"]
    assert "-d" in captured["cmd"]
    assert "JUPYTER_TOKEN" in captured["env"]
    assert captured["env"]["JUPYTER_TOKEN"]


def test_generate_jupyter_token_is_stable(monkeypatch):
    import _setup

    first = _setup._generate_jupyter_token()
    second = _setup._generate_jupyter_token()
    assert first == second
    assert len(first) >= 32


def test_generate_jupyter_token_opt_out(monkeypatch):
    import _setup

    monkeypatch.setenv("PATTER_NOTEBOOKS_NO_TOKEN", "1")
    assert _setup._generate_jupyter_token() == ""
