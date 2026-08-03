# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    ApplicationEventModel,
    ApplicationSettingsModel,
    ApplicationTaskModel,
    AutomationJobModel,
    IncidentModel,
    NotificationModel,
)
from hugin.domain.applications import ApplicationEventType
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
from hugin.services.notifications import (
    NotificationService,
    _compact_text,
    _notification_body,
)

pytestmark = pytest.mark.integration


def test_notification_text_is_compact_and_bounded() -> None:
    assert _compact_text("  строка\nс   пробелами  ", maximum=30) == "строка с пробелами"
    assert _compact_text("123456", maximum=5) == "1234…"
    assert _notification_body("Первая", None, "Вторая") == "Первая\nВторая"
    bounded = _notification_body("я" * 1_100)
    assert len(bounded) == 1_000
    assert bounded.endswith("…")


def test_external_channels_ignore_events_before_activation() -> None:
    activation = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    settings = ApplicationSettingsModel(
        timezone_name="UTC+05:00",
        windows_notifications_enabled=True,
        telegram_enabled=True,
        email_enabled=True,
        notification_routing={
            "NEW_MESSAGE": ["WINDOWS", "TELEGRAM", "EMAIL"],
        },
        notification_cutoffs={
            "NEW_MESSAGE:TELEGRAM": activation.isoformat(),
            "NEW_MESSAGE:EMAIL": activation.isoformat(),
        },
        updated_at=activation,
    )

    assert NotificationService._channels(
        settings,
        "NEW_MESSAGE",
        activation - timedelta(seconds=1),
    ) == (NotificationChannel.WINDOWS,)
    assert NotificationService._channels(
        settings,
        "NEW_MESSAGE",
        activation,
    ) == (
        NotificationChannel.WINDOWS,
        NotificationChannel.TELEGRAM,
        NotificationChannel.EMAIL,
    )


def create_application(session: Session) -> tuple[int, int]:
    account = AccountRepository(session).create("Уведомления")
    resume = ResumeRepository(session).upsert(account.id, "notify-resume", "Python backend")
    vacancy = VacancyRepository(session).upsert(
        VacancyData(
            hh_id="notify-101",
            title="Python-разработчик",
            source_url="https://hh.ru/vacancy/notify-101",
            employer_name="ООО Тест",
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
                    updated_at=now,
                )
            )
            session.add(
                AutomationJobModel(
                    key=f"statuses:{account_id}",
                    kind=AutomationJobKind.STATUSES,
                    state=AutomationJobState.BLOCKED,
                    account_id=account_id,
                    interval_seconds=1_800,
                    last_error_code="CAPTCHA_REQUIRED",
                    last_error_message="Пройдите проверку hh.ru",
                    updated_at=now,
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
            session.add(
                ApplicationEventModel(
                    application_id=application_id,
                    event_type=ApplicationEventType.APPLIED,
                    payload={"hh_status": "APPLIED", "source": "hh.ru"},
                    created_at=now,
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
            activation = (now - timedelta(minutes=1)).isoformat()
            application_settings.notification_cutoffs = {
                f"{event}:{channel}": activation
                for event in ("NEW_MESSAGE", "INVITATION")
                for channel in ("TELEGRAM", "EMAIL")
            }

        with database.sessions.begin() as session:
            service = NotificationService(session)
            assert service.collect(account_id, now) == 10
            assert service.collect(account_id, now) == 0
            failed_jobs = session.scalars(
                select(AutomationJobModel).where(
                    AutomationJobModel.last_error_code == "CAPTCHA_REQUIRED"
                )
            ).all()
            for job in failed_jobs:
                job.last_error_code = None
                job.last_error_message = None
                job.updated_at = now + timedelta(minutes=5)
            session.flush()
            assert service.collect(account_id, now + timedelta(minutes=5)) == 0
            for job in failed_jobs:
                job.last_error_code = "CAPTCHA_REQUIRED"
                job.last_error_message = "Пройдите проверку hh.ru повторно"
                job.updated_at = now + timedelta(minutes=10)
            session.flush()
            assert service.collect(account_id, now + timedelta(minutes=10)) == 1
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(NotificationModel)
                    .where(NotificationModel.event_type == "AUTH_REQUIRED")
                )
                == 2
            )
            summary = session.scalar(
                select(NotificationModel).where(NotificationModel.event_type == "DAILY_SUMMARY")
            )
            assert summary is not None
            assert "Отправлено: 0." in str(summary.payload["body"])
            new_message = session.scalar(
                select(NotificationModel).where(
                    NotificationModel.event_type == "NEW_MESSAGE",
                    NotificationModel.channel == NotificationChannel.WINDOWS,
                )
            )
            assert new_message is not None
            assert new_message.payload["body"] == (
                "Вакансия: Python-разработчик\nРаботодатель: ООО Тест\nСообщение: Здравствуйте!"
            )
            invitation = session.scalar(
                select(NotificationModel).where(
                    NotificationModel.event_type == "INVITATION",
                    NotificationModel.channel == NotificationChannel.WINDOWS,
                )
            )
            assert invitation is not None
            assert invitation.payload["body"] == (
                "Вакансия: Python-разработчик\nРаботодатель: ООО Тест\nПриглашение: Собеседование"
            )

            repository = CommunicationRepository(session)
            first = repository.claim_due_notification(now)
            assert first is not None
            assert first.channel is NotificationChannel.WINDOWS
            assert first.payload["action_url"] == "https://hh.ru/vacancy/notify-101"
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


def test_unconfigured_notification_channel_is_not_retried(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    try:
        with database.sessions.begin() as session:
            repository = CommunicationRepository(session)
            notification = repository.enqueue_notification(
                deduplication_key="email-without-credentials",
                event_type="NEW_MESSAGE",
                channel=NotificationChannel.EMAIL,
                payload={"title": "Hugin", "body": "Новое сообщение"},
                scheduled_at=now,
            )
            repository.mark_notification_failed(
                notification.id,
                error_code="EMAIL_NOT_CONFIGURED",
                retry_at=now,
            )

        with database.sessions.begin() as session:
            assert (
                CommunicationRepository(session).claim_due_notification(now + timedelta(days=1))
                is None
            )
    finally:
        database.close()


def test_unconfigured_notification_service_is_not_retried(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    try:
        with database.sessions.begin() as session:
            repository = CommunicationRepository(session)
            notification = repository.enqueue_notification(
                deduplication_key="gateway-without-credentials",
                event_type="NEW_MESSAGE",
                channel=NotificationChannel.TELEGRAM,
                payload={"title": "Hugin", "body": "Новое сообщение"},
                scheduled_at=now,
            )
            repository.mark_notification_failed(
                notification.id,
                error_code="NOTIFICATION_SERVICE_NOT_CONFIGURED",
                retry_at=now,
            )

        with database.sessions.begin() as session:
            assert (
                CommunicationRepository(session).claim_due_notification(now + timedelta(days=1))
                is None
            )
    finally:
        database.close()
