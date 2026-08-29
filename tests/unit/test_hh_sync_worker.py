from __future__ import annotations

import threading
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.domain.automation import (
    AutomationJobKind,
    AutomationJobRecord,
    AutomationJobState,
)
from hugin.domain.communications import MessageSendOutcome, MessageSendResult
from hugin.domain.content import MessageDirection, RecruiterMessageState
from hugin.domain.hh_sync import (
    HhChatMessageData,
    HhChatReadFailure,
    HhNegotiationData,
    HhNegotiationStatus,
    HhRecruiterMessagesReadResult,
    HhSyncBlockedError,
    HhSyncRetryableError,
)
from hugin.domain.tasks import SystemState
from hugin.domain.vacancies import VacancyData
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    ResumeRepository,
    VacancyRepository,
)
from hugin.repositories.communications import CommunicationRepository
from hugin.services.autonomy import DEFAULT_AUTONOMY_POLICY, AutonomyPolicyService
from hugin.services.hh_login import LoginStatus
from hugin.workers import hh_sync as worker_module
from hugin.workers.automation import (
    AutomationJobBlocked,
    AutomationJobDeferred,
    AutomationJobRetry,
)


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
    message_failures: ClassVar[tuple[HhChatReadFailure, ...]] = ()
    statuses: ClassVar[tuple[HhNegotiationData, ...]] = ()
    requested_ids: ClassVar[tuple[str, ...] | None] = None
    initialization_options: ClassVar[list[dict[str, object]]] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        type(self).initialization_options.append(dict(_kwargs))

    def __enter__(self) -> FakeBrowser:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read_recruiter_messages(
        self,
        vacancy_ids: tuple[str, ...],
    ) -> HhRecruiterMessagesReadResult:
        type(self).requested_ids = vacancy_ids
        return HhRecruiterMessagesReadResult(
            messages=self.messages,
            failures=self.message_failures,
        )

    def read_application_statuses(self) -> tuple[HhNegotiationData, ...]:
        return self.statuses


class FakeLoginService:
    status = LoginStatus.AUTHENTICATED
    credential_store: ClassVar[object | None] = None
    authentication_calls: ClassVar[list[int]] = []
    observation_calls: ClassVar[list[int]] = []

    def __init__(self, credentials: object) -> None:
        type(self).credential_store = credentials

    def authenticate(self, account_id: int, browser: object) -> SimpleNamespace:
        assert account_id == 1
        assert isinstance(browser, FakeBrowser)
        type(self).authentication_calls.append(account_id)
        return SimpleNamespace(
            authenticated=self.status is LoginStatus.AUTHENTICATED,
            status=self.status,
        )

    def observe_authentication(self, account_id: int, browser: object) -> SimpleNamespace:
        assert account_id == 1
        assert isinstance(browser, FakeBrowser)
        type(self).observation_calls.append(account_id)
        return SimpleNamespace(
            authenticated=self.status is LoginStatus.AUTHENTICATED,
            status=self.status,
        )


def prepare_handler(
    monkeypatch: pytest.MonkeyPatch,
    kind: AutomationJobKind,
    settings: Settings | None = None,
    *,
    browser_lock: threading.Lock | None = None,
) -> worker_module.HhSyncJobHandler:
    monkeypatch.setattr(worker_module, "VisibleHhBrowser", FakeBrowser)
    monkeypatch.setattr(worker_module, "HhLoginService", FakeLoginService)
    monkeypatch.setattr(FakeBrowser, "initialization_options", [])
    monkeypatch.setattr(FakeLoginService, "authentication_calls", [])
    monkeypatch.setattr(FakeLoginService, "observation_calls", [])
    handler = worker_module.HhSyncJobHandler(
        settings or Settings(environment="test"),
        kind,
        browser_lock=browser_lock,
    )
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
    reply_modes: list[bool] = []

    def synchronize(
        messages: tuple[HhChatMessageData, ...],
        *,
        allow_replies: bool,
    ) -> dict[str, int]:
        assert not handler._browser_lock.locked()
        reply_modes.append(allow_replies)
        return {"created": len(messages)}

    monkeypatch.setattr(
        handler,
        "_synchronize_messages",
        synchronize,
    )

    first_job = make_job(AutomationJobKind.MESSAGES)
    result = handler(first_job)
    handler(replace(first_job, last_success_at=first_job.last_started_at))
    handler(
        replace(
            first_job,
            last_result={"message_baseline_initialized": True},
        )
    )

    assert result == {"created": 1, "message_baseline_initialized": True}
    assert FakeBrowser.requested_ids == ("101",)
    assert reply_modes == [False, False, True]


