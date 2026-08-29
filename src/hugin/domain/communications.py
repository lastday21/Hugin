from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from hugin.domain.content import (
    DeliveryState,
    InvitationState,
    MessageDirection,
    NotificationChannel,
    RecruiterActionKind,
    RecruiterActionSource,
    RecruiterActionState,
    RecruiterMessageState,
)
from hugin.domain.directions import ConfigPayload


class MessageSendOutcome(StrEnum):
    SENT = "SENT"
    FAILED = "FAILED"
    UNKNOWN_RESULT = "UNKNOWN_RESULT"


class MessageSendFailureCode(StrEnum):
    NEGOTIATIONS_OPEN_FAILED = "HH_MESSAGE_NEGOTIATIONS_OPEN_FAILED"
    CHAT_OPEN_FAILED = "HH_MESSAGE_CHAT_OPEN_FAILED"
    CHAT_NOT_FOUND = "HH_MESSAGE_CHAT_NOT_FOUND"
    CHAT_FRAME_MISSING = "HH_MESSAGE_CHAT_FRAME_MISSING"
    EDITOR_UNAVAILABLE = "HH_MESSAGE_EDITOR_UNAVAILABLE"
    SNAPSHOT_UNAVAILABLE = "HH_MESSAGE_SNAPSHOT_UNAVAILABLE"
    SUBMIT_UNAVAILABLE = "HH_MESSAGE_SUBMIT_UNAVAILABLE"
    HTTP_4XX = "HH_MESSAGE_HTTP_4XX"
    UNSPECIFIED = "HH_MESSAGE_FAILED_UNSPECIFIED"


@dataclass(frozen=True, slots=True)
class RecruiterMessageRecord:
    id: int
    application_id: int
    hh_id: str | None
    direction: MessageDirection
    body: str
    state: RecruiterMessageState
    content_hash: str | None
    content_version: int
    read_at: datetime | None
    confirmed_at: datetime | None
    sent_at: datetime | None
    received_at: datetime | None
    auto_send_approved: bool
    reply_template_key: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RecruiterMessageActionRecord:
    message_id: int
    kind: RecruiterActionKind
    state: RecruiterActionState
    source: RecruiterActionSource
    reason_code: str
    reason: str
    due_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class InvitationRecord:
    id: int
    application_id: int
    hh_id: str | None
    title: str
    details: str | None
    interview_at: datetime | None
    booking_url: str | None
    state: InvitationState
    seen_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    id: int
    application_id: int | None
    incident_id: int | None
    deduplication_key: str
    event_type: str
    channel: NotificationChannel
    state: DeliveryState
    payload: ConfigPayload
    scheduled_at: datetime
    sent_at: datetime | None
    error_code: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MessageSendRequest:
    message_id: int
    application_id: int
    body: str
    content_hash: str
    content_version: int


@dataclass(frozen=True, slots=True)
class MessageSendResult:
    outcome: MessageSendOutcome
    external_id: str | None = None
    failure_code: MessageSendFailureCode | None = None


class CommunicationNotFoundError(LookupError):
    pass


class CommunicationStateError(ValueError):
    pass


class StaleMessageDraftError(ValueError):
    pass
