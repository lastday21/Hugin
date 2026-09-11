from typing import Any

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import ApplicationModel, VacancyModel
from hugin.domain.applications import ApplicationState
from hugin.services.autonomous_replies import AutonomousReplyBatch, AutonomousReplyService
from hugin.services.background_processes import BackgroundProcessService
from hugin.services.communications import CommunicationService, RecordingMessageSender
from hugin.workers.replies import ReplyWorker
from tests.unit.test_communications import create_application

pytestmark = pytest.mark.integration


def seed(settings: Settings) -> tuple[int, int, int]:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account, first_id = create_application(
                session, account_label="Replies", vacancy_hh_id="first"
            )
            first = session.get(ApplicationModel, first_id)
            assert first is not None
            vacancy = VacancyModel(
                hh_id="second", title="Python", source_url="https://hh.ru/vacancy/second"
            )
            session.add(vacancy)
            session.flush()
            second = ApplicationModel(
                account_id=account,
                resume_id=first.resume_id,
                vacancy_id=vacancy.id,
                state=ApplicationState.APPLIED,
            )
            session.add(second)
            session.flush()
            communication = CommunicationService(session, RecordingMessageSender())
            for application_id in (first.id, second.id):
                communication.save_incoming(
                    application_id=application_id,
                    hh_id=str(application_id),
                    body="Готовы обсудить вакансию?",
                )
            BackgroundProcessService(session, account).set_enabled("replies", True)
            return account, first.id, second.id
    finally:
        database.close()


def disable(settings: Settings, account: int) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            BackgroundProcessService(session, account).set_enabled("replies", False)
    finally:
        database.close()