def test_message_handler_records_bad_chat_without_losing_other_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    national_lottery_message = HhChatMessageData(
        vacancy_id="136354935",
        hh_id="national-lottery-message",
        direction=MessageDirection.INCOMING,
        body="Расскажите про свой опыт.",
    )
    monkeypatch.setattr(FakeBrowser, "messages", (national_lottery_message,))
    monkeypatch.setattr(
        FakeBrowser,
        "message_failures",
        (
            HhChatReadFailure(
                vacancy_id="135428288",
                code="HH_CHAT_EMPTY",
                message=("Переписка открылась, но hh.ru не отдал сообщения"),
            ),
        ),
    )
    handler = prepare_handler(monkeypatch, AutomationJobKind.MESSAGES)
    monkeypatch.setattr(
        handler,
        "_tracked_vacancy_ids",
        lambda: ("135428288", "136354935"),
    )
    synchronized: list[tuple[HhChatMessageData, ...]] = []

    def synchronize(
        messages: tuple[HhChatMessageData, ...],
        *,
        allow_replies: bool,
    ) -> dict[str, int]:
        assert not allow_replies
        synchronized.append(messages)
        return {"created": len(messages)}

    monkeypatch.setattr(handler, "_synchronize_messages", synchronize)

    result = handler(make_job(AutomationJobKind.MESSAGES))

    assert synchronized == [(national_lottery_message,)]
    assert result["created"] == 1
    assert result["message_chats_failed"] == 1
    assert result["message_chat_failure_ids"] == "135428288"
    assert "HH_CHAT_EMPTY" in str(result["message_chat_failure_details"])


def test_sync_handler_defers_before_browser_when_application_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "VisibleHhBrowser", FakeBrowser)
    handler = worker_module.HhSyncJobHandler(
        Settings(environment="test"),
        AutomationJobKind.MESSAGES,
        application_work_pending=lambda: True,
    )
    monkeypatch.setattr(
        handler,
        "_tracked_vacancy_ids",
        lambda: pytest.fail("При готовом отклике чтение сообщений начинать нельзя"),
    )

    with pytest.raises(AutomationJobDeferred, match="готовый отклик") as raised:
        handler(make_job(AutomationJobKind.MESSAGES))

    assert raised.value.code == "APPLICATION_READY"
    assert raised.value.retry_after_seconds == 60


def test_sync_handler_yields_after_login_if_application_became_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "VisibleHhBrowser", FakeBrowser)
    monkeypatch.setattr(worker_module, "HhLoginService", FakeLoginService)
    readiness = iter((False, True))
    handler = worker_module.HhSyncJobHandler(
        Settings(environment="test"),
        AutomationJobKind.MESSAGES,
        application_work_pending=lambda: next(readiness),
    )
    monkeypatch.setattr(handler, "_tracked_vacancy_ids", lambda: ("101",))
    monkeypatch.setattr(
        FakeBrowser,
        "read_recruiter_messages",
        lambda *_args: pytest.fail("После появления готового отклика сообщения читать нельзя"),
    )

    with pytest.raises(AutomationJobDeferred, match="готовый отклик"):
        handler(make_job(AutomationJobKind.MESSAGES))


def test_sync_handler_quickly_defers_when_browser_profile_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = prepare_handler(monkeypatch, AutomationJobKind.MESSAGES)

    def fail_enter(_browser: FakeBrowser) -> FakeBrowser:
        raise RuntimeError("Профиль hh.ru занят другой задачей дольше допустимого времени")

    monkeypatch.setattr(FakeBrowser, "__enter__", fail_enter)

    with pytest.raises(AutomationJobDeferred) as raised:
        handler(make_job(AutomationJobKind.MESSAGES))

    assert raised.value.code == "BROWSER_PROFILE_BUSY"
    assert raised.value.retry_after_seconds == 15
    assert FakeBrowser.initialization_options[-1]["profile_lock_timeout_seconds"] == 2.0


