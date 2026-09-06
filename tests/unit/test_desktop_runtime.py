from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import hugin.desktop_runtime as desktop_runtime
from hugin.startup_status import CLOSE_COMMAND


class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def __iadd__(self, callback: object) -> FakeEvent:
        self.handlers.append(callback)
        return self


class FakeEvents:
    def __init__(self, closing: FakeEvent) -> None:
        self.closing: desktop_runtime.WindowEvent = closing


class FakeWindow:
    def __init__(self) -> None:
        self.closing = FakeEvent()
        self.events: desktop_runtime.WindowEvents = FakeEvents(self.closing)
        self.hidden = 0
        self.shown = 0
        self.destroyed = 0

    def hide(self) -> None:
        self.hidden += 1

    def show(self) -> None:
        self.shown += 1

    def destroy(self) -> None:
        self.destroyed += 1


class FakeImage:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeImageModule:
    def __init__(self, image: FakeImage) -> None:
        self.image = image

    def open(self, _filename: Path) -> FakeImage:
        return self.image


class FakeMenuItem:
    def __init__(self, text: str, action: object, *, default: bool = False) -> None:
        self.text = text
        self.action = action
        self.default = default


class FakeIcon:
    def __init__(self, menu: tuple[FakeMenuItem, ...]) -> None:
        self.menu = menu
        self.started = False
        self.stopped = False
        self.notifications: list[tuple[str, str | None]] = []

    def run_detached(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def notify(self, message: str, title: str | None = None) -> None:
        self.notifications.append((message, title))


class FakePystray:
    def __init__(self) -> None:
        self.icon: FakeIcon | None = None

    @staticmethod
    def Menu(*items: FakeMenuItem) -> tuple[FakeMenuItem, ...]:
        return items

    @staticmethod
    def MenuItem(text: str, action: object, *, default: bool = False) -> FakeMenuItem:
        return FakeMenuItem(text, action, default=default)

    def Icon(
        self,
        _name: str,
        _image: object,
        _title: str,
        *,
        menu: tuple[FakeMenuItem, ...],
    ) -> FakeIcon:
        self.icon = FakeIcon(menu)
        return self.icon


class FakeInputStream:
    def __init__(self) -> None:
        self.values: list[str] = []
        self.closed = False

    def write(self, value: str) -> int:
        self.values.append(value)
        return len(value)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeStatusProcess:
    def __init__(self) -> None:
        self.stdin = FakeInputStream()
        self.waited = False
        self.killed = False

    def poll(self) -> None:
        return None

    def wait(self, timeout: int) -> int:
        assert timeout == 5
        self.waited = True
        return 0

    def kill(self) -> None:
        self.killed = True


def test_startup_status_uses_separate_process(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeStatusProcess()
    launches: list[tuple[object, ...]] = []

    def popen(*args: object, **kwargs: object) -> FakeStatusProcess:
        launches.append((args, kwargs))
        return process

    monkeypatch.setattr("hugin.desktop_runtime.subprocess.Popen", popen)
    status = desktop_runtime.StartupStatusWindow()

    status.update("Погодите, запускается Docker Desktop…")
    status.update("Запускаются контейнеры Hugin…")
    status.close()

    assert len(launches) == 1
    assert process.stdin.values == [
        "Погодите, запускается Docker Desktop…\n",
        "Запускаются контейнеры Hugin…\n",
        f"{CLOSE_COMMAND}\n",
    ]
    assert process.stdin.closed
    assert process.waited
    assert not process.killed


def test_tray_hides_window_and_exits_only_from_menu(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    window = FakeWindow()
    image = FakeImage()
    pystray = FakePystray()
    modules = {
        "pystray": pystray,
        "PIL.Image": FakeImageModule(image),
    }
    monkeypatch.setattr(desktop_runtime, "import_module", modules.__getitem__)
    tray = desktop_runtime.DesktopTray(window, tmp_path / "hugin.ico")

    tray.start()

    assert pystray.icon is not None
    assert pystray.icon.started
    closing = window.closing.handlers[0]
    assert callable(closing)
    assert closing() is False
    assert window.hidden == 1
    assert window.destroyed == 0
    assert len(pystray.icon.notifications) == 1

    open_item, exit_item = pystray.icon.menu
    assert open_item.text == "Открыть Hugin"
    assert open_item.default
    assert callable(open_item.action)
    open_item.action()
    assert window.shown == 1

    assert exit_item.text == "Выйти"
    assert callable(exit_item.action)
    exit_item.action()
    assert window.destroyed == 1
    assert pystray.icon.stopped
    assert closing() is True

    tray.stop()
    assert image.closed


def test_docker_desktop_is_started_and_waited_for(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Docker Desktop.exe"
    executable.write_bytes(b"")
    readiness = iter((False, False, True))
    launched: list[tuple[object, ...]] = []
    messages: list[str] = []

    monkeypatch.setattr(desktop_runtime, "docker_engine_is_ready", lambda: next(readiness))
    monkeypatch.setattr("hugin.desktop_runtime.shutil.which", lambda _name: "docker.exe")
    monkeypatch.setattr(desktop_runtime, "docker_desktop_executable", lambda: executable)
    monkeypatch.setattr(
        "hugin.desktop_runtime.subprocess.Popen",
        lambda *args, **kwargs: launched.append((args, kwargs)),
    )
    monkeypatch.setattr("hugin.desktop_runtime.time.sleep", lambda _seconds: None)

    started = desktop_runtime.ensure_docker_desktop_running(status=messages.append)

    assert started
    assert launched[0][0] == ([str(executable)],)
    assert messages == ["Погодите, запускается Docker Desktop…"]


def test_ready_docker_is_not_started(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_runtime, "docker_engine_is_ready", lambda: True)
    monkeypatch.setattr(
        "hugin.desktop_runtime.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("Docker Desktop не должен запускаться"),
    )

    assert not desktop_runtime.ensure_docker_desktop_running()


def test_docker_start_timeout_has_clear_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Docker Desktop.exe"
    executable.write_bytes(b"")
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(desktop_runtime, "docker_engine_is_ready", lambda: False)
    monkeypatch.setattr("hugin.desktop_runtime.shutil.which", lambda _name: "docker.exe")
    monkeypatch.setattr(desktop_runtime, "docker_desktop_executable", lambda: executable)
    monkeypatch.setattr(
        "hugin.desktop_runtime.subprocess.Popen",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr("hugin.desktop_runtime.time.monotonic", lambda: next(clock))

    with pytest.raises(RuntimeError, match="не запустился вовремя"):
        desktop_runtime.ensure_docker_desktop_running(timeout_seconds=1)


def test_docker_probe_handles_missing_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hugin.desktop_runtime.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )
    assert not desktop_runtime.docker_engine_is_ready()

    def timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired("docker", 8)

    monkeypatch.setattr("hugin.desktop_runtime.subprocess.run", timeout)
    assert not desktop_runtime.docker_engine_is_ready()
