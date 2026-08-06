# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.domain.applications import ApplicationState
from hugin.domain.content import InvitationState, MessageDirection, RecruiterMessageState
from hugin.domain.hh_sync import (
    HhChatMessageData,
    HhNegotiationData,
    HhNegotiationStatus,
)
from hugin.domain.tasks import TaskState
from hugin.domain.vacancies import VacancyData
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    QueueTaskRepository,
    ResumeRepository,
    VacancyRepository,
)
from hugin.repositories.communications import CommunicationRepository
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.hh_sync import HhSynchronizationService

pytestmark = pytest.mark.integration


def test_test_assignment_takes_priority_over_interview_wording() -> None:
    body = "Приглашаем продолжить отбор. Для этого обязательно выполните тестовое задание."

    assert HhSynchronizationService._attention_title(body) == "Задание от работодателя"


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        (
            "К сожалению, мы не приглашаем вас на следующий этап.",
            None,
        ),
        (
            "Мы не готовы пригласить вас на собеседование.",
            None,
        ),
        (
            "Не забудьте: приглашаем на собеседование.",
            "Приглашение на собеседование",
        ),
    ),
)
def test_interview_classification_respects_negative_context(
    body: str,
    expected: str | None,
) -> None:
    assert HhSynchronizationService._attention_title(body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        (
            "Мы не готовы пригласить вас на собеседование, "
            "но предлагаем выполнить тестовое задание.",
            "Задание от работодателя",
        ),
        (
            "Мы не готовы пригласить вас на собеседование, "
            "но сообщите, пожалуйста, когда сможете приступить.",
            "Вопрос работодателя",
        ),
    ),
)
def test_negative_interview_context_preserves_other_attention(
    body: str,
    expected: str,
) -> None:
    assert HhSynchronizationService._attention_title(body) == expected


