from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationModel,
    ApplicationSettingsModel,
    HhAccountModel,
    InvitationModel,
    RecruiterMessageActionModel,
    RecruiterMessageModel,
    VacancyModel,
)
from hugin.domain.content import (
    InvitationState,
    MessageDirection,
    RecruiterActionKind,
    RecruiterActionState,
    RecruiterMessageState,
)
from hugin.domain.directions import ConfigPayload
from hugin.domain.time import as_utc
from hugin.repositories.communications import CommunicationRepository
from hugin.services.ai_prompts import (
    AI_MODEL_OPTIONS,
    AI_REASONING_OPTIONS,
    DEFAULT_AI_PROMPTS,
    AiModelOption,
    AiPromptSettings,
    AiPromptSettingsService,
    AiReasoningOption,
)
from hugin.services.recruiter_reply_policy import (
    RecruiterReplyDisposition,
    classify_recruiter_reply,
    repeated_incoming_already_answered,
    unresolved_action_position_before_invitation_reminder,
)

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
    ai_model_settings: UiAiModelSettings
    ai_prompt_settings: UiAiPromptSettings


@dataclass(frozen=True, slots=True)
class UiNotificationSettings:
    windows_enabled: bool
    telegram_enabled: bool
    email_enabled: bool
    routing: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class UiAiModelSettings:
    selected: str
    options: tuple[AiModelOption, ...]
    reasoning_effort: str
    reasoning_options: tuple[AiReasoningOption, ...]


@dataclass(frozen=True, slots=True)
class UiAiPromptSettings:
    resume: str
    cover_letter: str
    recruiter_reply: str
    defaults: AiPromptSettings


