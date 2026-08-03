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
from hugin.domain.time import as_utc, timezone_by_name
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
            select(
                RecruiterMessageModel.id,
                ApplicationModel.id,
                RecruiterMessageModel.body,
                RecruiterMessageModel.received_at,
                RecruiterMessageModel.created_at,
                VacancyModel.title,
                VacancyModel.employer_name,
                VacancyModel.source_url,
            )
            .join(ApplicationModel, ApplicationModel.id == RecruiterMessageModel.application_id)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .where(
                ApplicationModel.account_id == account_id,
                RecruiterMessageModel.direction == MessageDirection.INCOMING,
            )
        )
        for (
            message_id,
            application_id,
            message_body,
            received_at,
            message_created_at,
            vacancy_title,
            employer_name,
            source_url,
        ) in incoming:
            message_preview = _compact_text(message_body, maximum=600)
            created += self.enqueue_event(
                account_id=account_id,
                event_type="NEW_MESSAGE",
                source_key=f"message:{message_id}",
                title="Новое сообщение от работодателя",
                body=_notification_body(
                    f"Вакансия: {vacancy_title}",
                    f"Работодатель: {employer_name}" if employer_name else None,
                    f"Сообщение: {message_preview}" if message_preview else None,
                ),
                occurred_at=received_at or message_created_at,
                action_url=source_url,
                application_id=application_id,
                scheduled_at=selected_at,
            )

        invitations = self._session.execute(
            select(
                InvitationModel.id,
                ApplicationModel.id,
                InvitationModel.title,
                InvitationModel.details,
                InvitationModel.created_at,
                VacancyModel.title,
                VacancyModel.employer_name,
                VacancyModel.source_url,
            )
            .join(ApplicationModel, ApplicationModel.id == InvitationModel.application_id)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .where(
                ApplicationModel.account_id == account_id,
                InvitationModel.state != InvitationState.CLOSED,
            )
        )
        for (
            invitation_id,
            application_id,
            invitation_title,
            invitation_details,
            invitation_created_at,
            vacancy_title,
            employer_name,
            source_url,
        ) in invitations:
            details_preview = _compact_text(invitation_details or "", maximum=500)
            created += self.enqueue_event(
                account_id=account_id,
                event_type="INVITATION",
                source_key=f"invitation:{invitation_id}",
                title="Приглашение от работодателя",
                body=_notification_body(
                    f"Вакансия: {vacancy_title}",
                    f"Работодатель: {employer_name}" if employer_name else None,
                    f"Приглашение: {invitation_title}",
                    f"Подробности: {details_preview}" if details_preview else None,
                ),
                occurred_at=invitation_created_at,
                action_url=source_url,
                application_id=application_id,
                scheduled_at=selected_at,
            )

        tasks = self._session.execute(
            select(
                ApplicationTaskModel.id,
                ApplicationTaskModel.state,
                ApplicationTaskModel.updated_at,
                ApplicationModel.id,
                VacancyModel.title,
                VacancyModel.employer_name,
                VacancyModel.source_url,
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
        for (
            task_id,
            state,
            task_updated_at,
            application_id,
            vacancy_title,
            employer_name,
            source_url,
        ) in tasks:
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
                body=_notification_body(
                    f"Вакансия: {vacancy_title}",
                    f"Работодатель: {employer_name}" if employer_name else None,
                ),
                occurred_at=task_updated_at,
                action_url=source_url,
                application_id=application_id,
                scheduled_at=selected_at,
            )

        jobs = self._session.scalars(
            select(AutomationJobModel).where(
                AutomationJobModel.account_id == account_id,
                AutomationJobModel.last_error_code.is_not(None),
            )
        )
        account_errors: dict[tuple[str, str], list[AutomationJobModel]] = {}
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
            error_code = str(job.last_error_code)
            account_errors.setdefault((error_code, job_event_type), []).append(job)

        for (error_code, job_event_type), failed_jobs in account_errors.items():
            episode_at = min(as_utc(job.updated_at) for job in failed_jobs)
            representative = min(failed_jobs, key=lambda job: as_utc(job.updated_at))
            created += self.enqueue_event(
                account_id=account_id,
                event_type=job_event_type,
                source_key=(
                    f"account:{account_id}:{error_code}:"
                    f"{episode_at.isoformat(timespec='microseconds')}"
                ),
                title=(
                    "Предупреждение аккаунта hh.ru"
                    if job_event_type == "ACCOUNT_WARNING"
                    else "Нужно восстановить вход в hh.ru"
                ),
                body=(representative.last_error_message or "Откройте Hugin и завершите проверку."),
                occurred_at=episode_at,
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
                occurred_at=incident.created_at,
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
        occurred_at: datetime,
        scheduled_at: datetime,
        action_url: str | None = None,
        application_id: int | None = None,
        incident_id: int | None = None,
    ) -> int:
        selected_event = event_type.strip().upper()
        if selected_event not in WINDOWS_NOTIFICATION_EVENTS:
            raise ValueError("Неизвестный вид уведомления")
        settings = self._settings()
        selected_occurred_at = as_utc(occurred_at)
        channels = self._channels(settings, selected_event, selected_occurred_at)
        created = 0
        for channel in channels:
            key = f"{source_key}:{selected_event}:{channel.value}"[:128]
            before = self._session.scalar(
                select(func.count())
                .select_from(NotificationModel)
                .where(NotificationModel.deduplication_key == key)
            )
            payload: dict[str, object] = {
                "title": title[:200],
                "body": body[:1000],
                "occurred_at": selected_occurred_at.isoformat(),
            }
            if action_url:
                payload["action_url"] = action_url
            self._communications.enqueue_notification(
                deduplication_key=key,
                event_type=selected_event,
                channel=channel,
                payload=payload,
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
            occurred_at=now,
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
        occurred_at: datetime,
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
            if not enabled[channel] or channel in channels:
                continue
            cutoff = NotificationService._notification_cutoff(settings, event_type, channel)
            if cutoff is not None and occurred_at < cutoff:
                continue
            channels.append(channel)
        return tuple(channels)

    @staticmethod
    def _notification_cutoff(
        settings: ApplicationSettingsModel,
        event_type: str,
        channel: NotificationChannel,
    ) -> datetime | None:
        raw = settings.notification_cutoffs.get(f"{event_type}:{channel.value}")
        if isinstance(raw, str):
            try:
                return as_utc(datetime.fromisoformat(raw))
            except ValueError:
                pass
        if channel is not NotificationChannel.WINDOWS:
            return as_utc(settings.updated_at)
        return None


def _compact_text(value: str, *, maximum: int) -> str:
    selected = " ".join(value.split())
    if len(selected) <= maximum:
        return selected
    return f"{selected[: maximum - 1].rstrip()}…"


def _notification_body(*lines: str | None) -> str:
    body = "\n".join(line for line in lines if line)
    if len(body) <= 1_000:
        return body
    return f"{body[:999].rstrip()}…"
