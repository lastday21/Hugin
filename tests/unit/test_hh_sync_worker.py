from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest

from hugin.core.settings import Settings
from hugin.domain.automation import (
    AutomationJobKind,
    AutomationJobRecord,
    AutomationJobState,
)
from hugin.domain.content import MessageDirection
from hugin.domain.hh_sync import (
    HhChatMessageData,
    HhNegotiationData,
    HhNegotiationStatus,
    HhSyncBlockedError,
)
from hugin.domain.tasks import SystemState
from hugin.services.hh_login import LoginStatus
from hugin.workers import hh_sync as worker_module
from hugin.workers.automation import AutomationJobBlocked


def make_job(kind: AutomationJobKind) -> AutomationJobRecord:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    return AutomationJobRecord(
        key=f"{kind.value.lower()}:1",
        kind=kind,
        state=AutomationJobState.RUNNING,
        account_id=1,
        search_query_id=None,
        interval_seconds=300,
        next_run_at=now,
        last_started_at=now,
        last_finished_at=None,
        last_success_at=None,
        heartbeat_at=now,
        consecutive_failures=0,
        last_error_code=None,
        last_error_message=None,
        last_result={},
        created_at=now,
        updated_at=now,
    )


class FakeBrowser:
    messages: ClassVar[tuple[HhChatMessageData, ...]] = ()
    statuses: ClassVar[tuple[HhNegotiationData, ...]] = ()
    requested_ids: ClassVar[tuple[str, ...] | None] = None

    def __init__(self, *_args: object) -> None:
        pass

    def __enter__(self) -> FakeBrowser:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read_recruiter_messages(
        self,
        vacancy_ids: tuple[str, ...],
    ) -> tuple[HhChatMessageData, ...]:
        type(self).requested_ids = vacancy_ids
        return self.messages

    def read_application_statuses(self) -> tuple[HhNegotiationData, ...]:
        return self.statuses


class FakeLoginService:
    status = LoginStatus.AUTHENTICATED

    def __init__(self, _credentials: object) -> None:
        pass

    def authenticate(self, account_id: int, browser: object) -> SimpleNamespace:
        assert account_id == 1
        assert isinstance(browser, FakeBrowser)
        return SimpleNamespace(
            authenticated=self.status is LoginStatus.AUTHENTICATED,
            status=self.status,
        )


def prepare_handler(
    monkeypatch: pytest.MonkeyPatch,
    kind: AutomationJobKind,
) -> worker_module.HhSyncJobHandler:
    monkeypatch.setattr(worker_module, "VisibleHhBrowser", FakeBrowser)
    monkeypatch.setattr(worker_module, "HhLoginService", FakeLoginService)
    handler = worker_module.HhSyncJobHandler(Settings(environment="test"), kind)
    monkeypatch.setattr(handler, "_tracked_vacancy_ids", lambda: ("101",))
    return handler


def test_message_handler_reads_only_tracked_chats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = HhChatMessageData(
        vacancy_id="101",
        hh_id="message-1",
        direction=MessageDirection.INCOMING,
        body="Здравствуйте!",
    )
    FakeBrowser.messages = (message,)
    handler = prepare_handler(monkeypatch, AutomationJobKind.MESSAGES)
    monkeypatch.setattr(
        handler,
        "_synchronize_messages",
        lambda messages: {"created": len(messages)},
    )

    result = handler(make_job(AutomationJobKind.MESSAGES))

    assert result == {"created": 1}
    assert FakeBrowser.requested_ids == ("101",)


def test_status_handler_passes_statuses_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = HhNegotiationData(
        "101",
        HhNegotiationStatus.VIEWED,
        "Просмотрен",
    )
    FakeBrowser.statuses = (status,)
    handler = prepare_handler(monkeypatch, AutomationJobKind.STATUSES)
    monkeypatch.setattr(
        handler,
        "_synchronize_statuses",
        lambda statuses: {"updated": len(statuses)},
    )

    assert handler(make_job(AutomationJobKind.STATUSES)) == {"updated": 1}


