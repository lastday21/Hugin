from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hugin import desktop_runtime


class Pipe:
    def __init__(self, broken: bool) -> None:
        self.broken = broken

    def write(self, value: str) -> int:
        if self.broken:
            raise BrokenPipeError("child closed the pipe")
        return len(value)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class Child:
    def __init__(self, *, broken: bool = False, stuck: bool = False) -> None:
        self.stdin = Pipe(broken)
        self.stuck = stuck
        self.killed = False
        self.reaped = False

    def poll(self) -> int | None:
        return 0 if self.reaped else None

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: int) -> int:
        if self.stuck and not self.killed:
            raise subprocess.TimeoutExpired("startup", timeout)
        self.reaped = True
        return 0


def test_broken_status_pipe_does_not_leave_an_orphan_and_next_update_can_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = Child(broken=True)
    replacement = Child()
    children = iter((broken, replacement))
    monkeypatch.setattr(
        "hugin.desktop_runtime.subprocess.Popen", lambda *args, **kwargs: next(children)
    )
    status = desktop_runtime.StartupStatusWindow()
    status.update("Checking")
    assert broken.killed and broken.reaped
    status.update("Ready")
    status.close()
    assert replacement.reaped
    status.close()


def test_slow_child_exit_does_not_turn_status_window_failure_into_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = Child(broken=True)

    def slow_exit(timeout: int) -> int:
        raise subprocess.TimeoutExpired("startup", timeout)

    monkeypatch.setattr(child, "wait", slow_exit)
    monkeypatch.setattr("hugin.desktop_runtime.subprocess.Popen", lambda *args, **kwargs: child)
    status = desktop_runtime.StartupStatusWindow()
    status.update("Checking")
    assert child.killed
    replacement = Child()
    monkeypatch.setattr(
        "hugin.desktop_runtime.subprocess.Popen", lambda *args, **kwargs: replacement
    )
    status.update("Ready")
    status.close()
    assert replacement.reaped


@pytest.mark.parametrize("broken", [False, True])
def test_close_reaps_a_stuck_or_disconnected_status_child(
    monkeypatch: pytest.MonkeyPatch, broken: bool
) -> None:
    child = Child(stuck=True)
    monkeypatch.setattr("hugin.desktop_runtime.subprocess.Popen", lambda *args, **kwargs: child)
    status = desktop_runtime.StartupStatusWindow()
    status.update("")
    status.update("Waiting")
    child.stdin.broken = broken
    status.close()
    assert child.killed and child.reaped
    status.close()


def test_missing_docker_is_explained_before_trying_to_launch_any_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop_runtime, "docker_engine_is_ready", lambda: False)
    monkeypatch.setattr("hugin.desktop_runtime.shutil.which", lambda value: None)
    with pytest.raises(RuntimeError, match="docker"):
        desktop_runtime.ensure_docker_desktop_running()


def test_missing_desktop_installation_and_launch_denial_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop_runtime, "docker_engine_is_ready", lambda: False)
    monkeypatch.setattr("hugin.desktop_runtime.shutil.which", lambda value: "docker")
    monkeypatch.setattr(desktop_runtime, "docker_desktop_executable", lambda: None)
    with pytest.raises(RuntimeError):
        desktop_runtime.ensure_docker_desktop_running()
    monkeypatch.setattr(
        desktop_runtime, "docker_desktop_executable", lambda: Path("Docker Desktop.exe")
    )

    def denied(*args: object, **kwargs: object) -> None:
        raise OSError("access denied")

    monkeypatch.setattr("hugin.desktop_runtime.subprocess.Popen", denied)
    with pytest.raises(RuntimeError):
        desktop_runtime.ensure_docker_desktop_running()


def test_desktop_discovery_uses_real_existing_path_and_ignores_stale_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setattr("hugin.desktop_runtime.shutil.which", lambda value: None)
    monkeypatch.setattr(
        desktop_runtime, "_docker_desktop_registry_path", lambda: tmp_path / "stale.exe"
    )
    assert desktop_runtime.docker_desktop_executable() is None
    executable = tmp_path / "LOCALAPPDATA/Docker/Docker/Docker Desktop.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    assert desktop_runtime.docker_desktop_executable() == executable


@pytest.mark.parametrize("value", ["D:/Docker/Docker Desktop.exe", "   ", 42])
def test_registry_discovery_validates_installation_value_and_closes_key(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    key = object()
    closed: list[object] = []

    def open_key(root: object, name: str) -> object:
        assert root == "machine"
        assert name.endswith(r"App Paths\Docker Desktop.exe")
        return key

    registry = SimpleNamespace(
        HKEY_LOCAL_MACHINE="machine",
        OpenKey=open_key,
        QueryValueEx=lambda handle, name: (value, 1),
        CloseKey=closed.append,
    )
    monkeypatch.setattr(desktop_runtime, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(desktop_runtime, "import_module", lambda name: registry)
    expected = Path(value) if isinstance(value, str) and value.strip() else None
    assert desktop_runtime._docker_desktop_registry_path() == expected
    assert closed == [key]


def test_registry_read_failure_releases_key_and_non_windows_skips_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = object()
    closed: list[object] = []

    def denied(*args: object) -> None:
        raise OSError("registry value inaccessible")

    registry = SimpleNamespace(
        HKEY_LOCAL_MACHINE="machine",
        OpenKey=lambda *args: key,
        QueryValueEx=denied,
        CloseKey=closed.append,
    )
    monkeypatch.setattr(desktop_runtime, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(desktop_runtime, "import_module", lambda name: registry)
    assert desktop_runtime._docker_desktop_registry_path() is None
    assert closed == [key]
    monkeypatch.setattr(desktop_runtime, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(desktop_runtime, "import_module", denied)
    assert desktop_runtime._docker_desktop_registry_path() is None