def test_sync_handler_does_not_wait_for_shared_browser_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_lock = threading.Lock()
    browser_lock.acquire()
    monkeypatch.setattr(
        worker_module,
        "_BACKGROUND_PROFILE_LOCK_TIMEOUT_SECONDS",
        0.01,
    )
    handler = prepare_handler(
        monkeypatch,
        AutomationJobKind.MESSAGES,
        browser_lock=browser_lock,
    )

    try:
        with pytest.raises(AutomationJobDeferred) as raised:
            handler(make_job(AutomationJobKind.MESSAGES))
    finally:
        browser_lock.release()

    assert raised.value.code == "BROWSER_PROFILE_BUSY"
    assert raised.value.retry_after_seconds == 15
    assert not FakeBrowser.initialization_options


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


def test_temporary_hh_limit_is_retried_without_protecting_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = prepare_handler(monkeypatch, AutomationJobKind.STATUSES)
    protected: list[SystemState] = []
    monkeypatch.setattr(handler, "_protect_system", protected.append)
    monkeypatch.setattr(
        FakeBrowser,
        "read_application_statuses",
        lambda _browser: (_ for _ in ()).throw(
            HhSyncRetryableError(
                "HH_RATE_LIMITED",
                "hh.ru временно ограничил обращения",
                retry_after_seconds=180,
            )
        ),
    )

    with pytest.raises(AutomationJobRetry) as error:
        handler(make_job(AutomationJobKind.STATUSES))

    assert error.value.code == "HH_RATE_LIMITED"
    assert error.value.retry_after_seconds == 180
    assert protected == []


@pytest.mark.integration
def test_message_rate_limit_pauses_and_retries_same_approved_reply(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    response_text = "Здравствуйте! Да, готов обсудить детали."
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Повтор ответа после ограничения")
            resume = ResumeRepository(session).upsert(
                account.id,
                "rate-limit-resume",
                "Python-разработчик",
            )
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="101",
                    title="Python-разработчик",
                    source_url="https://hh.ru/vacancy/101",
                )
            )
            ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
            )
            account_id = account.id
            AutonomyPolicyService(session).update(
                {
                    **DEFAULT_AUTONOMY_POLICY,
                    "reply_templates": [
                        {
                            "key": "interest",
                            "incoming_text": "Предложение ещё актуально?",
                            "response_text": response_text,
                            "enabled": True,
                        }
                    ],
                }
            )
        assert account_id == 1

        incoming = HhChatMessageData(
            vacancy_id="101",
            hh_id="rate-limit-incoming",
            direction=MessageDirection.INCOMING,
            body="Предложение ещё актуально?",
        )
        monkeypatch.setattr(FakeBrowser, "messages", (incoming,))
        monkeypatch.setattr(FakeLoginService, "status", LoginStatus.AUTHENTICATED)
        attempts: list[str] = []

        def send_recruiter_message(
            _browser: FakeBrowser,
            source_url: str,
            body: str,
        ) -> MessageSendResult:
            assert source_url == "https://hh.ru/vacancy/101"
            attempts.append(body)
            if len(attempts) == 1:
                raise HhSyncRetryableError(
                    "HH_RATE_LIMITED",
                    "hh.ru временно ограничил отправку сообщений",
                    retry_after_seconds=180,
                )
            return MessageSendResult(MessageSendOutcome.SENT, "hh-reply-1")

        monkeypatch.setattr(
            FakeBrowser,
            "send_recruiter_message",
            send_recruiter_message,
            raising=False,
        )
        handler = prepare_handler(
            monkeypatch,
            AutomationJobKind.MESSAGES,
            settings,
        )
        job = replace(
            make_job(AutomationJobKind.MESSAGES),
            last_result={"message_baseline_initialized": True},
        )

        with pytest.raises(AutomationJobRetry) as error:
            handler(job)

        assert error.value.code == "HH_RATE_LIMITED"
        assert error.value.retry_after_seconds == 180
        with database.sessions() as session:
            outgoing_after_limit = tuple(
                message
                for message in CommunicationRepository(session).list_messages_for_account(
                    account_id
                )
                if message.direction is MessageDirection.OUTGOING
            )
            assert len(outgoing_after_limit) == 1
            assert outgoing_after_limit[0].state is RecruiterMessageState.CONFIRMED

        result = handler(job)

        assert result["replies_sent"] == 1
        assert attempts == [response_text, response_text]
        with database.sessions() as session:
            outgoing_after_retry = tuple(
                message
                for message in CommunicationRepository(session).list_messages_for_account(
                    account_id
                )
                if message.direction is MessageDirection.OUTGOING
            )
            assert len(outgoing_after_retry) == 1
            assert outgoing_after_retry[0].state is RecruiterMessageState.SENT
            assert outgoing_after_retry[0].hh_id == "hh-reply-1"
    finally:
        database.close()


