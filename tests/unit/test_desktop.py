from __future__ import annotations

import socket
import subprocess
import time
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from urllib.error import URLError

import pytest

from hugin import desktop
from hugin.adapters.notification_credentials import TelegramGatewayCredentials
from hugin.adapters.telegram_gateway import (
    GatewayConnection,
    GatewayStatus,
    Pairing,
    TelegramGatewayAuthorizationError,
    TelegramGatewayError,
)
from hugin.core.settings import Settings
from hugin.domain import HhFormReviewResult, HhFormReviewStatus, HhScreeningForm
from hugin.domain.content import RecruiterMessageState
from hugin.services.hh_login import LoginResult, LoginStatus


@dataclass
class FakeDraft:
    application_id: int = 11
    form_id: int = 12
    source_url: str = "https://hh.ru/vacancy/101"
    resume_title: str = "Python"
    version_hash: str = "version-1"
    answers: dict[str, str] | None = None
    cover_letter: str | None = "Здравствуйте!"

    def __post_init__(self) -> None:
        if self.answers is None:
            self.answers = {"salary": "120000"}


class FakeSessions:
    def begin(self) -> object:
        return nullcontext(object())


class FakeDatabase:
    def __init__(self) -> None:
        self.sessions = FakeSessions()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeDraftService:
    draft: ClassVar[FakeDraft] = FakeDraft()
    error: ClassVar[Exception | None] = None
    captured: ClassVar[list[tuple[int, HhScreeningForm]]] = []
    invalidated: ClassVar[list[int]] = []

    def __init__(self, _session: object) -> None:
        pass

    def get_pending(self, _account_id: int, _vacancy_id: str) -> FakeDraft:
        if self.error is not None:
            raise self.error
        return self.draft

    def capture(self, application_id: int, form: HhScreeningForm) -> None:
        self.captured.append((application_id, form))

    def invalidate(self, form_id: int) -> None:
        self.invalidated.append(form_id)


class FakeBrowser:
    result: ClassVar[HhFormReviewResult] = HhFormReviewResult(
        HhFormReviewStatus.READY,
        "https://hh.ru/vacancy/101",
        filled_keys=("salary",),
        skipped_keys=("motivation",),
    )
    instances: ClassVar[list[FakeBrowser]] = []

    def __init__(self, *_args: object) -> None:
        self.closed = False
        self.opened_login = False
        self.instances.append(self)

    def __enter__(self) -> FakeBrowser:
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def open_login(self) -> None:
        self.opened_login = True

    def is_authenticated(self) -> bool:
        return True

    def open_screening_form(self, *_args: object, **_kwargs: object) -> HhFormReviewResult:
        return self.result


@pytest.fixture(autouse=True)
def reset_fakes() -> None:
    FakeDraftService.error = None
    FakeDraftService.captured = []
    FakeDraftService.invalidated = []
    FakeBrowser.instances = []
    FakeBrowser.result = HhFormReviewResult(
        HhFormReviewStatus.READY,
        "https://hh.ru/vacancy/101",
        filled_keys=("salary",),
        skipped_keys=("motivation",),
    )


def prepare_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> desktop.DesktopBridge:
    def create_database(_settings: Settings) -> FakeDatabase:
        return FakeDatabase()

    monkeypatch.setattr(desktop, "upgrade_database", lambda _settings: None)
    monkeypatch.setattr(desktop, "create_database", create_database)
    monkeypatch.setattr(desktop, "ScreeningDraftService", FakeDraftService)
    monkeypatch.setattr(desktop, "VisibleHhBrowser", FakeBrowser)
    return desktop.DesktopBridge(Settings(environment="test", data_dir=tmp_path))