def test_lost_login_blocks_job_and_protects_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeLoginService.status = LoginStatus.CAPTCHA_REQUIRED
    handler = prepare_handler(monkeypatch, AutomationJobKind.MESSAGES)
    protected: list[SystemState] = []
    monkeypatch.setattr(handler, "_protect_system", protected.append)

    with pytest.raises(AutomationJobBlocked) as error:
        handler(make_job(AutomationJobKind.MESSAGES))

    assert error.value.code == "CAPTCHA_REQUIRED"
    assert protected == [SystemState.CAPTCHA_REQUIRED]
    FakeLoginService.status = LoginStatus.AUTHENTICATED


def test_account_warning_from_page_blocks_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = prepare_handler(monkeypatch, AutomationJobKind.STATUSES)
    protected: list[SystemState] = []
    monkeypatch.setattr(handler, "_protect_system", protected.append)
    monkeypatch.setattr(
        FakeBrowser,
        "read_application_statuses",
        lambda _browser: (_ for _ in ()).throw(
            HhSyncBlockedError("ACCOUNT_WARNING", "hh.ru показал предупреждение")
        ),
    )

    with pytest.raises(AutomationJobBlocked) as error:
        handler(make_job(AutomationJobKind.STATUSES))

    assert error.value.code == "ACCOUNT_WARNING"
    assert protected == [SystemState.ACCOUNT_WARNING]


def test_handler_database_helpers_close_every_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSessions:
        def __call__(self) -> object:
            return nullcontext(object())

        def begin(self) -> object:
            return nullcontext(object())

    class FakeDatabase:
        def __init__(self) -> None:
            self.sessions = FakeSessions()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    databases: list[FakeDatabase] = []

    class FakeSynchronizationService:
        def __init__(self, _session: object) -> None:
            pass

        def tracked_vacancy_ids(self, account_id: int) -> tuple[str, ...]:
            assert account_id == 1
            return ("101", "202")

        def synchronize_messages(self, **values: object) -> dict[str, int]:
            assert values["account_id"] == 1
            return {"created": len(cast(tuple[object, ...], values["messages"]))}

        def synchronize_statuses(self, **values: object) -> dict[str, int]:
            assert values["account_id"] == 1
            return {"updated": len(cast(tuple[object, ...], values["statuses"]))}

    def create_database(_settings: Settings) -> FakeDatabase:
        database = FakeDatabase()
        databases.append(database)
        return database

    monkeypatch.setattr(worker_module, "create_database", create_database)
    monkeypatch.setattr(
        worker_module,
        "HhSynchronizationService",
        FakeSynchronizationService,
    )
    handler = worker_module.HhSyncJobHandler(
        Settings(environment="test"),
        AutomationJobKind.MESSAGES,
    )
    message = HhChatMessageData(
        "101",
        "message-1",
        MessageDirection.INCOMING,
        "Здравствуйте!",
    )
    status = HhNegotiationData("101", HhNegotiationStatus.VIEWED, "Просмотрен")

    assert handler._tracked_vacancy_ids() == ("101", "202")
    assert handler._synchronize_messages((message,)) == {"created": 1}
    assert handler._synchronize_statuses((status,)) == {"updated": 1}
    assert len(databases) == 3
    assert all(database.closed for database in databases)


def test_handler_rejects_wrong_job() -> None:
    handler = worker_module.HhSyncJobHandler(
        Settings(environment="test"),
        AutomationJobKind.MESSAGES,
    )
    with pytest.raises(ValueError, match="неподходящее"):
        handler(make_job(AutomationJobKind.STATUSES))
    with pytest.raises(ValueError, match="другому аккаунту"):
        handler(replace(make_job(AutomationJobKind.MESSAGES), account_id=2))