class UiCommunicationService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, account_id: int) -> UiCommunications:
        if self._session.get(HhAccountModel, account_id) is None:
            raise LookupError("Аккаунт hh.ru не найден")
        settings = self._session.get(ApplicationSettingsModel, 1)
        if settings is None:
            raise LookupError("Настройки уведомлений не найдены")
        CommunicationRepository(self._session).dismiss_expired_message_actions(
            account_id=account_id,
            changed_at=datetime.now(UTC),
        )

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

        actions_by_message: dict[int, list[RecruiterMessageActionModel]] = defaultdict(list)
        for action in self._session.scalars(
            select(RecruiterMessageActionModel)
            .join(
                RecruiterMessageModel,
                RecruiterMessageModel.id == RecruiterMessageActionModel.message_id,
            )
            .join(
                ApplicationModel,
                ApplicationModel.id == RecruiterMessageModel.application_id,
            )
            .where(ApplicationModel.account_id == account_id)
        ):
            actions_by_message[action.message_id].append(action)

        conversations: list[UiConversation] = []
        for rows in grouped.values():
            application = rows[0][1]
            vacancy = rows[0][2]
            message_models = tuple(message for message, _application, _vacancy in rows)
            messages = tuple(self._message(message) for message in message_models)
            unread_count = sum(
                message.direction is MessageDirection.INCOMING and message.read_at is None
                for message, _application, _vacancy in rows
            )
            latest = rows[-1][0]
            latest_incoming = next(
                (
                    message
                    for message, _application, _vacancy in reversed(rows)
                    if message.direction is MessageDirection.INCOMING
                ),
                None,
            )
            needs_reply = self._needs_reply(
                application,
                latest,
                latest_incoming,
                message_models,
                actions_by_message,
            )
            conversations.append(
                UiConversation(
                    application_id=application.id,
                    vacancy_id=vacancy.hh_id,
                    vacancy_title=vacancy.title,
                    company=vacancy.employer_name or "Компания не указана",
                    source_url=vacancy.source_url,
                    unread_count=max(unread_count, 1) if needs_reply else 0,
                    needs_reply=needs_reply,
                    messages=messages,
                )
            )

        invitation_rows = self._session.execute(
            select(InvitationModel, ApplicationModel, VacancyModel)
            .join(ApplicationModel, ApplicationModel.id == InvitationModel.application_id)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .where(
                ApplicationModel.account_id == account_id,
                InvitationModel.state != InvitationState.CLOSED,
            )
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
            ai_model_settings=self._ai_model_settings(),
            ai_prompt_settings=self._ai_prompt_settings(),
        )

    def update_ai_model_settings(
        self,
        *,
        account_id: int,
        model: str,
        reasoning_effort: str,
    ) -> UiCommunications:
        if self._session.get(HhAccountModel, account_id) is None:
            raise LookupError("Аккаунт hh.ru не найден")
        AiPromptSettingsService(self._session).update_model(model, reasoning_effort)
        return self.get(account_id)

    def update_ai_prompt_settings(
        self,
        *,
        account_id: int,
        resume: str,
        cover_letter: str,
        recruiter_reply: str,
    ) -> UiCommunications:
        if self._session.get(HhAccountModel, account_id) is None:
            raise LookupError("Аккаунт hh.ru не найден")
        AiPromptSettingsService(self._session).update(
            resume=resume,
            cover_letter=cover_letter,
            recruiter_reply=recruiter_reply,
        )
        return self.get(account_id)

    def reset_ai_prompt_settings(self, *, account_id: int) -> UiCommunications:
        if self._session.get(HhAccountModel, account_id) is None:
            raise LookupError("Аккаунт hh.ru не найден")
        AiPromptSettingsService(self._session).reset()
        return self.get(account_id)

    def update_notification_settings(
        self,
        *,
        account_id: int,
        windows_enabled: bool,
        telegram_enabled: bool,
        email_enabled: bool,
        events: tuple[str, ...],
        now: datetime | None = None,
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

        changed_at = as_utc(now or datetime.now(UTC))
        previous = self._all_routing(settings.notification_routing)
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
        cutoffs = {
            str(key): str(value)
            for key, value in settings.notification_cutoffs.items()
            if isinstance(value, str)
        }
        active_cutoff_keys: set[str] = set()
        for event, channels in updated.items():
            previous_channels = previous.get(event, [])
            for channel in channels:
                key = f"{event}:{channel}"
                active_cutoff_keys.add(key)
                if channel not in previous_channels:
                    cutoffs[key] = changed_at.isoformat()
                elif key not in cutoffs and channel != "WINDOWS":
                    cutoffs[key] = as_utc(settings.updated_at).isoformat()
        settings.windows_notifications_enabled = windows_enabled
        settings.telegram_enabled = telegram_enabled
        settings.email_enabled = email_enabled
        routing: ConfigPayload = {event: channels for event, channels in updated.items()}
        settings.notification_routing = routing
        settings.notification_cutoffs = {
            key: value for key, value in cutoffs.items() if key in active_cutoff_keys
        }
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
    def _needs_reply(
        application: ApplicationModel,
        message: RecruiterMessageModel,
        latest_incoming: RecruiterMessageModel | None,
        messages: tuple[RecruiterMessageModel, ...],
        actions_by_message: dict[int, list[RecruiterMessageActionModel]],
    ) -> bool:
        if repeated_incoming_already_answered(messages):
            return False
        if latest_incoming is not None:
            disposition = classify_recruiter_reply(application.state, latest_incoming.body)
            action_decision = UiCommunicationService._action_decision(
                actions_by_message.get(latest_incoming.id, []),
                disposition,
            )
            if action_decision is not None:
                return action_decision
        else:
            disposition = RecruiterReplyDisposition.NO_REPLY
        if latest_incoming is not None and disposition is RecruiterReplyDisposition.NO_REPLY:
            action_position = unresolved_action_position_before_invitation_reminder(
                application.state,
                messages,
            )
            if action_position is None:
                return False
            action_message = messages[action_position]
            action_disposition = classify_recruiter_reply(
                application.state,
                action_message.body,
            )
            action_decision = UiCommunicationService._action_decision(
                actions_by_message.get(action_message.id, []),
                action_disposition,
            )
            return True if action_decision is None else action_decision
        if message.direction is MessageDirection.INCOMING:
            return True
        return message.state in {
            RecruiterMessageState.DRAFT,
            RecruiterMessageState.REVIEW_REQUIRED,
            RecruiterMessageState.FAILED,
        }

    @staticmethod
    def _action_decision(
        actions: list[RecruiterMessageActionModel],
        disposition: RecruiterReplyDisposition,
    ) -> bool | None:
        if disposition is RecruiterReplyDisposition.MANUAL:
            if any(
                action.kind is RecruiterActionKind.REPLY
                and action.state is RecruiterActionState.REQUIRED
                for action in actions
            ):
                return True
            relevant = tuple(
                action for action in actions if action.kind is not RecruiterActionKind.REPLY
            )
        elif disposition is RecruiterReplyDisposition.NO_REPLY:
            return None
        elif disposition is RecruiterReplyDisposition.AMBIGUOUS:
            relevant = tuple(
                action for action in actions if action.kind is RecruiterActionKind.REPLY
            )
        else:
            relevant = tuple(
                action
                for action in actions
                if action.kind is RecruiterActionKind.REPLY
                and action.state
                in {
                    RecruiterActionState.COMPLETED,
                    RecruiterActionState.DISMISSED,
                    RecruiterActionState.REQUIRED,
                }
            )
        if not relevant:
            return None
        return any(action.state is RecruiterActionState.REQUIRED for action in relevant)

    def _ai_prompt_settings(self) -> UiAiPromptSettings:
        current = AiPromptSettingsService(self._session).get()
        return UiAiPromptSettings(
            resume=current.resume,
            cover_letter=current.cover_letter,
            recruiter_reply=current.recruiter_reply,
            defaults=DEFAULT_AI_PROMPTS,
        )

    def _ai_model_settings(self) -> UiAiModelSettings:
        settings = AiPromptSettingsService(self._session)
        return UiAiModelSettings(
            selected=settings.get_model(),
            options=AI_MODEL_OPTIONS,
            reasoning_effort=settings.get_reasoning_effort(),
            reasoning_options=AI_REASONING_OPTIONS,
        )

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