def test_hh_statuses_and_messages_are_synchronized_idempotently(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    checked_at = datetime(2026, 7, 27, 8, 30, tzinfo=UTC)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Синхронизация")
            resume = ResumeRepository(session).upsert(
                account.id,
                "sync-resume",
                "Python backend",
            )
            vacancies = VacancyRepository(session)
            applications = ApplicationRepository(session)
            first_vacancy = vacancies.upsert(
                VacancyData(
                    hh_id="sync-101",
                    title="Python-разработчик",
                    source_url="https://hh.ru/vacancy/sync-101",
                )
            )
            second_vacancy = vacancies.upsert(
                VacancyData(
                    hh_id="sync-202",
                    title="Инженер",
                    source_url="https://hh.ru/vacancy/sync-202",
                )
            )
            first = applications.create_apply_intent(
                account.id,
                first_vacancy.id,
                resume.id,
            )
            second = applications.create_apply_intent(
                account.id,
                second_vacancy.id,
                resume.id,
            )
            tasks = QueueTaskRepository(session)
            first_task = tasks.enqueue(first.id, 80)
            second_task = tasks.enqueue(second.id, 70)

        with database.sessions.begin() as session:
            service = HhSynchronizationService(session)
            assert service.tracked_vacancy_ids(account.id) == ("sync-202", "sync-101")
            applied = service.synchronize_statuses(
                account_id=account.id,
                statuses=(
                    HhNegotiationData(
                        "sync-101",
                        HhNegotiationStatus.VIEWED,
                        "Просмотрен",
                        True,
                    ),
                    HhNegotiationData(
                        "sync-202",
                        HhNegotiationStatus.REJECTED,
                        "Отказ",
                    ),
                    HhNegotiationData(
                        "not-tracked",
                        HhNegotiationStatus.INVITED,
                        "Приглашение",
                    ),
                ),
                checked_at=checked_at,
            )
            assert applied == {
                "tracked": 2,
                "received": 3,
                "matched": 2,
                "updated": 4,
                "invitations": 0,
            }
            assert ApplicationRepository(session).get(first.id).state is ApplicationState.VIEWED
            assert ApplicationRepository(session).get(second.id).state is ApplicationState.REJECTED
            assert QueueTaskRepository(session).get(first_task.id).state is TaskState.SKIPPED
            assert QueueTaskRepository(session).get(second_task.id).state is TaskState.SKIPPED

            invited = service.synchronize_statuses(
                account_id=account.id,
                statuses=(
                    HhNegotiationData(
                        "sync-101",
                        HhNegotiationStatus.INVITED,
                        "Собеседование",
                        True,
                    ),
                ),
                checked_at=checked_at,
            )
            assert invited["updated"] == 1
            assert invited["invitations"] == 1
            repeated = service.synchronize_statuses(
                account_id=account.id,
                statuses=(
                    HhNegotiationData(
                        "sync-101",
                        HhNegotiationStatus.APPLIED,
                        "Не просмотрен",
                        True,
                    ),
                ),
                checked_at=checked_at,
            )
            assert repeated["updated"] == 0
            assert ApplicationRepository(session).get(first.id).state is ApplicationState.INVITED

            messages = (
                HhChatMessageData(
                    vacancy_id="sync-101",
                    hh_id="message-1",
                    direction=MessageDirection.INCOMING,
                    body=(
                        "Приглашаем на собеседование. "
                        "Выберите время: https://calendar.example.com/interview."
                    ),
                ),
                HhChatMessageData(
                    vacancy_id="sync-101",
                    hh_id="message-2",
                    direction=MessageDirection.OUTGOING,
                    body="Спасибо, подтверждаю.",
                ),
            )
            first_sync_details = service.synchronize_messages_with_new_ids(
                account_id=account.id,
                messages=messages,
                checked_at=checked_at,
            )
            first_sync = first_sync_details.metrics
            second_sync = service.synchronize_messages(
                account_id=account.id,
                messages=messages,
                checked_at=checked_at,
            )
            assert first_sync["created"] == 2
            assert first_sync["incoming"] == 1
            assert first_sync["outgoing"] == 1
            assert first_sync["attention"] == 1
            assert len(first_sync_details.new_incoming_message_ids) == 1
            assert second_sync["created"] == 0

            communication = CommunicationRepository(session)
            stored_messages = communication.list_messages_for_account(account.id)
            assert len(stored_messages) == 2
            assert {message.state for message in stored_messages} == {
                RecruiterMessageState.RECEIVED,
                RecruiterMessageState.SENT,
            }
            invitations = communication.list_invitations_for_account(account.id)
            assert len(invitations) == 2
            assert invitations[0].booking_url == "https://calendar.example.com/interview"
            assert invitations[1].state is InvitationState.CLOSED

            status_after_message = service.synchronize_statuses(
                account_id=account.id,
                statuses=(
                    HhNegotiationData(
                        "sync-101",
                        HhNegotiationStatus.INVITED,
                        "Собеседование",
                        True,
                    ),
                ),
                checked_at=checked_at,
            )
            assert status_after_message["invitations"] == 0
            assert len(communication.list_invitations_for_account(account.id)) == 2
    finally:
        database.close()


def test_status_sync_reconciles_unknown_application_automatically(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Сверка неизвестного результата")
            resume = ResumeRepository(session).upsert(
                account.id,
                "unknown-sync-resume",
                "Python backend",
            )
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="unknown-sync-vacancy",
                    title="Python-разработчик",
                    source_url="https://hh.ru/vacancy/unknown-sync-vacancy",
                )
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
            )
            tasks = QueueTaskRepository(session)
            task = tasks.enqueue(application.id, 80)
            assert tasks.claim_exact(task.id) is not None
            tasks.transition(
                task.id,
                TaskState.UNKNOWN_RESULT,
                error_code="UNKNOWN_RESULT",
            )

            HhSynchronizationService(session).synchronize_statuses(
                account_id=account.id,
                statuses=(
                    HhNegotiationData(
                        vacancy.hh_id,
                        HhNegotiationStatus.APPLIED,
                        "Не просмотрен",
                        True,
                    ),
                ),
            )

            assert (
                ApplicationRepository(session).get(application.id).state is ApplicationState.APPLIED
            )
            assert tasks.get(task.id).state is TaskState.COMPLETED
            events = ApplicationRepository(session).list_events(application.id)
            assert events[-1].payload["source"] == "hugin_reconciliation"
            service = ApplicationAutomationService(session)
            assert (
                service.applied_since(
                    account.id,
                    datetime(2026, 1, 1, tzinfo=UTC),
                )
                == 1
            )

            HhSynchronizationService(session).synchronize_statuses(
                account_id=account.id,
                statuses=(
                    HhNegotiationData(
                        vacancy.hh_id,
                        HhNegotiationStatus.APPLIED,
                        "Не просмотрен",
                        True,
                    ),
                ),
            )
            assert (
                service.applied_since(
                    account.id,
                    datetime(2026, 1, 1, tzinfo=UTC),
                )
                == 1
            )
    finally:
        database.close()


