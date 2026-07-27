from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationModel,
    ApplicationSettingsModel,
    HhAccountModel,
    InvitationModel,
    RecruiterMessageModel,
    VacancyModel,
)
from hugin.domain.content import InvitationState, MessageDirection, RecruiterMessageState
from hugin.domain.directions import ConfigPayload

WINDOWS_NOTIFICATION_EVENTS = (
    "NEW_MESSAGE",
    "INVITATION",
    "REPLY_REQUIRED",
    "FORM_REQUIRED",
    "AUTH_REQUIRED",
    "ACCOUNT_WARNING",
    "UNKNOWN_RESULT",
    "CRITICAL_ERROR",
    "DAILY_SUMMARY",
)


@dataclass(frozen=True, slots=True)
class UiRecruiterMessage:
    id: int
    direction: str
    body: str
    state: str
    occurred_at: datetime
    read_at: datetime | None
    content_hash: str | None
    content_version: int


@dataclass(frozen=True, slots=True)
class UiConversation:
    application_id: int
    vacancy_id: str
    vacancy_title: str
    company: str
    source_url: str
    unread_count: int
    needs_reply: bool
    messages: tuple[UiRecruiterMessage, ...]


@dataclass(frozen=True, slots=True)
class UiInvitation:
    id: int
    application_id: int
    vacancy_id: str
    vacancy_title: str
    company: str
    source_url: str
    title: str
    details: str | None
    interview_at: datetime | None
    booking_url: str | None
    state: str
    seen_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UiCommunications:
    conversations: tuple[UiConversation, ...]
    invitations: tuple[UiInvitation, ...]
    unread_messages: int
    unseen_invitations: int
    notification_settings: UiNotificationSettings


@dataclass(frozen=True, slots=True)
class UiNotificationSettings:
    windows_enabled: bool
    telegram_enabled: bool
    email_enabled: bool
    routing: dict[str, tuple[str, ...]]


