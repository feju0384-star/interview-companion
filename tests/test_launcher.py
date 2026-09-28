import subprocess
import sys

import pytest
import launcher


def test_failed_dependency_install_is_retried_without_overwriting_success_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "python_works", lambda path: True)
    (tmp_path / ".venv").mkdir()
    marker = tmp_path / ".venv" / ".requirements-installed"
    marker.write_bytes(b"old-requirements")
    (tmp_path / "requirements.txt").write_bytes(b"new-requirements")
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "pip")
    monkeypatch.setattr(launcher.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        launcher.ensure_environment()
    assert marker.read_bytes() == b"old-requirements"
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: None)
    launcher.ensure_environment()
    assert marker.read_bytes() == b"new-requirements"


def test_existing_background_service_does_not_reinstall_or_open_browser(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["launcher.py", "--background"])
    monkeypatch.setattr(launcher, "running", lambda: True)
    def forbidden(*args):
        raise AssertionError("Existing background service should be reused")
    monkeypatch.setattr(launcher, "ensure_environment", forbidden)
    monkeypatch.setattr(launcher.webbrowser, "open", forbidden)
    launcher.main()