@pytest.mark.parametrize(
    "kind",
    (AutomationJobKind.MESSAGES, AutomationJobKind.STATUSES),
)
def test_login_network_timeout_is_retried_without_protecting_system(
    monkeypatch: pytest.MonkeyPatch,
    kind: AutomationJobKind,
) -> None:
    handler = prepare_handler(monkeypatch, kind)
    protected: list[SystemState] = []
    monkeypatch.setattr(handler, "_protect_system", protected.append)
    monkeypatch.setattr(
        FakeLoginService,
        "authenticate",
        lambda _service, _account_id, _browser: (_ for _ in ()).throw(
            HhSyncRetryableError(
                "HH_NETWORK_TIMEOUT",
                "Страница входа hh.ru временно недоступна",
                retry_after_seconds=60,
            )
        ),
    )

    with pytest.raises(AutomationJobRetry) as error:
        handler(make_job(kind))

    assert error.value.code == "HH_NETWORK_TIMEOUT"
    assert error.value.retry_after_seconds == 60
    assert protected == []


def test_recovery_restores_mode_and_only_unblocks_login_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = prepare_handler(monkeypatch, AutomationJobKind.MESSAGES)
    monkeypatch.setattr(
        handler,
        "_authentication_system_state",
        lambda: SystemState.AUTH_REQUIRED,
    )
    monkeypatch.setattr(FakeLoginService, "status", LoginStatus.AUTHENTICATED)
    monkeypatch.setattr(FakeLoginService, "authentication_calls", [])
    credential_store = object()
    monkeypatch.setattr(
        worker_module,
        "WindowsCredentialStore",
        lambda: credential_store,
    )
    resumed: list[object] = []
    unblocked: list[str] = []
    jobs = (
        replace(
            make_job(AutomationJobKind.MESSAGES),
            state=AutomationJobState.BLOCKED,
            last_error_code="AUTH_REQUIRED",
        ),
        replace(
            make_job(AutomationJobKind.STATUSES),
            state=AutomationJobState.BLOCKED,
            last_error_code="CAPTCHA_REQUIRED",
        ),
        replace(
            make_job(AutomationJobKind.SEARCH),
            key="search:7",
            state=AutomationJobState.BLOCKED,
            last_error_code="ACCOUNT_WARNING",
        ),
    )

    class FakeSessions:
        def begin(self) -> object:
            return nullcontext(object())

    class FakeDatabase:
        def __init__(self) -> None:
            self.sessions = FakeSessions()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeSystemStateRepository:
        def __init__(self, _session: object) -> None:
            pass

        def lock(self) -> SimpleNamespace:
            return SimpleNamespace(state=SystemState.AUTH_REQUIRED)

    class FakeApplicationAutomationService:
        def __init__(self, session: object) -> None:
            self._session = session

        def resume_after_authentication(self) -> None:
            resumed.append(self._session)

    class FakeScheduler:
        def __init__(self, _session: object) -> None:
            pass

        def list_for_account(self, account_id: int) -> tuple[AutomationJobRecord, ...]:
            assert account_id == 1
            return jobs

        def unblock(self, job_key: str) -> None:
            unblocked.append(job_key)

    database = FakeDatabase()
    monkeypatch.setattr(worker_module, "create_database", lambda _settings: database)
    monkeypatch.setattr(
        worker_module,
        "SystemStateRepository",
        FakeSystemStateRepository,
    )
    monkeypatch.setattr(
        worker_module,
        "ApplicationAutomationService",
        FakeApplicationAutomationService,
    )
    monkeypatch.setattr(worker_module, "AutomationSchedulerService", FakeScheduler)

    assert handler.recover_authentication()

    assert FakeLoginService.credential_store is credential_store
    assert FakeLoginService.authentication_calls == [1]
    assert FakeLoginService.observation_calls == []
    assert FakeBrowser.initialization_options[-1]["start_minimized"] is False
    assert len(resumed) == 1
    assert unblocked == ["messages:1", "statuses:1"]
    assert database.closed