def test_bridge_opens_saved_form_without_submitting_and_closes_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bridge = prepare_bridge(monkeypatch, tmp_path)

    result = bridge.open_form(" 101 ")
    second = bridge.open_form("101")

    assert result == {
        "status": "READY",
        "message": "Анкета заполнена и оставлена открытой без отправки",
        "filled": 1,
        "skipped": 1,
    }
    assert second["status"] == "READY"
    assert len(FakeBrowser.instances) == 2
    assert all(browser.opened_login for browser in FakeBrowser.instances)
    assert all(browser.closed for browser in FakeBrowser.instances)
    bridge.close()
    bridge.close()


def test_bridge_refreshes_changed_form_and_invalidates_closed_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bridge = prepare_bridge(monkeypatch, tmp_path)
    changed_form = HhScreeningForm(fields=())
    FakeBrowser.result = HhFormReviewResult(
        HhFormReviewStatus.FORM_CHANGED,
        "https://hh.ru/vacancy/101",
        current_form=changed_form,
    )

    changed = bridge.open_form("101")

    assert changed["status"] == "FORM_CHANGED"
    assert FakeDraftService.captured == [(11, changed_form)]

    FakeBrowser.result = HhFormReviewResult(
        HhFormReviewStatus.VACANCY_CLOSED,
        "https://hh.ru/vacancy/101",
        message="Вакансия закрыта",
    )
    closed = bridge.open_form("101")

    assert closed == {"status": "VACANCY_CLOSED", "message": "Вакансия закрыта"}
    assert FakeDraftService.invalidated == [12]
    bridge.close()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (LoginStatus.CREDENTIALS_REQUIRED, "Сначала сохраните данные входа hh.ru"),
        (LoginStatus.CONFIRMATION_REQUIRED, "Введите код в открытом окне браузера"),
        (LoginStatus.CAPTCHA_REQUIRED, "Пройдите проверку в открытом окне браузера"),
        (LoginStatus.INVALID_CREDENTIALS, "hh.ru отклонил логин или пароль"),
        (LoginStatus.MANUAL_ACTION_REQUIRED, "Завершите вход в открытом окне браузера"),
    ],
)
def test_bridge_explains_incomplete_login(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: LoginStatus,
    expected: str,
) -> None:
    bridge = prepare_bridge(monkeypatch, tmp_path)

    class FakeLoginService:
        def __init__(self, _store: object) -> None:
            pass

        def authenticate(self, _account_id: int, _browser: object) -> LoginResult:
            return LoginResult(status)

    monkeypatch.setattr(desktop, "HhLoginService", FakeLoginService)

    assert bridge.open_form("101") == {"status": status.value.upper(), "message": expected}
    bridge.close()


