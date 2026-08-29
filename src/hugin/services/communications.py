# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol

from sqlalchemy.orm import Session

from hugin.domain.communications import (
    CommunicationStateError,
    InvitationRecord,
    MessageSendFailureCode,
    MessageSendOutcome,
    MessageSendRequest,
    MessageSendResult,
    NotificationRecord,
    RecruiterMessageActionRecord,
    RecruiterMessageRecord,
    StaleMessageDraftError,
)
from hugin.domain.content import (
    IncidentSeverity,
    NotificationChannel,
    RecruiterActionKind,
    RecruiterActionSource,
    RecruiterActionState,
    RecruiterMessageState,
)
from hugin.domain.directions import ConfigPayload
from hugin.domain.time import as_utc
from hugin.repositories.communications import CommunicationRepository
from hugin.services.incidents import IncidentService

_MESSAGE_SEND_INCIDENT_CODES = (
    "RECRUITER_MESSAGE_SEND_FAILED",
    "RECRUITER_MESSAGE_SEND_UNKNOWN",
)


class MessageSender(Protocol):
    def send(self, request: MessageSendRequest) -> MessageSendResult: ...


class RecordingMessageSender:
    """Record requested sends without opening a browser or using the network."""

    def __init__(
        self,
        outcome: MessageSendOutcome = MessageSendOutcome.SENT,
        failure_code: MessageSendFailureCode | None = None,
    ) -> None:
        self.outcome = outcome
        self.failure_code = failure_code
        self._attempts: list[MessageSendRequest] = []
        self._results: dict[tuple[int, int, str], MessageSendResult] = {}

    @property
    def attempts(self) -> tuple[MessageSendRequest, ...]:
        return tuple(self._attempts)

    def send(self, request: MessageSendRequest) -> MessageSendResult:
        key = (request.message_id, request.content_version, request.content_hash)
        existing = self._results.get(key)
        if existing is not None:
            return existing
        self._attempts.append(request)
        external_id = (
            f"recording:{request.message_id}:{request.content_version}"
            if self.outcome is MessageSendOutcome.SENT
            else None
        )
        result = MessageSendResult(self.outcome, external_id, self.failure_code)
        self._results[key] = result
        return result


