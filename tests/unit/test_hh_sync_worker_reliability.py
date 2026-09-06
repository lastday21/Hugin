from __future__ import annotations

from types import SimpleNamespace

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.domain.automation import AutomationJobKind
from hugin.domain.communications import MessageSendOutcome, MessageSendResult
from hugin.domain.content import MessageDirection, RecruiterMessageState
from hugin.domain.hh_sync import HhChatMessageData
from hugin.domain.tasks import SystemState
from hugin.repositories.communications import CommunicationRepository
from hugin.repositories.tasks import SystemStateRepository
from hugin.services.autonomous_replies import ApprovedReplyToSend, AutonomousReplyService
from hugin.services.autonomy import DEFAULT_AUTONOMY_POLICY, AutonomyPolicyService
from hugin.services.hh_login import LoginStatus
from hugin.services.hh_sync import HhSynchronizationService
from hugin.workers.automation import AutomationJobBlocked
from tests.unit.test_communications import create_application
from tests.unit.test_hh_sync_worker import FakeBrowser, FakeLoginService, prepare_handler

pytestmark = pytest.mark.integration


def prepare_reply(settings: Settings) -> tuple[int, tuple[ApprovedReplyToSend, ...]]:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, _application_id = create_application(
                session, account_label="Проверка восстановления", vacancy_hh_id="101"
            )
            AutonomyPolicyService(session).update(
                {
                    **DEFAULT_AUTONOMY_POLICY,
                    "reply_templates": [
                        {
                            "key": "interest",
                            "incoming_text": "Предложение ещё актуально?",
                            "response_text": "Здравствуйте! Да, готов обсудить детали.",
                            "enabled": True,
                        }
                    ],
                }
            )
            synchronization = HhSynchronizationService(session).synchronize_messages_with_new_ids(
                account_id=account_id,
                messages=(
                    HhChatMessageData(
                        "101", "interest-1", MessageDirection.INCOMING, "Предложение ещё актуально?"
                    ),
                ),
            )
            batch = AutonomousReplyService(session).prepare(
                account_id=account_id,
                incoming_message_ids=synchronization.new_incoming_message_ids,
            )
            assert len(batch.approved) == 1
            return account_id, batch.approved
    finally:
        database.close()


@pytest.mark.parametrize(
    ("outcome", "metric", "state"),
    [
        (MessageSendOutcome.SENT, "sent", RecruiterMessageState.SENT),
        (MessageSendOutcome.UNKNOWN_RESULT, "unknown", RecruiterMessageState.UNKNOWN_RESULT),
        (MessageSendOutcome.FAILED, "failed", RecruiterMessageState.FAILED),
    ],
)
def test_reply_result_is_persisted_and_the_same_approval_cannot_send_twice(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    outcome: MessageSendOutcome,
    metric: str,
    state: RecruiterMessageState,
) -> None:
    account_id, approved = prepare_reply(settings)
    handler = prepare_handler(monkeypatch, AutomationJobKind.MESSAGES, settings)
    monkeypatch.setattr(FakeLoginService, "status", LoginStatus.AUTHENTICATED)
    calls: list[str] = []

    def send(_browser: FakeBrowser, source_url: str, body: str) -> MessageSendResult:
        assert source_url == approved[0].source_url
        calls.append(body)
        return MessageSendResult(outcome, "reply-1" if outcome is MessageSendOutcome.SENT else None)

    monkeypatch.setattr(FakeBrowser, "send_recruiter_message", send, raising=False)
    first = handler._send_approved_replies(approved)
    assert first[metric] == 1
    assert sum(first.values()) == 1
    assert handler._send_approved_replies(approved) == {
        "sent": 0,
        "failed": 0,
        "unknown": 0,
        "cancelled": 1,
    }
    assert len(calls) == 1
    database = create_database(settings)
    try:
        with database.sessions() as session:
            messages = CommunicationRepository(session).list_messages_for_account(account_id)
            outgoing = [item for item in messages if item.direction is MessageDirection.OUTGOING]
            assert len(outgoing) == 1
            assert outgoing[0].state is state
    finally:
        database.close()


def test_policy_revocation_after_preparation_cancels_the_reply(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _account_id, approved = prepare_reply(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            AutonomyPolicyService(session).update(
                {**DEFAULT_AUTONOMY_POLICY, "auto_send_approved_replies": False}
            )
    finally:
        database.close()
    handler = prepare_handler(monkeypatch, AutomationJobKind.MESSAGES, settings)
    monkeypatch.setattr(FakeLoginService, "status", LoginStatus.AUTHENTICATED)
    monkeypatch.setattr(
        FakeBrowser,
        "send_recruiter_message",
        lambda *_args: pytest.fail("Отозванное разрешение не допускает отправку"),
        raising=False,
    )
    assert handler._send_approved_replies(approved) == {
        "sent": 0,
        "failed": 0,
        "unknown": 0,
        "cancelled": 1,
    }


def test_lost_login_before_reply_preserves_the_unsent_approval(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id, approved = prepare_reply(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            SystemStateRepository(session).transition(SystemState.RUNNING)
        handler = prepare_handler(monkeypatch, AutomationJobKind.MESSAGES, settings)
        monkeypatch.setattr(FakeLoginService, "status", LoginStatus.CAPTCHA_REQUIRED)
        with pytest.raises(AutomationJobBlocked) as error:
            handler._send_approved_replies(approved)
        assert error.value.code == "CAPTCHA_REQUIRED"
        assert not handler._browser_lock.locked()
        with database.sessions() as session:
            assert SystemStateRepository(session).get().state is SystemState.CAPTCHA_REQUIRED
            messages = CommunicationRepository(session).list_messages_for_account(account_id)
            outgoing = [item for item in messages if item.direction is MessageDirection.OUTGOING]
            assert len(outgoing) == 1
            assert outgoing[0].state is RecruiterMessageState.CONFIRMED
    finally:
        database.close()


def test_security_warning_arriving_during_login_cannot_be_cleared_by_recovery(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            system = SystemStateRepository(session)
            system.transition(SystemState.RUNNING)
            system.transition(SystemState.AUTH_REQUIRED)
        handler = prepare_handler(monkeypatch, AutomationJobKind.MESSAGES, settings)

        def login(
            _service: FakeLoginService, _account_id: int, _browser: object
        ) -> SimpleNamespace:
            handler._protect_system(SystemState.ACCOUNT_WARNING)
            return SimpleNamespace(authenticated=True, status=LoginStatus.AUTHENTICATED)

        monkeypatch.setattr(FakeLoginService, "authenticate", login)
        assert not handler.recover_authentication()
        assert not handler._browser_lock.locked()
        with database.sessions() as session:
            assert SystemStateRepository(session).get().state is SystemState.ACCOUNT_WARNING
    finally:
        database.close()
