from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

from hugin.startup_status import CLOSE_COMMAND

DOCKER_START_TIMEOUT_SECONDS = 180
DOCKER_POLL_SECONDS = 2.0


class WindowEvent(Protocol):
    def __iadd__(self, callback: Callable[[], bool]) -> WindowEvent: ...


class WindowEvents(Protocol):
    closing: WindowEvent


class TrayWindow(Protocol):
    events: WindowEvents

    def destroy(self) -> None: ...

    def hide(self) -> None: ...

    def show(self) -> None: ...


class _TrayIcon(Protocol):
    def run_detached(self) -> None: ...

    def stop(self) -> None: ...

    def notify(self, message: str, title: str | None = None) -> None: ...


class _PystrayModule(Protocol):
    def Icon(self, *args: object, **kwargs: object) -> _TrayIcon: ...

    def Menu(self, *items: object) -> object: ...

    def MenuItem(self, *args: object, **kwargs: object) -> object: ...


class _ImageModule(Protocol):
    def open(self, filename: Path) -> object: ...


class StartupStatusWindow:
    """Небольшое окно ожидания до запуска основного интерфейса."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def update(self, message: str) -> None:
        text = message.strip()
        if not text:
            return
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                process = subprocess.Popen(
                    [sys.executable, "-m", "hugin.startup_status"],
                    stdin=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                )
                self._process = process
            stream = process.stdin
            if stream is None:
                return
            try:
                stream.write(f"{text}\n")
                stream.flush()
            except (BrokenPipeError, OSError):
                with suppress(OSError, subprocess.TimeoutExpired):
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=2)
                with suppress(OSError):
                    stream.close()
                self._process = None

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
        if process is None:
            return
        stream = process.stdin
        try:
            if stream is not None:
                stream.write(f"{CLOSE_COMMAND}\n")
                stream.flush()
                stream.close()
            process.wait(timeout=5)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            with suppress(OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=2)


class DesktopTray:
    """Скрывает основное окно в трей и завершает приложение только через меню."""

    def __init__(self, window: TrayWindow, icon_path: Path) -> None:
        self._window = window
        self._icon_path = icon_path
        self._icon: _TrayIcon | None = None
        self._image: object | None = None
        self._exit_requested = False
        self._hidden_notice_shown = False
        self._lock = threading.Lock()

    def start(self) -> None:
        try:
            pystray = cast(_PystrayModule, import_module("pystray"))
            image_module = cast(_ImageModule, import_module("PIL.Image"))
        except ImportError as error:
            raise RuntimeError(
                "Не установлена поддержка системного трея. Выполните uv sync --extra desktop"
            ) from error

        self._image = image_module.open(self._icon_path)
        menu = pystray.Menu(
            pystray.MenuItem("Открыть Hugin", self._show_window, default=True),
            pystray.MenuItem("Выйти", self._request_exit),
        )
        icon = pystray.Icon(
            "hugin",
            self._image,
            "Hugin — поиск работы",
            menu=menu,
        )
        self._icon = icon
        closing = self._window.events.closing
        closing += self._on_window_closing
        icon.run_detached()

    def stop(self) -> None:
        with self._lock:
            self._exit_requested = True
            icon = self._icon
            self._icon = None
        if icon is not None:
            icon.stop()
        image = self._image
        self._image = None
        close = getattr(image, "close", None)
        if callable(close):
            close()

    def _on_window_closing(self) -> bool:
        with self._lock:
            if self._exit_requested:
                return True
            icon = self._icon
            show_notice = not self._hidden_notice_shown
            self._hidden_notice_shown = True
        self._window.hide()
        if icon is not None and show_notice:
            icon.notify(
                "Hugin продолжает поиск в фоне. Для полного закрытия выберите «Выйти».",
                "Hugin скрыт в системный трей",
            )
        return False

    def _show_window(self, *_args: object) -> None:
        self._window.show()

    def _request_exit(self, *_args: object) -> None:
        with self._lock:
            if self._exit_requested:
                return
            self._exit_requested = True
            icon = self._icon
        if icon is not None:
            icon.stop()
        self._window.destroy()


def ensure_docker_desktop_running(
    *,
    status: Callable[[str], None] | None = None,
    timeout_seconds: int = DOCKER_START_TIMEOUT_SECONDS,
) -> bool:
    """Запускает Docker Desktop при недоступном движке и ждёт его готовности."""

    if docker_engine_is_ready():
        return False
    if shutil.which("docker") is None:
        raise RuntimeError("Docker Desktop не установлен или команда docker недоступна")
    executable = docker_desktop_executable()
    if executable is None:
        raise RuntimeError("Docker Desktop не найден. Установите Docker Desktop и повторите запуск")
    if status is not None:
        status("Погодите, запускается Docker Desktop…")
    try:
        subprocess.Popen(
            [str(executable)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=int(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            ),
        )
    except OSError as error:
        raise RuntimeError("Не удалось запустить Docker Desktop") from error

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if docker_engine_is_ready():
            return True
        time.sleep(DOCKER_POLL_SECONDS)
    raise RuntimeError("Docker Desktop не запустился вовремя. Повторите запуск Hugin")


def docker_engine_is_ready(*, timeout_seconds: int = 8) -> bool:
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def docker_desktop_executable() -> Path | None:
    candidates: list[Path] = []
    for variable in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        root = os.getenv(variable)
        if root:
            candidates.append(Path(root) / "Docker" / "Docker" / "Docker Desktop.exe")
    if located := shutil.which("Docker Desktop"):
        candidates.append(Path(located))
    registry_path = _docker_desktop_registry_path()
    if registry_path is not None:
        candidates.append(registry_path)
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _docker_desktop_registry_path() -> Path | None:
    if os.name != "nt":
        return None
    try:
        winreg = import_module("winreg")
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Docker Desktop.exe",
        )
        try:
            value, _kind = winreg.QueryValueEx(key, None)
        finally:
            winreg.CloseKey(key)
    except (AttributeError, OSError):
        return None
    return Path(value) if isinstance(value, str) and value.strip() else None