class CommunicationService:
    def __init__(self, session: Session, sender: MessageSender) -> None:
        self._repository = CommunicationRepository(session)
        self._sender = sender
        self._incidents = IncidentService(session)

    def messages(self, account_id: int) -> tuple[RecruiterMessageRecord, ...]:
        return self._repository.list_messages_for_account(self._positive_id(account_id))

    def invitations(self, account_id: int) -> tuple[InvitationRecord, ...]:
        return self._repository.list_invitations_for_account(self._positive_id(account_id))

    def save_incoming(
        self,
        *,
        application_id: int,
        hh_id: str,
        body: str,
        received_at: datetime | None = None,
    ) -> RecruiterMessageRecord:
        return self._repository.save_incoming(
            application_id=self._positive_id(application_id),
            hh_id=self._stable_id(hh_id, "сообщения"),
            body=self._body(body),
            received_at=self._now(received_at),
        )

    def mark_incoming_read(
        self,
        *,
        account_id: int,
        message_id: int,
        read_at: datetime | None = None,
    ) -> RecruiterMessageRecord:
        return self._repository.mark_incoming_read(
            self._positive_id(account_id),
            self._positive_id(message_id),
            self._now(read_at),
        )

    def message_actions(self, account_id: int) -> tuple[RecruiterMessageActionRecord, ...]:
        return self._repository.list_message_actions_for_account(self._positive_id(account_id))

    def record_reply_requirement(
        self,
        *,
        account_id: int,
        message_id: int,
        required: bool,
        source: RecruiterActionSource,
        reason_code: str,
        reason: str,
        changed_at: datetime | None = None,
    ) -> RecruiterMessageActionRecord:
        if source not in {RecruiterActionSource.MODEL, RecruiterActionSource.SYSTEM}:
            raise ValueError("Проверку необходимости ответа сохраняет модель или система")
        return self._record_message_action(
            account_id=account_id,
            message_id=message_id,
            kind=RecruiterActionKind.REPLY,
            state=(
                RecruiterActionState.REQUIRED if required else RecruiterActionState.NOT_REQUIRED
            ),
            source=source,
            reason_code=reason_code,
            reason=reason,
            due_at=None,
            changed_at=changed_at,
            preserve_resolved=True,
        )

    def complete_external_action(
        self,
        *,
        account_id: int,
        message_id: int,
        kind: RecruiterActionKind,
        reason_code: str,
        reason: str,
        changed_at: datetime | None = None,
    ) -> RecruiterMessageActionRecord:
        if kind is RecruiterActionKind.REPLY:
            raise ValueError("Ответ в hh.ru нельзя завершить как внешнее действие")
        return self._record_message_action(
            account_id=account_id,
            message_id=message_id,
            kind=kind,
            state=RecruiterActionState.COMPLETED,
            source=RecruiterActionSource.USER,
            reason_code=reason_code,
            reason=reason,
            due_at=None,
            changed_at=changed_at,
            preserve_resolved=False,
        )

    def require_external_action(
        self,
        *,
        account_id: int,
        message_id: int,
        kind: RecruiterActionKind,
        source: RecruiterActionSource,
        reason_code: str,
        reason: str,
        due_at: datetime | None = None,
        changed_at: datetime | None = None,
    ) -> RecruiterMessageActionRecord:
        if kind is RecruiterActionKind.REPLY:
            raise ValueError("Для ответа в hh.ru используется отдельная проверка")
        if source is RecruiterActionSource.MODEL:
            raise ValueError("Модель не может назначать внешнее действие")
        return self._record_message_action(
            account_id=account_id,
            message_id=message_id,
            kind=kind,
            state=RecruiterActionState.REQUIRED,
            source=source,
            reason_code=reason_code,
            reason=reason,
            due_at=due_at,
            changed_at=changed_at,
            preserve_resolved=True,
        )

    def dismiss_incoming_action(
        self,
        *,
        account_id: int,
        message_id: int,
        kind: RecruiterActionKind,
        source: RecruiterActionSource,
        reason_code: str,
        reason: str,
        changed_at: datetime | None = None,
    ) -> RecruiterMessageActionRecord:
        if source is RecruiterActionSource.MODEL:
            raise ValueError("Модель не может исключать этап отбора")
        return self._record_message_action(
            account_id=account_id,
            message_id=message_id,
            kind=kind,
            state=RecruiterActionState.DISMISSED,
            source=source,
            reason_code=reason_code,
            reason=reason,
            due_at=None,
            changed_at=changed_at,
            preserve_resolved=source is not RecruiterActionSource.USER,
        )

    def dismiss_expired_actions(
        self,
        *,
        account_id: int,
        changed_at: datetime | None = None,
    ) -> int:
        return self._repository.dismiss_expired_message_actions(
            account_id=self._positive_id(account_id),
            changed_at=self._now(changed_at),
        )

    def create_outgoing_draft(
        self,
        *,
        application_id: int,
        body: str,
        auto_send_approved: bool = False,
        reply_template_key: str | None = None,
    ) -> RecruiterMessageRecord:
        exact_body = self._body(body)
        selected_template_key = reply_template_key.strip() if reply_template_key else None
        if selected_template_key is not None and len(selected_template_key) > 64:
            raise ValueError("Ключ утверждённого ответа длиннее 64 символов")
        if auto_send_approved and not selected_template_key:
            raise ValueError("Для автоматической отправки нужен ключ утверждённого ответа")
        return self._repository.create_outgoing_draft(
            application_id=self._positive_id(application_id),
            body=exact_body,
            content_hash=self.content_hash(exact_body),
            auto_send_approved=auto_send_approved,
            reply_template_key=selected_template_key,
        )

    def edit_outgoing_draft(
        self,
        *,
        account_id: int,
        message_id: int,
        body: str,
    ) -> RecruiterMessageRecord:
        exact_body = self._body(body)
        return self._repository.edit_outgoing_draft(
            account_id=self._positive_id(account_id),
            message_id=self._positive_id(message_id),
            body=exact_body,
            content_hash=self.content_hash(exact_body),
        )

    def confirm_outgoing_draft(
        self,
        *,
        account_id: int,
        message_id: int,
        content_version: int,
        content_hash: str,
        confirmed_at: datetime | None = None,
    ) -> RecruiterMessageRecord:
        return self._repository.confirm_outgoing_draft(
            account_id=self._positive_id(account_id),
            message_id=self._positive_id(message_id),
            content_version=self._positive_id(content_version),
            content_hash=self._hash(content_hash),
            confirmed_at=self._now(confirmed_at),
        )

    def approve_outgoing_for_automatic_send(
        self,
        *,
        account_id: int,
        message_id: int,
        content_version: int,
        content_hash: str,
        approval_key: str,
    ) -> RecruiterMessageRecord:
        selected_key = approval_key.strip()
        if not selected_key or len(selected_key) > 64:
            raise ValueError("Некорректный ключ автоматического ответа")
        return self._repository.approve_outgoing_for_automatic_send(
            account_id=self._positive_id(account_id),
            message_id=self._positive_id(message_id),
            content_version=self._positive_id(content_version),
            content_hash=self._hash(content_hash),
            approval_key=selected_key,
        )

    def confirm_outgoing_retry(
        self,
        *,
        account_id: int,
        message_id: int,
        content_version: int,
        content_hash: str,
        confirmed_at: datetime | None = None,
    ) -> RecruiterMessageRecord:
        selected_account_id = self._positive_id(account_id)
        selected_message_id = self._positive_id(message_id)
        selected_version = self._positive_id(content_version)
        selected_hash = self._hash(content_hash)
        message = self._repository.lock_message_for_send(
            selected_account_id,
            selected_message_id,
        )
        self._require_exact_version(message, selected_version, selected_hash)
        if message.state is RecruiterMessageState.CONFIRMED:
            return message
        if message.state is not RecruiterMessageState.FAILED:
            raise CommunicationStateError(
                "Повтор разрешён только для подтверждённого ответа или явной ошибки отправки"
            )
        return self._repository.confirm_outgoing_draft(
            account_id=selected_account_id,
            message_id=selected_message_id,
            content_version=selected_version,
            content_hash=selected_hash,
            confirmed_at=self._now(confirmed_at),
        )

    def send_confirmed(
        self,
        *,
        account_id: int,
        message_id: int,
        content_version: int,
        content_hash: str,
        finished_at: datetime | None = None,
    ) -> RecruiterMessageRecord:
        selected_account_id = self._positive_id(account_id)
        selected_message_id = self._positive_id(message_id)
        selected_version = self._positive_id(content_version)
        selected_hash = self._hash(content_hash)
        message = self._repository.lock_message_for_send(
            selected_account_id,
            selected_message_id,
        )
        self._require_exact_version(message, selected_version, selected_hash)
        if message.state in {
            RecruiterMessageState.SENT,
            RecruiterMessageState.FAILED,
            RecruiterMessageState.UNKNOWN_RESULT,
        }:
            if message.state is RecruiterMessageState.SENT:
                self._repository.complete_reply_action_for_sent_outgoing(
                    account_id=selected_account_id,
                    message_id=selected_message_id,
                    completed_at=self._now(finished_at),
                )
            return message
        if message.state is not RecruiterMessageState.CONFIRMED:
            raise CommunicationStateError(
                "Отправить можно только явно подтверждённую версию черновика"
            )
        request = MessageSendRequest(
            message_id=message.id,
            application_id=message.application_id,
            body=message.body,
            content_hash=selected_hash,
            content_version=selected_version,
        )
        result = self._sender.send(request)
        recorded = self._repository.record_send_outcome(
            account_id=selected_account_id,
            message_id=selected_message_id,
            content_version=selected_version,
            content_hash=selected_hash,
            outcome=result.outcome,
            external_id=result.external_id,
            finished_at=self._now(finished_at),
        )
        if recorded.state is RecruiterMessageState.SENT:
            self._repository.complete_reply_action_for_sent_outgoing(
                account_id=selected_account_id,
                message_id=selected_message_id,
                completed_at=recorded.sent_at or self._now(finished_at),
            )
        self._record_send_incident(recorded, result.failure_code)
        return recorded

    def _record_send_incident(
        self,
        message: RecruiterMessageRecord,
        failure_code: MessageSendFailureCode | None,
    ) -> None:
        if message.state is RecruiterMessageState.FAILED:
            selected_failure_code = failure_code or MessageSendFailureCode.UNSPECIFIED
            self._incidents.report(
                code="RECRUITER_MESSAGE_SEND_FAILED",
                severity=IncidentSeverity.ERROR,
                message=(
                    f"Не удалось отправить ответ работодателю; сообщение №{message.id} "
                    "оставлено для безопасного повтора после проверки причины. "
                    f"Код этапа: {selected_failure_code.value}."
                ),
                scope_type="recruiter_message",
                scope_id=message.id,
            )
            return
        if message.state is RecruiterMessageState.UNKNOWN_RESULT:
            self._incidents.report(
                code="RECRUITER_MESSAGE_SEND_UNKNOWN",
                severity=IncidentSeverity.CRITICAL,
                message=(
                    f"Результат отправки ответа работодателю неизвестен; сообщение "
                    f"№{message.id} нельзя повторять до сверки с hh.ru."
                ),
                scope_type="recruiter_message",
                scope_id=message.id,
            )
            return
        if message.state is RecruiterMessageState.SENT:
            for code in _MESSAGE_SEND_INCIDENT_CODES:
                self._incidents.resolve(
                    code=code,
                    scope_type="recruiter_message",
                    scope_id=message.id,
                )

    def confirm_and_send(
        self,
        *,
        account_id: int,
        message_id: int,
        content_version: int,
        content_hash: str,
        now: datetime | None = None,
    ) -> RecruiterMessageRecord:
        selected_at = self._now(now)
        confirmed = self.confirm_outgoing_draft(
            account_id=account_id,
            message_id=message_id,
            content_version=content_version,
            content_hash=content_hash,
            confirmed_at=selected_at,
        )
        if confirmed.state in {
            RecruiterMessageState.SENT,
            RecruiterMessageState.FAILED,
            RecruiterMessageState.UNKNOWN_RESULT,
        }:
            return confirmed
        return self.send_confirmed(
            account_id=account_id,
            message_id=message_id,
            content_version=content_version,
            content_hash=content_hash,
            finished_at=selected_at,
        )

    def save_invitation(
        self,
        *,
        application_id: int,
        hh_id: str,
        title: str,
        details: str | None = None,
        interview_at: datetime | None = None,
        booking_url: str | None = None,
        updated_at: datetime | None = None,
    ) -> InvitationRecord:
        selected_title = " ".join(title.split())
        if not selected_title:
            raise ValueError("У приглашения должен быть заголовок")
        selected_url = booking_url.strip() if booking_url else None
        return self._repository.save_invitation(
            application_id=self._positive_id(application_id),
            hh_id=self._stable_id(hh_id, "приглашения"),
            title=selected_title[:255],
            details=details,
            interview_at=interview_at,
            booking_url=selected_url,
            updated_at=self._now(updated_at),
        )

    def mark_invitation_seen(
        self,
        *,
        account_id: int,
        invitation_id: int,
        seen_at: datetime | None = None,
    ) -> InvitationRecord:
        return self._repository.mark_invitation_seen(
            self._positive_id(account_id),
            self._positive_id(invitation_id),
            self._now(seen_at),
        )

    def enqueue_notification(
        self,
        *,
        deduplication_key: str,
        event_type: str,
        channel: NotificationChannel,
        payload: ConfigPayload,
        scheduled_at: datetime | None = None,
        application_id: int | None = None,
        incident_id: int | None = None,
    ) -> NotificationRecord:
        selected_key = deduplication_key.strip()
        if not selected_key or len(selected_key) > 128:
            raise ValueError("Ключ уведомления должен содержать от 1 до 128 символов")
        selected_event = event_type.strip().upper()
        if not selected_event or len(selected_event) > 64:
            raise ValueError("Некорректный вид события уведомления")
        return self._repository.enqueue_notification(
            deduplication_key=selected_key,
            event_type=selected_event,
            channel=channel,
            payload=dict(payload),
            scheduled_at=self._now(scheduled_at),
            application_id=(
                self._positive_id(application_id) if application_id is not None else None
            ),
            incident_id=self._positive_id(incident_id) if incident_id is not None else None,
        )

    @staticmethod
    def content_hash(body: str) -> str:
        return sha256(body.encode("utf-8")).hexdigest()

    def _record_message_action(
        self,
        *,
        account_id: int,
        message_id: int,
        kind: RecruiterActionKind,
        state: RecruiterActionState,
        source: RecruiterActionSource,
        reason_code: str,
        reason: str,
        due_at: datetime | None,
        changed_at: datetime | None,
        preserve_resolved: bool,
    ) -> RecruiterMessageActionRecord:
        selected_code = reason_code.strip().upper()
        if not selected_code or len(selected_code) > 64:
            raise ValueError("Код причины действия должен содержать от 1 до 64 символов")
        selected_reason = " ".join(reason.split())
        if not selected_reason or len(selected_reason) > 1000:
            raise ValueError("Причина действия должна содержать от 1 до 1000 символов")
        return self._repository.record_message_action(
            account_id=self._positive_id(account_id),
            message_id=self._positive_id(message_id),
            kind=kind,
            state=state,
            source=source,
            reason_code=selected_code,
            reason=selected_reason,
            due_at=as_utc(due_at) if due_at is not None else None,
            changed_at=self._now(changed_at),
            preserve_resolved=preserve_resolved,
        )

    @staticmethod
    def _body(value: str) -> str:
        if not value.strip():
            raise ValueError("Текст сообщения не может быть пустым")
        return value

    @staticmethod
    def _hash(value: str) -> str:
        selected = value.strip().lower()
        if len(selected) != 64 or any(
            character not in "0123456789abcdef" for character in selected
        ):
            raise ValueError("Некорректный отпечаток текста")
        return selected

    @staticmethod
    def _stable_id(value: str, label: str) -> str:
        selected = value.strip()
        if not selected or len(selected) > 128:
            raise ValueError(f"Некорректный идентификатор {label}")
        return selected

    @staticmethod
    def _positive_id(value: int) -> int:
        if value < 1:
            raise ValueError("Идентификатор должен быть положительным")
        return value

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        return as_utc(value or datetime.now(UTC))

    @staticmethod
    def _require_exact_version(
        message: RecruiterMessageRecord,
        content_version: int,
        content_hash: str,
    ) -> None:
        if message.content_version != content_version or message.content_hash != content_hash:
            raise StaleMessageDraftError(
                "Черновик изменился. Проверьте актуальную версию перед отправкой"
            )