def test_disabled_worker_never_builds_model_or_prepares(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, _, _ = seed(settings)
    disable(settings, account)

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Выключенный процесс начал подготовку")

    monkeypatch.setattr(AutonomousReplyService, "prepare", unexpected)
    assert not ReplyWorker(settings, account_id=account).run_once()


def test_cursor_survives_restart_and_error_does_not_starve_next_dialogue(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account, first, second = seed(settings)
    seen = []

    def prepare(self: AutonomousReplyService, **kwargs: Any) -> AutonomousReplyBatch:
        seen.append(kwargs["application_id"])
        assert kwargs["include_backlog"] is True and kwargs["incoming_message_ids"]
        if len(seen) == 1:
            raise RuntimeError("Ошибка первого диалога")
        return AutonomousReplyBatch((), 0, 1, 0)

    monkeypatch.setattr(AutonomousReplyService, "prepare", prepare)
    with pytest.raises(RuntimeError):
        ReplyWorker(settings, account_id=account).run_once()
    assert ReplyWorker(settings, account_id=account).run_once()
    assert seen == [first, second]
    assert not ReplyWorker(settings, account_id=account).run_once()


def test_stop_between_preparation_and_send_keeps_prepared_draft(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hugin.services.autonomous_replies import ApprovedReplyToSend
    from hugin.workers.hh_sync import HhSyncJobHandler

    account, first, _ = seed(settings)

    def prepare(self: AutonomousReplyService, **kwargs: Any) -> AutonomousReplyBatch:
        disable(settings, account)
        return AutonomousReplyBatch(
            (ApprovedReplyToSend(1, first, "https://hh.ru", "hash", 1),), 1, 0, 0
        )

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Остановка не разрешает отправку")

    monkeypatch.setattr(AutonomousReplyService, "prepare", prepare)
    monkeypatch.setattr(HhSyncJobHandler, "_send_approved_replies", unexpected)
    assert ReplyWorker(settings, account_id=account).run_once()


def test_close_during_login_cancels_send_without_revoking_saved_permission(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from hugin.domain.automation import AutomationJobKind
    from hugin.domain.content import RecruiterMessageState
    from hugin.repositories.communications import CommunicationRepository
    from hugin.services.hh_login import LoginStatus
    from tests.unit.test_hh_sync_worker import FakeBrowser, FakeLoginService, prepare_handler
    from tests.unit.test_hh_sync_worker_reliability import prepare_reply

    account_id, approved = prepare_reply(settings)
    prepare_handler(monkeypatch, AutomationJobKind.MESSAGES, settings)
    worker = ReplyWorker(settings, account_id=account_id)

    def authenticate(
        _service: FakeLoginService, _account: int, _browser: object
    ) -> SimpleNamespace:
        worker.stop()
        return SimpleNamespace(authenticated=True, status=LoginStatus.AUTHENTICATED)

    monkeypatch.setattr(FakeLoginService, "authenticate", authenticate)
    monkeypatch.setattr(
        FakeBrowser,
        "send_recruiter_message",
        lambda *_args: pytest.fail("Приложение закрыто; отправка запрещена"),
        raising=False,
    )
    assert worker.run_once()
    database = create_database(settings)
    try:
        with database.sessions() as session:
            assert BackgroundProcessService(session, account_id).enabled("replies")
            message = next(
                item
                for item in CommunicationRepository(session).list_messages_for_account(account_id)
                if item.id == approved[0].message_id
            )
            assert message.state == RecruiterMessageState.CONFIRMED
            assert message.content_hash == approved[0].content_hash
    finally:
        database.close()


def test_prepare_filters_one_dialogue_and_respects_stop_before_model(settings: Settings) -> None:
    from hugin.services.autonomy import AutonomyPolicyService
    from tests.unit.test_recruiter_reply import FakeReplyModel

    account, first, second = seed(settings)
    database = create_database(settings)
    model = FakeReplyModel()
    try:
        with database.sessions.begin() as session:
            policy = AutonomyPolicyService(session)
            policy.update(
                {
                    **policy.get().as_payload(),
                    "reply_templates": [
                        {
                            "key": "interest",
                            "incoming_text": "Готовы обсудить вакансию?",
                            "response_text": "Здравствуйте! Да, готов обсудить задачи.",
                            "enabled": True,
                        }
                    ],
                }
            )
            batch = AutonomousReplyService(session).prepare(
                account_id=account,
                application_id=second,
                include_backlog=True,
                model_factory=lambda: model,
            )
            assert batch.drafts_created == 1
            assert len(model.prompts) == 1
            messages = CommunicationService(session, RecordingMessageSender()).messages(account)
            assert all(
                message.application_id == second
                for message in messages
                if message.direction.value == "OUTGOING"
            )
            stopped = AutonomousReplyService(session).prepare(
                account_id=account,
                application_id=first,
                include_backlog=True,
                model_factory=lambda: model,
                can_continue=lambda: False,
            )
            assert stopped == AutonomousReplyBatch((), 0, 0, 0)
            assert len(model.prompts) == 1
    finally:
        database.close()


def test_stop_after_requirement_model_prevents_reply_model(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import select

    import hugin.workers.replies as worker_module
    from hugin.database.models import RecruiterMessageModel
    from hugin.domain.content import MessageDirection

    account, first, _ = seed(settings)
    database = create_database(settings)
    with database.sessions.begin() as session:
        incoming = session.scalar(
            select(RecruiterMessageModel).where(RecruiterMessageModel.application_id == first)
        )
        assert incoming is not None
        incoming.body = "Давайте пока оставим это здесь."
    operations: list[str] = []

    class Model:
        model_name = "test"

        def complete(self, system_prompt: str, user_prompt: str) -> str:
            disable(settings, account)
            return "REPLY_REQUIRED"

    def factory(settings: Settings, **kwargs: Any) -> Model:
        assert kwargs["timeout_seconds"] <= 60
        operations.append(kwargs["operation"])
        return Model()

    monkeypatch.setattr(worker_module, "configured_codex_cli_client", factory)
    assert ReplyWorker(settings, account_id=account).run_once()
    assert operations == ["recruiter_reply_requirement"]
    with database.sessions() as session:
        messages = CommunicationService(session, RecordingMessageSender()).messages(account)
        assert not any(message.direction == MessageDirection.OUTGOING for message in messages)
    database.close()


def test_model_rechecks_permission_at_call_time_and_stop_event(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hugin.workers.replies as worker_module
    from hugin.workers.model_turn import ModelTurn
    from hugin.workers.replies import ReplyStopped
    from tests.unit.test_recruiter_reply import FakeReplyModel

    account, _, _ = seed(settings)
    model = FakeReplyModel()
    monkeypatch.setattr(worker_module, "configured_codex_cli_client", lambda *args, **kwargs: model)
    worker = ReplyWorker(settings, account_id=account)
    guarded = worker._model(ModelTurn(worker._allowed))
    assert guarded.model_name == model.model_name
    disable(settings, account)
    with pytest.raises(RuntimeError):
        guarded.complete("system", "user")
    assert model.prompts == []
    with pytest.raises(ReplyStopped):
        worker._model(ModelTurn(worker._allowed))
    worker.stop()
    assert not worker._allowed() and not worker.run_once()


def test_unknown_outgoing_is_not_retried_by_worker(settings: Settings) -> None:
    from hugin.database.models import RecruiterMessageModel
    from hugin.domain.content import RecruiterMessageState

    account, first, second = seed(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            for application_id in (first, second):
                draft = CommunicationService(
                    session, RecordingMessageSender()
                ).create_outgoing_draft(application_id=application_id, body="Сохранённый ответ")
                outgoing = session.get(RecruiterMessageModel, draft.id)
                assert outgoing is not None
                outgoing.state = RecruiterMessageState.UNKNOWN_RESULT
        assert not ReplyWorker(settings, account_id=account).run_once()
    finally:
        database.close()


def test_stop_after_claim_prevents_preparation(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account, _, _ = seed(settings)
    worker = ReplyWorker(settings, account_id=account)
    monkeypatch.setattr(worker, "_allowed", lambda: False)
    assert not worker.run_once()


def test_model_cancellation_is_a_normal_stop(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hugin.workers.replies import ReplyStopped

    account, _, _ = seed(settings)

    def prepare(self: AutonomousReplyService, **kwargs: Any) -> AutonomousReplyBatch:
        raise ReplyStopped("Остановлено во время подготовки")

    monkeypatch.setattr(AutonomousReplyService, "prepare", prepare)
    assert not ReplyWorker(settings, account_id=account).run_once()


def test_warning_from_send_protects_account(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hugin.database.models import SystemStateModel
    from hugin.domain.hh_sync import HhSyncBlockedError
    from hugin.domain.tasks import SystemState
    from hugin.services.autonomous_replies import ApprovedReplyToSend
    from hugin.workers.automation import AutomationJobBlocked

    account, first, _ = seed(settings)
    worker = ReplyWorker(settings, account_id=account)

    def prepare(self: AutonomousReplyService, **kwargs: Any) -> AutonomousReplyBatch:
        return AutonomousReplyBatch(
            (ApprovedReplyToSend(1, first, "https://hh.ru", "hash", 1),), 1, 0, 0
        )

    def blocked(*args: Any, **kwargs: Any) -> dict[str, int]:
        raise HhSyncBlockedError("ACCOUNT_WARNING", "Предупреждение hh.ru")

    monkeypatch.setattr(AutonomousReplyService, "prepare", prepare)
    monkeypatch.setattr(worker._sender, "_send_approved_replies", blocked)
    with pytest.raises(AutomationJobBlocked) as error:
        worker.run_once()
    assert error.value.code == "ACCOUNT_WARNING"
    database = create_database(settings)
    try:
        with database.sessions() as session:
            state = session.get(SystemStateModel, 1)
            assert state is not None and state.state == SystemState.ACCOUNT_WARNING
    finally:
        database.close()


@pytest.mark.integration
def test_message_rate_limit_pauses_and_retries_same_approved_reply(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from hugin.database.models import BackgroundProcessRunModel
    from hugin.domain.automation import AutomationJobKind
    from hugin.domain.communications import MessageSendOutcome, MessageSendResult
    from hugin.domain.content import MessageDirection, RecruiterMessageState
    from hugin.domain.hh_sync import HhChatMessageData, HhSyncRetryableError
    from hugin.domain.vacancies import VacancyData
    from hugin.repositories.applications import ApplicationRepository
    from hugin.repositories.communications import CommunicationRepository
    from hugin.repositories.directions import AccountRepository, ResumeRepository
    from hugin.repositories.vacancies import VacancyRepository
    from hugin.services.autonomy import DEFAULT_AUTONOMY_POLICY, AutonomyPolicyService
    from hugin.services.hh_login import LoginStatus
    from hugin.workers.automation import AutomationJobRetry
    from tests.unit.test_hh_sync_worker import FakeBrowser, FakeLoginService, prepare_handler

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
        handler._synchronize_messages((incoming,))
        worker = ReplyWorker(settings, account_id=account_id)

        with pytest.raises(AutomationJobRetry) as error:
            worker.run_once()

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

        assert not worker.run_once()
        with database.sessions.begin() as session:
            runtime = session.get(BackgroundProcessRunModel, (account_id, "replies"))
            assert runtime is not None
            assert runtime.retry_after_at is not None
            assert runtime.retry_after_at > datetime.now(UTC) + timedelta(seconds=170)
            runtime.retry_after_at = runtime.next_attempt_at = datetime.now(UTC) - timedelta(
                seconds=1
            )
        assert worker.run_once()
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
