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


def prepare_bridge(monkeypatch: pytest.MonkeyPatch) -> desktop.DesktopBridge:
    def create_database(_settings: Settings) -> FakeDatabase:
        return FakeDatabase()

    monkeypatch.setattr(desktop, "upgrade_database", lambda _settings: None)
    monkeypatch.setattr(desktop, "create_database", create_database)
    monkeypatch.setattr(desktop, "ScreeningDraftService", FakeDraftService)
    monkeypatch.setattr(desktop, "VisibleHhBrowser", FakeBrowser)
    return desktop.DesktopBridge(Settings(environment="test"))


def test_bridge_opens_saved_form_without_submitting_and_closes_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = prepare_bridge(monkeypatch)

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
) -> None:
    bridge = prepare_bridge(monkeypatch)
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
    status: LoginStatus,
    expected: str,
) -> None:
    bridge = prepare_bridge(monkeypatch)

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
) -> None:
    bridge = prepare_bridge(monkeypatch)

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
) -> None:
    bridge = prepare_bridge(monkeypatch)

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
    bridge = desktop.DesktopBridge(Settings(environment="test"))

    assert bridge.send_reply(7, "bad", 2)["status"] == "UNAVAILABLE"
    assert bridge.send_reply(7, "a" * 64, 2) == {
        "status": "SENT",
        "message": "Ответ отправлен и сохранён.",
    }


def test_bridge_saves_notification_credentials_in_windows_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[object] = []

    class Store:
        def load_telegram(self) -> object | None:
            return saved[0] if saved else None

        def load_email(self) -> object | None:
            return saved[1] if len(saved) > 1 else None

        def save_telegram(self, credentials: object) -> None:
            saved.append(credentials)

        def save_email(self, credentials: object) -> None:
            while len(saved) < 1:
                saved.append(None)
            saved.append(credentials)

    monkeypatch.setattr(desktop, "WindowsNotificationCredentialStore", Store)
    bridge = desktop.DesktopBridge(Settings(environment="test"))

    assert bridge.notification_credentials_status()["telegram_configured"] is False
    assert bridge.save_telegram_notifications("bot-token", "-100123")["status"] == "READY"
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
            ["docker", "compose", "up", "--detach", "--build", "--wait"],
            tmp_path,
            True,
            True,
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
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


def test_main_starts_window_and_always_closes_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(environment="development")
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

    desktop.main()

    assert events[0] == "services"
    assert events[-5:] == [
        ("start", False),
        "close",
        "worker-stop",
        "worker-stop",
        "worker-stop",
    ]
    assert created[0][0] == ("Hugin — поиск работы", settings.desktop_api_url)


def test_main_explains_missing_window_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "get_settings", lambda: Settings(environment="test"))
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
) -> None:
    events: list[str] = []

    @contextmanager
    def instance() -> Iterator[None]:
        events.append("lock")
        yield

    monkeypatch.setattr(desktop, "single_desktop_instance", instance)
    monkeypatch.setattr(
        desktop,
        "main",
        lambda: (_ for _ in ()).throw(RuntimeError("Docker не запущен")),
    )
    monkeypatch.setattr(desktop, "show_launch_error", events.append)

    desktop.launch()

    assert events == ["lock", "Docker не запущен"]
