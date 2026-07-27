from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    ApplicationSettingsModel,
    ApplicationTaskModel,
    AutomationJobModel,
    IncidentModel,
)
from hugin.domain.automation import AutomationJobKind, AutomationJobState
from hugin.domain.content import (
    DeliveryState,
    IncidentSeverity,
    IncidentState,
    NotificationChannel,
)
from hugin.domain.tasks import TaskState
from hugin.domain.vacancies import VacancyData
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    ResumeRepository,
    VacancyRepository,
)
from hugin.repositories.communications import CommunicationRepository
from hugin.services.communications import CommunicationService, RecordingMessageSender
from hugin.services.notifications import NotificationService

pytestmark = pytest.mark.integration


def create_application(session: Session) -> tuple[int, int]:
    account = AccountRepository(session).create("Уведомления")
    resume = ResumeRepository(session).upsert(account.id, "notify-resume", "Python backend")
    vacancy = VacancyRepository(session).upsert(
        VacancyData(
            hh_id="notify-101",
            title="Python-разработчик",
            source_url="https://hh.ru/vacancy/notify-101",
        )
    )
    application = ApplicationRepository(session).create_apply_intent(
        account.id,
        vacancy.id,
        resume.id,
    )
    return account.id, application.id


def test_notification_collection_delivery_and_retry_are_idempotent(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime(2026, 7, 27, 16, 0, tzinfo=UTC)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(session)
            communications = CommunicationService(session, RecordingMessageSender())
            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-1",
                body="Здравствуйте!",
                received_at=now,
            )
            communications.save_invitation(
                application_id=application_id,
                hh_id="invitation-1",
                title="Собеседование",
                updated_at=now,
            )
            session.add(
                ApplicationTaskModel(
                    application_id=application_id,
                    state=TaskState.INPUT_REQUIRED,
                    priority_score=80,
                    scheduled_at=now,
                    last_error_code="QUESTIONS_REQUIRED",
                )
            )
            session.add(
                AutomationJobModel(
                    key=f"messages:{account_id}",
                    kind=AutomationJobKind.MESSAGES,
                    state=AutomationJobState.BLOCKED,
                    account_id=account_id,
                    interval_seconds=300,
                    last_error_code="CAPTCHA_REQUIRED",
                    last_error_message="Пройдите проверку hh.ru",
                )
            )
            session.add(
                IncidentModel(
                    code="NOTIFICATION_TEST",
                    severity=IncidentSeverity.ERROR,
                    state=IncidentState.OPEN,
                    message="Фоновая проверка завершилась ошибкой",
                )
            )
            application_settings = session.get(ApplicationSettingsModel, 1)
            assert application_settings is not None
            application_settings.timezone_name = "UTC+05:00"
            application_settings.windows_notifications_enabled = True
            application_settings.telegram_enabled = True
            application_settings.email_enabled = True
            application_settings.notification_routing = {
                "NEW_MESSAGE": ["WINDOWS", "TELEGRAM", "EMAIL"],
                "INVITATION": ["WINDOWS", "TELEGRAM", "EMAIL"],
            }

        with database.sessions.begin() as session:
            service = NotificationService(session)
            assert service.collect(account_id, now) == 10
            assert service.collect(account_id, now) == 0

            repository = CommunicationRepository(session)
            first = repository.claim_due_notification(now)
            assert first is not None
            assert first.channel is NotificationChannel.WINDOWS
            failed = repository.mark_notification_failed(
                first.id,
                error_code="temporary_error",
                retry_at=now + timedelta(minutes=5),
            )
            assert failed.state is DeliveryState.FAILED
            assert repository.claim_due_notification(now) is not None
            sent = repository.mark_notification_sent(first.id, now)
            assert sent.state is DeliveryState.SENT
            assert sent.sent_at == now
    finally:
        database.close()
