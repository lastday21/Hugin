# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime, time, tzinfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationModel,
    ApplicationSettingsModel,
    ApplicationTaskModel,
    AutomationJobModel,
    IncidentModel,
    InvitationModel,
    NotificationModel,
    RecruiterMessageModel,
    ScreeningFormModel,
    VacancyModel,
)
from hugin.domain.applications import ApplicationState
from hugin.domain.content import (
    IncidentSeverity,
    IncidentState,
    InvitationState,
    MessageDirection,
    NotificationChannel,
)
from hugin.domain.tasks import TaskState
from hugin.domain.time import timezone_by_name
from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.communications import CommunicationRepository
from hugin.services.ui_communications import WINDOWS_NOTIFICATION_EVENTS


class NotificationService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._applications = ApplicationRepository(session)
        self._communications = CommunicationRepository(session)

    def collect(self, account_id: int, now: datetime | None = None) -> int:
        selected_at = now or datetime.now(UTC)
        created = 0

        incoming = self._session.execute(
            select(RecruiterMessageModel.id, ApplicationModel.id, VacancyModel.title)
            .join(ApplicationModel, ApplicationModel.id == RecruiterMessageModel.application_id)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .where(
                ApplicationModel.account_id == account_id,
                RecruiterMessageModel.direction == MessageDirection.INCOMING,
            )
        )
        for message_id, application_id, vacancy_title in incoming:
            created += self.enqueue_event(
                account_id=account_id,
                event_type="NEW_MESSAGE",
                source_key=f"message:{message_id}",
                title="Новое сообщение от работодателя",
                body=f"Отклик: {vacancy_title}",
                application_id=application_id,
                scheduled_at=selected_at,
            )

        invitations = self._session.execute(
            select(InvitationModel.id, ApplicationModel.id, VacancyModel.title)
            .join(ApplicationModel, ApplicationModel.id == InvitationModel.application_id)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .where(
                ApplicationModel.account_id == account_id,
                InvitationModel.state != InvitationState.CLOSED,
            )
        )
        for invitation_id, application_id, vacancy_title in invitations:
            created += self.enqueue_event(
                account_id=account_id,
                event_type="INVITATION",
                source_key=f"invitation:{invitation_id}",
                title="Приглашение от работодателя",
                body=f"Отклик: {vacancy_title}",
                application_id=application_id,
                scheduled_at=selected_at,
            )

        tasks = self._session.execute(
            select(
                ApplicationTaskModel.id,
                ApplicationTaskModel.state,
                ApplicationModel.id,
                VacancyModel.title,
            )
            .join(ApplicationModel, ApplicationModel.id == ApplicationTaskModel.application_id)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationTaskModel.state.in_(
                    {
                        TaskState.INPUT_REQUIRED,
                        TaskState.REVIEW_REQUIRED,
                        TaskState.UNKNOWN_RESULT,
                    }
                ),
            )
        )
        for task_id, state, application_id, vacancy_title in tasks:
            event_type = "UNKNOWN_RESULT" if state is TaskState.UNKNOWN_RESULT else "FORM_REQUIRED"
            title = (
                "Нужно проверить результат отклика"
                if state is TaskState.UNKNOWN_RESULT
                else "Нужны данные для анкеты"
            )
            created += self.enqueue_event(
                account_id=account_id,
                event_type=event_type,
                source_key=f"task:{task_id}:{state.value}",
                title=title,
                body=f"Вакансия: {vacancy_title}",
                application_id=application_id,
                scheduled_at=selected_at,
            )

        jobs = self._session.scalars(
            select(AutomationJobModel).where(
                AutomationJobModel.account_id == account_id,
                AutomationJobModel.last_error_code.is_not(None),
            )
        )
        for job in jobs:
            job_event_type: str | None = (
                "ACCOUNT_WARNING"
                if job.last_error_code == "ACCOUNT_WARNING"
                else "AUTH_REQUIRED"
                if job.last_error_code
                in {
                    "AUTH_REQUIRED",
                    "CAPTCHA_REQUIRED",
                    "CREDENTIALS_REQUIRED",
                    "CONFIRMATION_REQUIRED",
                    "INVALID_CREDENTIALS",
                    "MANUAL_ACTION_REQUIRED",
                }
                else None
            )
            if job_event_type is None:
                continue
            created += self.enqueue_event(
                account_id=account_id,
                event_type=job_event_type,
                source_key=f"job:{job.key}:{job.last_error_code}",
                title=(
                    "Предупреждение аккаунта hh.ru"
                    if job_event_type == "ACCOUNT_WARNING"
                    else "Нужно восстановить вход в hh.ru"
                ),
                body=job.last_error_message or "Откройте Hugin и завершите проверку.",
                scheduled_at=selected_at,
            )

        incidents = self._session.scalars(
            select(IncidentModel).where(
                IncidentModel.state == IncidentState.OPEN,
                IncidentModel.severity.in_(
                    {
                        IncidentSeverity.ERROR,
                        IncidentSeverity.CRITICAL,
                    }
                ),
            )
        )
        for incident in incidents:
            created += self.enqueue_event(
                account_id=account_id,
                event_type="CRITICAL_ERROR",
                source_key=f"incident:{incident.id}",
                title="Hugin требует внимания",
                body=incident.message[:500],
                incident_id=incident.id,
                scheduled_at=selected_at,
            )

        settings = self._settings()
        local_zone = timezone_by_name(settings.timezone_name)
        local_now = selected_at.astimezone(local_zone)
        if local_now.hour >= 20:
            created += self._enqueue_daily_summary(
                account_id,
                selected_at,
                local_zone,
            )
        return created

    def enqueue_event(
        self,
        *,
        account_id: int,
        event_type: str,
        source_key: str,
        title: str,
        body: str,
        scheduled_at: datetime,
        application_id: int | None = None,
        incident_id: int | None = None,
    ) -> int:
        selected_event = event_type.strip().upper()
        if selected_event not in WINDOWS_NOTIFICATION_EVENTS:
            raise ValueError("Неизвестный вид уведомления")
        settings = self._settings()
        channels = self._channels(settings, selected_event)
        created = 0
        for channel in channels:
            key = f"{source_key}:{selected_event}:{channel.value}"[:128]
            before = self._session.scalar(
                select(func.count())
                .select_from(NotificationModel)
                .where(NotificationModel.deduplication_key == key)
            )
            self._communications.enqueue_notification(
                deduplication_key=key,
                event_type=selected_event,
                channel=channel,
                payload={"title": title[:200], "body": body[:1000]},
                scheduled_at=scheduled_at,
                application_id=application_id,
                incident_id=incident_id,
            )
            created += int(not before)
        return created

    def _enqueue_daily_summary(
        self,
        account_id: int,
        now: datetime,
        local_zone: tzinfo,
    ) -> int:
        local_now = now.astimezone(local_zone)
        local_date = local_now.date()
        day_start = datetime.combine(local_date, time.min, tzinfo=local_now.tzinfo).astimezone(UTC)
        found = self._session.scalar(
            select(func.count())
            .select_from(VacancyModel)
            .where(VacancyModel.created_at >= day_start)
        )
        queued = self._session.scalar(
            select(func.count())
            .select_from(ApplicationModel)
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationModel.created_at >= day_start,
            )
        )
        sent = self._applications.count_applied_since(account_id, day_start)
        viewed = self._session.scalar(
            select(func.count())
            .select_from(ApplicationModel)
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationModel.state == ApplicationState.VIEWED,
                ApplicationModel.updated_at >= day_start,
            )
        )
        rejected = self._session.scalar(
            select(func.count())
            .select_from(ApplicationModel)
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationModel.state == ApplicationState.REJECTED,
                ApplicationModel.updated_at >= day_start,
            )
        )
        incoming = self._session.scalar(
            select(func.count())
            .select_from(RecruiterMessageModel)
            .join(ApplicationModel)
            .where(
                ApplicationModel.account_id == account_id,
                RecruiterMessageModel.direction == MessageDirection.INCOMING,
                RecruiterMessageModel.received_at >= day_start,
            )
        )
        invitations = self._session.scalar(
            select(func.count())
            .select_from(InvitationModel)
            .join(ApplicationModel)
            .where(
                ApplicationModel.account_id == account_id,
                InvitationModel.created_at >= day_start,
            )
        )
        forms = self._session.scalar(
            select(func.count())
            .select_from(ScreeningFormModel)
            .join(ApplicationModel)
            .where(
                ApplicationModel.account_id == account_id,
                ScreeningFormModel.created_at >= day_start,
            )
        )
        return self.enqueue_event(
            account_id=account_id,
            event_type="DAILY_SUMMARY",
            source_key=f"summary:{local_date.isoformat()}",
            title="Итоги работы Hugin",
            body=(
                f"Найдено: {found or 0}. В очереди: {queued or 0}. "
                f"Отправлено: {sent or 0}. Просмотров: {viewed or 0}. "
                f"Отказов: {rejected or 0}. "
                f"Новых сообщений: {incoming or 0}. "
                f"Приглашений: {invitations or 0}. "
                f"Анкет: {forms or 0}."
            ),
            scheduled_at=now,
        )

    def _settings(self) -> ApplicationSettingsModel:
        settings = self._session.get(ApplicationSettingsModel, 1)
        if settings is None:
            raise LookupError("Настройки уведомлений не найдены")
        return settings

    @staticmethod
    def _channels(
        settings: ApplicationSettingsModel,
        event_type: str,
    ) -> tuple[NotificationChannel, ...]:
        raw = settings.notification_routing.get(event_type)
        values = raw if isinstance(raw, list) else ["WINDOWS"]
        enabled = {
            NotificationChannel.WINDOWS: settings.windows_notifications_enabled,
            NotificationChannel.TELEGRAM: settings.telegram_enabled,
            NotificationChannel.EMAIL: settings.email_enabled,
        }
        channels: list[NotificationChannel] = []
        for value in values:
            try:
                channel = NotificationChannel(str(value))
            except ValueError:
                continue
            if enabled[channel] and channel not in channels:
                channels.append(channel)
        return tuple(channels)