def test_bridge_rejects_bad_input_missing_draft_and_foreign_link(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bridge = prepare_bridge(monkeypatch, tmp_path)

    assert bridge.open_form("")["status"] == "UNAVAILABLE"
    assert bridge.open_form("x" * 65)["status"] == "UNAVAILABLE"
    FakeDraftService.error = LookupError("Черновик не найден")
    assert bridge.open_form("101") == {
        "status": "UNAVAILABLE",
        "message": "Черновик не найден",
    }
    assert bridge.open_url("https://example.com/")["status"] == "UNAVAILABLE"
    monkeypatch.setattr(webbrowser, "open", lambda *_args, **_kwargs: True)
    assert bridge.open_url(" https://hh.ru/vacancy/101 ")["status"] == "READY"
    monkeypatch.setattr(webbrowser, "open", lambda *_args, **_kwargs: False)
    assert bridge.open_url("https://www.hh.ru/vacancy/101")["status"] == "UNAVAILABLE"


def test_bridge_opens_only_saved_secure_invitation_link(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bridge = prepare_bridge(monkeypatch, tmp_path)

    class FakeCommunicationService:
        booking_url: ClassVar[str | None] = None

        def __init__(self, _session: object, _sender: object) -> None:
            pass

        def invitations(self, account_id: int) -> tuple[SimpleNamespace, ...]:
            assert account_id == 1
            return (SimpleNamespace(id=7, booking_url=self.booking_url),)

    monkeypatch.setattr(desktop, "CommunicationService", FakeCommunicationService)
    assert bridge.open_invitation(0)["status"] == "UNAVAILABLE"
    assert bridge.open_invitation(8)["status"] == "UNAVAILABLE"
    assert bridge.open_invitation(7)["status"] == "UNAVAILABLE"

    FakeCommunicationService.booking_url = "http://example.com/interview"
    assert bridge.open_invitation(7)["status"] == "UNAVAILABLE"
    FakeCommunicationService.booking_url = "https://user:secret@example.com/interview"
    assert bridge.open_invitation(7)["status"] == "UNAVAILABLE"

    opened: list[str] = []

    def open_link(url: str, **_kwargs: object) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", open_link)
    FakeCommunicationService.booking_url = " https://calendar.example.com/interview "
    assert bridge.open_invitation(7)["status"] == "READY"
    assert opened == ["https://calendar.example.com/interview"]


def test_bridge_sends_only_exact_confirmed_reply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ReplySession:
        def scalar(self, _statement: object) -> str:
            return "https://hh.ru/vacancy/101"

    class ReplySessions:
        def begin(self) -> object:
            return nullcontext(ReplySession())

    class ReplyDatabase:
        def __init__(self) -> None:
            self.sessions = ReplySessions()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeCommunicationService:
        def __init__(self, _session: object, _sender: object) -> None:
            pass

        def messages(self, _account_id: int) -> tuple[SimpleNamespace, ...]:
            return (
                SimpleNamespace(
                    id=7,
                    application_id=11,
                    content_hash="a" * 64,
                    content_version=2,
                    state=RecruiterMessageState.CONFIRMED,
                ),
            )

        def send_confirmed(self, **values: object) -> SimpleNamespace:
            assert values == {
                "account_id": 1,
                "message_id": 7,
                "content_version": 2,
                "content_hash": "a" * 64,
            }
            return SimpleNamespace(state=RecruiterMessageState.SENT)

    monkeypatch.setattr(desktop, "upgrade_database", lambda _settings: None)
    monkeypatch.setattr(desktop, "create_database", lambda _settings: ReplyDatabase())
    monkeypatch.setattr(desktop, "CommunicationService", FakeCommunicationService)
    monkeypatch.setattr(desktop, "VisibleHhBrowser", FakeBrowser)
    bridge = desktop.DesktopBridge(Settings(environment="test", data_dir=tmp_path))

    assert bridge.send_reply(7, "bad", 2)["status"] == "UNAVAILABLE"
    assert bridge.send_reply(7, "a" * 64, 2) == {
        "status": "SENT",
        "message": "Ответ отправлен и сохранён.",
    }


def test_bridge_generates_editable_reply_draft(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ReplySessions:
        def begin(self) -> object:
            return nullcontext(object())

    class ReplyDatabase:
        sessions = ReplySessions()

        def close(self) -> None:
            return None

    class FakeReplyService:
        def __init__(self, _session: object, model: object) -> None:
            assert model == "configured-model"

        def generate(self, **values: int) -> SimpleNamespace:
            assert values == {"account_id": 1, "application_id": 12}
            return SimpleNamespace(body="Подготовленный ответ")

    monkeypatch.setattr(desktop, "upgrade_database", lambda _settings: None)
    monkeypatch.setattr(desktop, "create_database", lambda _settings: ReplyDatabase())
    monkeypatch.setattr(
        desktop,
        "AiPromptSettingsService",
        lambda _session: SimpleNamespace(
            get_model=lambda: "selected-model",
            get_reasoning_effort=lambda: "high",
        ),
    )
    monkeypatch.setattr(
        desktop,
        "configured_yandex_ai_client",
        lambda _settings, *, model, reasoning_effort, operation: (
            "configured-model"
            if model == "selected-model"
            and reasoning_effort == "high"
            and operation == "recruiter_reply"
            else None
        ),
    )
    monkeypatch.setattr(desktop, "RecruiterReplyService", FakeReplyService)
    bridge = desktop.DesktopBridge(Settings(environment="test", data_dir=tmp_path))

    assert bridge.generate_reply(0)["status"] == "UNAVAILABLE"
    assert bridge.generate_reply(12) == {
        "status": "READY",
        "message": "Черновик подготовлен. Проверьте и при необходимости измените его.",
        "body": "Подготовленный ответ",
    }

    monkeypatch.setattr(
        desktop,
        "configured_yandex_ai_client",
        lambda _settings, *, model, reasoning_effort, operation: (_ for _ in ()).throw(
            LookupError(f"YandexGPT не настроен: {model}, режим {reasoning_effort}")
        ),
    )
    assert bridge.generate_reply(12) == {
        "status": "UNAVAILABLE",
        "message": "YandexGPT не настроен: selected-model, режим high",
    }


def test_bridge_saves_notification_credentials_in_windows_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values: dict[str, object] = {
        "telegram": TelegramGatewayCredentials(
            "hgt_connectionid.abcdefghijklmnopqrstuvwxyzABCDEFG"
        ),
    }

    class Store:
        def load_telegram_gateway(self) -> object | None:
            return values.get("telegram")

        def load_email(self) -> object | None:
            return values.get("email")

        def delete_telegram(self) -> bool:
            return values.pop("telegram", None) is not None

        def save_email(self, credentials: object) -> None:
            values["email"] = credentials

    class Gateway:
        def __init__(self, _url: str, *, timeout_seconds: int) -> None:
            assert timeout_seconds == 15

        def status(self) -> GatewayStatus:
            return GatewayStatus(True, "hugin_workbot")

        def connection_status(self, _access_token: str) -> GatewayStatus:
            return GatewayStatus(True, "hugin_workbot")

        def disconnect(self, _access_token: str) -> None:
            pass

    monkeypatch.setattr(desktop, "WindowsNotificationCredentialStore", Store)
    monkeypatch.setattr(desktop, "TelegramGatewayClient", Gateway)
    bridge = desktop.DesktopBridge(Settings(environment="test", data_dir=tmp_path))

    initial = bridge.notification_credentials_status()
    assert initial["telegram_bot_configured"] is True
    assert initial["telegram_configured"] is True
    assert initial["telegram_bot_username"] == "hugin_workbot"
    assert (
        bridge.save_email_notifications(
            "smtp.example.com",
            587,
            "user",
            "password",
            "from@example.com",
            "to@example.com",
            True,
        )["status"]
        == "READY"
    )
    status = bridge.notification_credentials_status()
    assert status["telegram_configured"] is True
    assert status["email_configured"] is True
    assert bridge.disconnect_telegram_notifications()["status"] == "READY"
    assert "telegram" not in values


def test_bridge_connects_telegram_through_safe_one_time_start_link(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values: dict[str, object] = {}
    events: list[object] = []
    access_token = "hgt_connectionid.abcdefghijklmnopqrstuvwxyzABCDEFG"
    pairing = Pairing(
        "pairing-id",
        "pairing-secret",
        "https://t.me/hugin_workbot?start=one_time",
        "hugin_workbot",
    )

    class Store:
        def save_telegram_gateway(self, credentials: object) -> None:
            values["telegram"] = credentials

    class Gateway:
        def __init__(self, url: str, *, timeout_seconds: int) -> None:
            assert url == "http://127.0.0.1:8020"
            assert timeout_seconds == 15

        def create_pairing(self) -> Pairing:
            events.append("create")
            return pairing

        def wait_for_connection(
            self,
            selected: Pairing,
            *,
            timeout_seconds: int,
        ) -> GatewayConnection:
            events.append(("wait", selected.pairing_id, timeout_seconds))
            return GatewayConnection(access_token, "hugin_workbot")

    def open_link(url: str, *, new: int) -> bool:
        events.append(("open", url, new))
        return True

    monkeypatch.setattr(desktop, "WindowsNotificationCredentialStore", Store)
    monkeypatch.setattr(desktop, "TelegramGatewayClient", Gateway)
    monkeypatch.setattr("hugin.desktop.webbrowser.open", open_link)
    bridge = desktop.DesktopBridge(Settings(environment="test", data_dir=tmp_path))

    assert bridge.connect_telegram_notifications()["status"] == "READY"
    assert events == [
        "create",
        ("open", "https://t.me/hugin_workbot?start=one_time", 2),
        ("wait", "pairing-id", 120),
    ]
    assert values["telegram"] == TelegramGatewayCredentials(access_token)


def test_bridge_does_not_bind_failed_telegram_chat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values: dict[str, object] = {}
    pairing = Pairing(
        "pairing-id",
        "pairing-secret",
        "https://t.me/hugin_workbot?start=one_time",
        "hugin_workbot",
    )

    class Store:
        def save_telegram_gateway(self, credentials: object) -> None:
            values["telegram"] = credentials

    class FailingGateway:
        def __init__(self, _url: str, *, timeout_seconds: int) -> None:
            assert timeout_seconds == 15

        def create_pairing(self) -> Pairing:
            return pairing

        def wait_for_connection(
            self,
            _pairing: Pairing,
            *,
            timeout_seconds: int,
        ) -> GatewayConnection:
            assert timeout_seconds == 120
            raise TelegramGatewayError("Служба не подтвердила подключение")

    monkeypatch.setattr(desktop, "WindowsNotificationCredentialStore", Store)
    monkeypatch.setattr(desktop, "TelegramGatewayClient", FailingGateway)
    monkeypatch.setattr("hugin.desktop.webbrowser.open", lambda _url, *, new: new == 2)
    bridge = desktop.DesktopBridge(Settings(environment="test", data_dir=tmp_path))
    failed = bridge.connect_telegram_notifications()
    assert failed["status"] == "UNAVAILABLE"
    assert "telegram" not in values


def test_bridge_reports_unavailable_when_shared_telegram_bot_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Store:
        def load_telegram_gateway(self) -> None:
            return None

        def load_email(self) -> None:
            return None

    class Gateway:
        def __init__(self, _url: str, *, timeout_seconds: int) -> None:
            assert timeout_seconds == 15

        def status(self) -> GatewayStatus:
            return GatewayStatus(False, "hugin_workbot")

        def create_pairing(self) -> Pairing:
            raise TelegramGatewayError("Служба Telegram ещё не готова")

    monkeypatch.setattr(desktop, "WindowsNotificationCredentialStore", Store)
    monkeypatch.setattr(desktop, "TelegramGatewayClient", Gateway)
    monkeypatch.setattr(
        "hugin.desktop.webbrowser.open",
        lambda *_args, **_kwargs: pytest.fail("Telegram must not open before gateway is ready"),
    )
    bridge = desktop.DesktopBridge(Settings(environment="test", data_dir=tmp_path))

    result = bridge.connect_telegram_notifications()

    assert result["status"] == "UNAVAILABLE"
    assert "ещё не готова" in str(result["message"])


def test_bridge_keeps_email_status_when_telegram_service_is_offline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Store:
        def load_telegram_gateway(self) -> None:
            return None

        def load_email(self) -> object:
            return object()

    class Gateway:
        def __init__(self, _url: str, *, timeout_seconds: int) -> None:
            assert timeout_seconds == 15

        def status(self) -> GatewayStatus:
            raise TelegramGatewayError("offline")

    monkeypatch.setattr(desktop, "WindowsNotificationCredentialStore", Store)
    monkeypatch.setattr(desktop, "TelegramGatewayClient", Gateway)
    bridge = desktop.DesktopBridge(Settings(environment="test", data_dir=tmp_path))

    result = bridge.notification_credentials_status()

    assert result["status"] == "READY"
    assert result["telegram_bot_configured"] is False
    assert result["email_configured"] is True


def test_bridge_removes_revoked_gateway_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deleted: list[bool] = []

    class Store:
        def load_telegram_gateway(self) -> TelegramGatewayCredentials:
            return TelegramGatewayCredentials("hgt_connectionid.abcdefghijklmnopqrstuvwxyzABCDEFG")

        def load_email(self) -> None:
            return None

        def delete_telegram(self) -> bool:
            deleted.append(True)
            return True

    class Gateway:
        def __init__(self, _url: str, *, timeout_seconds: int) -> None:
            assert timeout_seconds == 15

        def status(self) -> GatewayStatus:
            return GatewayStatus(True, "hugin_workbot")

        def connection_status(self, _access_token: str) -> GatewayStatus:
            raise TelegramGatewayAuthorizationError("revoked")

    monkeypatch.setattr(desktop, "WindowsNotificationCredentialStore", Store)
    monkeypatch.setattr(desktop, "TelegramGatewayClient", Gateway)
    bridge = desktop.DesktopBridge(Settings(environment="test", data_dir=tmp_path))

    result = bridge.notification_credentials_status()

    assert result["telegram_bot_configured"] is True
    assert result["telegram_configured"] is False
    assert deleted == [True]


def test_bridge_does_not_wait_when_telegram_link_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    waited: list[bool] = []

    class Store:
        pass

    class Gateway:
        def __init__(self, _url: str, *, timeout_seconds: int) -> None:
            assert timeout_seconds == 15

        def create_pairing(self) -> Pairing:
            return Pairing(
                "pairing-id",
                "pairing-secret",
                "https://t.me/hugin_workbot?start=one_time",
                "hugin_workbot",
            )

        def wait_for_connection(
            self,
            _pairing: Pairing,
            *,
            timeout_seconds: int,
        ) -> GatewayConnection:
            waited.append(True)
            raise AssertionError(timeout_seconds)

    monkeypatch.setattr(desktop, "WindowsNotificationCredentialStore", Store)
    monkeypatch.setattr(desktop, "TelegramGatewayClient", Gateway)
    monkeypatch.setattr("hugin.desktop.webbrowser.open", lambda _url, *, new: new != 2)
    bridge = desktop.DesktopBridge(Settings(environment="test", data_dir=tmp_path))

    result = bridge.connect_telegram_notifications()

    assert result["status"] == "UNAVAILABLE"
    assert waited == []


def test_project_directory_and_health_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    child = root / "src" / "hugin"
    child.mkdir(parents=True)
    (root / "compose.yaml").write_text("services: {}", encoding="utf-8")

    assert desktop.project_directory(child) == root
    with pytest.raises(RuntimeError, match=r"compose\.yaml"):
        desktop.project_directory(tmp_path / "missing")

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    class Opener:
        def open(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr(desktop, "build_opener", lambda *_args: Opener())
    assert desktop.api_is_ready("http://127.0.0.1:8010/")

    class FailingOpener:
        def open(self, *_args: object, **_kwargs: object) -> Response:
            raise URLError("нет связи")

    monkeypatch.setattr(desktop, "build_opener", lambda *_args: FailingOpener())
    assert not desktop.api_is_ready("http://127.0.0.1:8010")


def test_ensure_services_starts_docker_and_reports_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(environment="test")
    monkeypatch.setattr(desktop, "project_directory", lambda: tmp_path)
    backups: list[tuple[object, ...]] = []

    class Backup:
        def __init__(self, selected: Settings, *, adapter: object) -> None:
            backups.append(("init", selected, adapter))

        def create(self, reason: str) -> None:
            backups.append(("create", reason))

    adapter = object()
    monkeypatch.setattr(desktop, "BackupService", Backup)
    monkeypatch.setattr(
        desktop,
        "DockerPostgresBackupAdapter",
        lambda root: adapter if root == tmp_path else None,
    )
    readiness = iter((False, True))
    monkeypatch.setattr(desktop, "api_is_ready", lambda _url: next(readiness))
    calls: list[tuple[list[str], Path, bool, bool, int]] = []

    def run(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool,
        text: bool,
        creationflags: int,
    ) -> SimpleNamespace:
        assert not check
        calls.append((command, cwd, capture_output, text, creationflags))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    desktop.ensure_services(settings)
    assert calls == [
        (
            ["docker", "compose", "up", "--detach", "--wait", "db"],
            tmp_path,
            True,
            True,
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        ),
        (
            ["docker", "compose", "up", "--detach", "--build", "--wait"],
            tmp_path,
            True,
            True,
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        ),
    ]
    assert backups == [
        ("init", settings, adapter),
        ("create", "pre-update"),
    ]

    monkeypatch.setattr(desktop, "api_is_ready", lambda _url: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )
    with pytest.raises(RuntimeError, match="Не удалось запустить"):
        desktop.ensure_services(settings)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="не стал доступен"):
        desktop.ensure_services(settings, timeout_seconds=1)


def test_main_starts_window_and_always_closes_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = Settings(environment="development", data_dir=tmp_path)
    events: list[object] = []
    created: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeWebview:
        def create_window(self, *args: object, **kwargs: object) -> object:
            created.append((args, kwargs))
            return object()

        def start(self, *, debug: bool = False) -> None:
            events.append(("start", debug))

    class FakeBridge:
        def __init__(self, selected: Settings, **_kwargs: object) -> None:
            assert selected is settings

        def close(self) -> None:
            events.append("close")

    class FakeWorker:
        def __init__(self, selected: Settings, **_kwargs: object) -> None:
            assert selected is settings

        def start(self) -> None:
            events.append("worker-start")

        def stop(self) -> None:
            events.append("worker-stop")

    monkeypatch.setattr(desktop, "get_settings", lambda: settings)
    monkeypatch.setattr(desktop, "ensure_services", lambda _settings: events.append("services"))
    monkeypatch.setattr(desktop, "import_module", lambda _name: FakeWebview())
    monkeypatch.setattr(desktop, "DesktopBridge", FakeBridge)
    monkeypatch.setattr(desktop, "AutomationWorker", FakeWorker)
    monkeypatch.setattr(desktop, "ApplicationWorker", FakeWorker)
    monkeypatch.setattr(desktop, "NotificationWorker", FakeWorker)
    monkeypatch.setattr(desktop, "BackupWorker", FakeWorker)

    desktop.main()

    assert events[0] == "services"
    assert events[-6:] == [
        ("start", False),
        "close",
        "worker-stop",
        "worker-stop",
        "worker-stop",
        "worker-stop",
    ]
    assert created[0][0] == ("Hugin — поиск работы", settings.desktop_api_url)


def test_main_explains_missing_window_dependency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        desktop,
        "get_settings",
        lambda: Settings(environment="test", data_dir=tmp_path),
    )
    monkeypatch.setattr(desktop, "ensure_services", lambda _settings: None)
    monkeypatch.setattr(
        desktop,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("missing")),
    )

    with pytest.raises(RuntimeError, match="оконную часть"):
        desktop.main()


def test_single_instance_rejects_second_copy() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()

    with (
        desktop.single_desktop_instance(port),
        pytest.raises(RuntimeError, match="уже запущен"),
        desktop.single_desktop_instance(port),
    ):
        pass


def test_launch_shows_error_instead_of_leaving_hidden_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    @contextmanager
    def instance() -> Iterator[None]:
        events.append("lock")
        yield

    monkeypatch.setattr(desktop, "single_desktop_instance", instance)
    monkeypatch.setattr(
        desktop,
        "get_settings",
        lambda: Settings(environment="test", data_dir=tmp_path),
    )
    monkeypatch.setattr(
        desktop,
        "main",
        lambda: (_ for _ in ()).throw(RuntimeError("Docker не запущен")),
    )
    monkeypatch.setattr(desktop, "show_launch_error", events.append)

    desktop.launch()

    assert events == ["lock", "Docker не запущен"]