class UiCommunicationService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, account_id: int) -> UiCommunications:
        if self._session.get(HhAccountModel, account_id) is None:
            raise LookupError("Аккаунт hh.ru не найден")
        settings = self._session.get(ApplicationSettingsModel, 1)
        if settings is None:
            raise LookupError("Настройки уведомлений не найдены")

        message_rows = tuple(
            self._session.execute(
                select(
                    RecruiterMessageModel,
                    ApplicationModel,
                    VacancyModel,
                )
                .join(
                    ApplicationModel,
                    ApplicationModel.id == RecruiterMessageModel.application_id,
                )
                .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
                .where(ApplicationModel.account_id == account_id)
                .order_by(
                    RecruiterMessageModel.application_id,
                    RecruiterMessageModel.created_at,
                    RecruiterMessageModel.id,
                )
            )
        )
        grouped: dict[
            int,
            list[tuple[RecruiterMessageModel, ApplicationModel, VacancyModel]],
        ] = defaultdict(list)
        for message, application, vacancy in message_rows:
            grouped[application.id].append((message, application, vacancy))

        conversations: list[UiConversation] = []
        for rows in grouped.values():
            application = rows[0][1]
            vacancy = rows[0][2]
            messages = tuple(self._message(message) for message, _application, _vacancy in rows)
            unread_count = sum(
                message.direction is MessageDirection.INCOMING and message.read_at is None
                for message, _application, _vacancy in rows
            )
            latest = rows[-1][0]
            conversations.append(
                UiConversation(
                    application_id=application.id,
                    vacancy_id=vacancy.hh_id,
                    vacancy_title=vacancy.title,
                    company=vacancy.employer_name or "Компания не указана",
                    source_url=vacancy.source_url,
                    unread_count=unread_count,
                    needs_reply=self._needs_reply(latest),
                    messages=messages,
                )
            )

        invitation_rows = self._session.execute(
            select(InvitationModel, ApplicationModel, VacancyModel)
            .join(ApplicationModel, ApplicationModel.id == InvitationModel.application_id)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .where(ApplicationModel.account_id == account_id)
            .order_by(
                InvitationModel.created_at.desc(),
                InvitationModel.id.desc(),
            )
        )
        invitations = tuple(
            UiInvitation(
                id=invitation.id,
                application_id=application.id,
                vacancy_id=vacancy.hh_id,
                vacancy_title=vacancy.title,
                company=vacancy.employer_name or "Компания не указана",
                source_url=vacancy.source_url,
                title=invitation.title,
                details=invitation.details,
                interview_at=invitation.interview_at,
                booking_url=invitation.booking_url,
                state=invitation.state.value,
                seen_at=invitation.seen_at,
                created_at=invitation.created_at,
            )
            for invitation, application, vacancy in invitation_rows
        )
        conversations.sort(
            key=lambda item: item.messages[-1].occurred_at,
            reverse=True,
        )
        return UiCommunications(
            conversations=tuple(conversations),
            invitations=invitations,
            unread_messages=sum(item.unread_count for item in conversations),
            unseen_invitations=sum(
                invitation.seen_at is None and invitation.state != InvitationState.CLOSED.value
                for invitation in invitations
            ),
            notification_settings=UiNotificationSettings(
                windows_enabled=settings.windows_notifications_enabled,
                telegram_enabled=settings.telegram_enabled,
                email_enabled=settings.email_enabled,
                routing=self._routing(settings.notification_routing),
            ),
        )

    def update_notification_settings(
        self,
        *,
        account_id: int,
        windows_enabled: bool,
        telegram_enabled: bool,
        email_enabled: bool,
        events: tuple[str, ...],
    ) -> UiCommunications:
        if self._session.get(HhAccountModel, account_id) is None:
            raise LookupError("Аккаунт hh.ru не найден")
        settings = self._session.get(ApplicationSettingsModel, 1)
        if settings is None:
            raise LookupError("Настройки уведомлений не найдены")
        selected_events = tuple(dict.fromkeys(event.strip().upper() for event in events))
        unknown = set(selected_events) - set(WINDOWS_NOTIFICATION_EVENTS)
        if unknown:
            raise ValueError("Выбран неизвестный вид уведомления")

        selected_channels = [
            channel
            for channel, enabled in (
                ("WINDOWS", windows_enabled),
                ("TELEGRAM", telegram_enabled),
                ("EMAIL", email_enabled),
            )
            if enabled
        ]
        updated: dict[str, list[str]] = {event: [] for event in WINDOWS_NOTIFICATION_EVENTS}
        for event in selected_events:
            updated[event].extend(selected_channels)
        settings.windows_notifications_enabled = windows_enabled
        settings.telegram_enabled = telegram_enabled
        settings.email_enabled = email_enabled
        routing: ConfigPayload = {event: channels for event, channels in updated.items()}
        settings.notification_routing = routing
        self._session.flush()
        return self.get(account_id)

    @staticmethod
    def _message(message: RecruiterMessageModel) -> UiRecruiterMessage:
        occurred_at = message.received_at or message.sent_at or message.created_at
        return UiRecruiterMessage(
            id=message.id,
            direction=message.direction.value,
            body=message.body,
            state=message.state.value,
            occurred_at=occurred_at,
            read_at=message.read_at,
            content_hash=message.content_hash,
            content_version=message.version,
        )

    @staticmethod
    def _needs_reply(message: RecruiterMessageModel) -> bool:
        if message.direction is MessageDirection.INCOMING:
            return True
        return message.state in {
            RecruiterMessageState.DRAFT,
            RecruiterMessageState.REVIEW_REQUIRED,
        }

    @staticmethod
    def _all_routing(value: dict[str, object]) -> dict[str, list[str]]:
        return {
            str(event): [str(channel) for channel in channels if isinstance(channel, str)]
            for event, channels in value.items()
            if isinstance(channels, list)
        }

    @classmethod
    def _routing(cls, value: dict[str, object]) -> dict[str, tuple[str, ...]]:
        routing = {event: tuple(channels) for event, channels in cls._all_routing(value).items()}
        if not any(event in routing for event in WINDOWS_NOTIFICATION_EVENTS):
            routing.update({event: ("WINDOWS",) for event in WINDOWS_NOTIFICATION_EVENTS})
        else:
            for event in WINDOWS_NOTIFICATION_EVENTS:
                routing.setdefault(event, ())
        return routing