def test_status_sync_leaves_unknown_application_for_manual_review_when_resume_is_ambiguous(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Неоднозначная сверка")
            first_resume = ResumeRepository(session).upsert(
                account.id,
                "ambiguous-sync-resume-1",
                "Python backend",
            )
            second_resume = ResumeRepository(session).upsert(
                account.id,
                "ambiguous-sync-resume-2",
                "Python-разработчик",
            )
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="ambiguous-sync-vacancy",
                    title="Python-разработчик",
                    source_url="https://hh.ru/vacancy/ambiguous-sync-vacancy",
                )
            )
            applications = ApplicationRepository(session)
            first_application = applications.create_apply_intent(
                account.id,
                vacancy.id,
                first_resume.id,
            )
            unknown_application = applications.create_apply_intent(
                account.id,
                vacancy.id,
                second_resume.id,
            )
            tasks = QueueTaskRepository(session)
            task = tasks.enqueue(unknown_application.id, 80)
            assert tasks.claim_exact(task.id) is not None
            tasks.transition(
                task.id,
                TaskState.UNKNOWN_RESULT,
                error_code="UNKNOWN_RESULT",
            )
            event_count = len(applications.list_events(unknown_application.id))

            result = HhSynchronizationService(session).synchronize_statuses(
                account_id=account.id,
                statuses=(
                    HhNegotiationData(
                        vacancy.hh_id,
                        HhNegotiationStatus.APPLIED,
                        "Не просмотрен",
                        True,
                    ),
                ),
            )

            assert result["matched"] == 0
            assert applications.get(first_application.id).state is ApplicationState.APPLYING
            assert applications.get(unknown_application.id).state is ApplicationState.APPLYING
            assert tasks.get(task.id).state is TaskState.UNKNOWN_RESULT
            assert len(applications.list_events(unknown_application.id)) == event_count
    finally:
        database.close()


def test_incoming_question_does_not_close_status_invitation(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    checked_at = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Вопрос после приглашения")
            resume = ResumeRepository(session).upsert(
                account.id,
                "question-after-invitation-resume",
                "Python backend",
            )
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="question-after-invitation",
                    title="Python-разработчик",
                    source_url="https://hh.ru/vacancy/question-after-invitation",
                )
            )
            ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
            )

        with database.sessions.begin() as session:
            service = HhSynchronizationService(session)
            status_result = service.synchronize_statuses(
                account_id=account.id,
                statuses=(
                    HhNegotiationData(
                        vacancy.hh_id,
                        HhNegotiationStatus.INVITED,
                        "Приглашение на собеседование",
                        True,
                    ),
                ),
                checked_at=checked_at,
            )
            message_result = service.synchronize_messages(
                account_id=account.id,
                messages=(
                    HhChatMessageData(
                        vacancy_id=vacancy.hh_id,
                        hh_id="question-message",
                        direction=MessageDirection.INCOMING,
                        body="Ответьте, пожалуйста, когда сможете приступить к работе.",
                    ),
                ),
                checked_at=checked_at,
            )

            assert status_result["invitations"] == 1
            assert message_result["attention"] == 1
            invitations = CommunicationRepository(session).list_invitations_for_account(account.id)
            assert len(invitations) == 2
            assert {invitation.title for invitation in invitations} == {
                "Приглашение на собеседование",
                "Вопрос работодателя",
            }
            assert all(invitation.state is InvitationState.RECEIVED for invitation in invitations)
    finally:
        database.close()