def test_recovery_never_clears_account_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = prepare_handler(monkeypatch, AutomationJobKind.MESSAGES)
    monkeypatch.setattr(
        handler,
        "_authentication_system_state",
        lambda: SystemState.ACCOUNT_WARNING,
    )
    resumed = 0
    unblocked: list[str] = []

    class FakeSessions:
        def begin(self) -> object:
            return nullcontext(object())

    class FakeDatabase:
        sessions = FakeSessions()

        def close(self) -> None:
            pass

    class FakeSystemStateRepository:
        def __init__(self, _session: object) -> None:
            pass

        def lock(self) -> SimpleNamespace:
            return SimpleNamespace(state=SystemState.ACCOUNT_WARNING)

    class FakeApplicationAutomationService:
        def __init__(self, _session: object) -> None:
            pass

        def resume_after_authentication(self) -> None:
            nonlocal resumed
            resumed += 1

    class FakeScheduler:
        def __init__(self, _session: object) -> None:
            pass

        def list_for_account(self, _account_id: int) -> tuple[AutomationJobRecord, ...]:
            return ()

        def unblock(self, job_key: str) -> None:
            unblocked.append(job_key)

    monkeypatch.setattr(worker_module, "create_database", lambda _settings: FakeDatabase())
    monkeypatch.setattr(
        worker_module,
        "SystemStateRepository",
        FakeSystemStateRepository,
    )
    monkeypatch.setattr(
        worker_module,
        "ApplicationAutomationService",
        FakeApplicationAutomationService,
    )
    monkeypatch.setattr(worker_module, "AutomationSchedulerService", FakeScheduler)

    assert not handler.recover_authentication()
    assert resumed == 0
    assert not unblocked


def test_recovery_keeps_system_blocked_until_login_service_confirms_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = prepare_handler(monkeypatch, AutomationJobKind.MESSAGES)
    monkeypatch.setattr(
        handler,
        "_authentication_system_state",
        lambda: SystemState.AUTH_REQUIRED,
    )
    monkeypatch.setattr(FakeLoginService, "status", LoginStatus.CAPTCHA_REQUIRED)
    monkeypatch.setattr(FakeLoginService, "authentication_calls", [])
    protected: list[SystemState] = []
    monkeypatch.setattr(handler, "_protect_system", protected.append)

    assert not handler.recover_authentication()
    assert FakeLoginService.authentication_calls == [1]
    assert protected == [SystemState.CAPTCHA_REQUIRED]


def test_captcha_recovery_only_observes_visible_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = prepare_handler(monkeypatch, AutomationJobKind.MESSAGES)
    monkeypatch.setattr(
        handler,
        "_authentication_system_state",
        lambda: SystemState.CAPTCHA_REQUIRED,
    )
    monkeypatch.setattr(FakeLoginService, "status", LoginStatus.AUTHENTICATED)
    monkeypatch.setattr(handler, "_restore_after_authentication", lambda: True)

    assert handler.recover_authentication()
    assert FakeLoginService.authentication_calls == []
    assert FakeLoginService.observation_calls == [1]
    assert FakeBrowser.initialization_options[-1]["start_minimized"] is False


def test_recovery_promotes_real_account_warning_and_does_not_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = prepare_handler(monkeypatch, AutomationJobKind.MESSAGES)
    monkeypatch.setattr(
        handler,
        "_authentication_system_state",
        lambda: SystemState.AUTH_REQUIRED,
    )
    monkeypatch.setattr(FakeLoginService, "status", LoginStatus.ACCOUNT_WARNING)
    protected: list[SystemState] = []
    monkeypatch.setattr(handler, "_protect_system", protected.append)
    monkeypatch.setattr(
        handler,
        "_restore_after_authentication",
        lambda: pytest.fail("Предупреждение аккаунта нельзя снимать автоматически"),
    )

    assert not handler.recover_authentication()
    assert protected == [SystemState.ACCOUNT_WARNING]


def test_recovery_does_not_wait_for_busy_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_lock = threading.Lock()
    browser_lock.acquire()
    handler = worker_module.HhSyncJobHandler(
        Settings(environment="test"),
        AutomationJobKind.MESSAGES,
        browser_lock=browser_lock,
    )
    monkeypatch.setattr(
        worker_module,
        "VisibleHhBrowser",
        lambda *_args, **_kwargs: pytest.fail("Занятый браузер нельзя открывать"),
    )
    try:
        assert not handler.recover_authentication()
    finally:
        browser_lock.release()


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
    assert handler._synchronize_messages((message,), allow_replies=False) == {"created": 1}
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
