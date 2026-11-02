from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from hugin.adapters.hh_browser import VisibleHhBrowser, _BrowserProfileLock


class BrowserControl:
    """Передача управления без изменения настроек отправки и фоновых процессов."""

    def __init__(self, data_dir: Path, account_id: int) -> None:
        if account_id < 1:
            raise ValueError("Некорректный номер аккаунта")
        self.path = data_dir / "browser-control" / f"account-{account_id}.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = _BrowserProfileLock(self.path.with_suffix(".lock"), timeout_seconds=5)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text("utf8"))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise RuntimeError("Повреждён запрос управления браузером")
        token = value.get("token")
        if (
            not isinstance(token, str)
            or len(token) != 32
            or any(character not in "0123456789abcdef" for character in token)
            or value.get("state") not in {"requested", "ready", "failed"}
        ):
            raise RuntimeError("Повреждён номер передачи управления браузером")
        if not isinstance(value.get("expires_at"), str):
            raise ValueError("Повреждён срок управления браузером")
        expires = datetime.fromisoformat(value["expires_at"])
        if expires.tzinfo is None:
            raise ValueError("Срок управления должен содержать часовой пояс")
        if value.get("url") is not None:
            VisibleHhBrowser._validate_verification_url(value["url"])
        if "show_window" in value and type(value["show_window"]) is not bool:
            raise ValueError("Некорректный режим окна браузера")
        return value

    def active(self, *, now: datetime | None = None) -> bool:
        return bool(self.read_active(now=now))

    def read_active(self, *, now: datetime | None = None) -> dict[str, Any]:
        value = self.read()
        if not value:
            return {}
        if datetime.fromisoformat(value["expires_at"]) > (now or datetime.now(UTC)):
            return value
        return {}

    def _write(self, value: dict[str, Any]) -> None:
        temporary = self.path.with_name(f".{uuid4().hex}.json")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf8") as stream:
                json.dump(value, stream, ensure_ascii=False)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def acquire(
        self,
        url: str | None,
        *,
        minutes: int = 30,
        now: datetime | None = None,
        show_window: bool = True,
    ) -> dict[str, Any]:
        if url is not None:
            VisibleHhBrowser._validate_verification_url(url)
        if not 1 <= minutes <= 60:
            raise ValueError("Время управления должно быть от 1 до 60 минут")
        if type(show_window) is not bool or (url is not None and not show_window):
            raise ValueError("Адрес ручного открытия требует видимого окна")
        selected = now or datetime.now(UTC)
        with self._locked():
            if self.active(now=selected):
                raise RuntimeError("Браузер уже передан другому исполнителю")
            value = {
                "token": uuid4().hex,
                "state": "requested",
                "url": url,
                "show_window": show_window,
                "expires_at": (selected + timedelta(minutes=minutes)).isoformat(),
            }
            self._write(value)
            return value

    def mark_ready(
        self, token: str, *, error: str | None = None, now: datetime | None = None
    ) -> bool:
        with self._locked():
            value = self.read_active(now=now)
            if value.get("token") != token:
                return False
            value["state"] = "failed" if error else "ready"
            if error:
                value["error"] = error
            self._write(value)
            return True

    def release(self, token: str, *, strict: bool = True) -> bool:
        with self._locked():
            value = self.read()
            if value and value.get("token") != token:
                if strict:
                    raise RuntimeError("Управление принадлежит другому исполнителю")
                return False
            self.path.unlink(missing_ok=True)
            return True
