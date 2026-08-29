from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.api.dependencies import read_session, require_session_key, write_session
from hugin.database.models import ApplicationModel
from hugin.domain.communications import (
    CommunicationNotFoundError,
    CommunicationStateError,
    StaleMessageDraftError,
)
from hugin.domain.content import MessageDirection, RecruiterMessageState
from hugin.services.ai_prompts import MAX_PROMPT_LENGTH
from hugin.services.communications import CommunicationService, RecordingMessageSender
from hugin.services.ui_communications import (
    WINDOWS_NOTIFICATION_EVENTS,
    UiCommunicationService,
)


class RecruiterMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    direction: str
    body: str
    state: str
    occurred_at: datetime
    read_at: datetime | None
    content_hash: str | None
    content_version: int


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    application_id: int
    vacancy_id: str
    vacancy_title: str
    company: str
    source_url: str
    unread_count: int
    needs_reply: bool
    messages: tuple[RecruiterMessageResponse, ...]


class InvitationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class NotificationSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    windows_enabled: bool
    telegram_enabled: bool
    email_enabled: bool
    routing: dict[str, tuple[str, ...]]


class AiPromptValuesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    resume: str
    cover_letter: str
    recruiter_reply: str


class AiPromptSettingsResponse(AiPromptValuesResponse):
    defaults: AiPromptValuesResponse


class AiModelOptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    value: str
    title: str
    description: str


class AiModelSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    selected: str
    options: tuple[AiModelOptionResponse, ...]
    reasoning_effort: str
    reasoning_options: tuple[AiModelOptionResponse, ...]


class CommunicationsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    conversations: tuple[ConversationResponse, ...]
    invitations: tuple[InvitationResponse, ...]
    unread_messages: int
    unseen_invitations: int
    notification_settings: NotificationSettingsResponse
    ai_model_settings: AiModelSettingsResponse
    ai_prompt_settings: AiPromptSettingsResponse


class ReplyDraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=5000)


class ReplyConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_hash: str = Field(min_length=64, max_length=64)
    content_version: int = Field(ge=1, strict=True)


class NotificationSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    windows_enabled: bool
    telegram_enabled: bool
    email_enabled: bool
    events: tuple[str, ...] = Field(max_length=len(WINDOWS_NOTIFICATION_EVENTS))


class AiPromptSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resume: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)
    cover_letter: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)
    recruiter_reply: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)


class AiModelSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=128)
    reasoning_effort: str = Field(min_length=1, max_length=16)


router = APIRouter(prefix="/api/communications", tags=["communications"])
ReadSession = Annotated[Session, Depends(read_session)]
WriteSession = Annotated[Session, Depends(write_session)]
SessionGuard = Annotated[None, Depends(require_session_key)]


def _response(session: Session, account_id: int) -> CommunicationsResponse:
    return CommunicationsResponse.model_validate(UiCommunicationService(session).get(account_id))


def _service(session: Session) -> CommunicationService:
    return CommunicationService(session, RecordingMessageSender())


def _require_application(session: Session, account_id: int, application_id: int) -> None:
    exists = session.scalar(
        select(ApplicationModel.id).where(
            ApplicationModel.id == application_id,
            ApplicationModel.account_id == account_id,
        )
    )
    if exists is None:
        raise CommunicationNotFoundError("Отклик не найден")


def _communication_error(error: Exception) -> HTTPException:
    if isinstance(error, (CommunicationNotFoundError, LookupError)):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, (CommunicationStateError, StaleMessageDraftError)):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


@router.get("", response_model=CommunicationsResponse)
def communications(
    session: WriteSession,
    account_id: int = Query(default=1, ge=1),
) -> CommunicationsResponse:
    try:
        return _response(session, account_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/conversations/{application_id}/read",
    response_model=CommunicationsResponse,
)
def mark_conversation_read(
    application_id: int,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> CommunicationsResponse:
    service = _service(session)
    try:
        _require_application(session, account_id, application_id)
        for message in service.messages(account_id):
            if (
                message.application_id == application_id
                and message.direction is MessageDirection.INCOMING
                and message.read_at is None
            ):
                service.mark_incoming_read(
                    account_id=account_id,
                    message_id=message.id,
                )
        return _response(session, account_id)
    except (LookupError, ValueError) as error:
        raise _communication_error(error) from error


@router.post(
    "/invitations/{invitation_id}/seen",
    response_model=CommunicationsResponse,
)
def mark_invitation_seen(
    invitation_id: int,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> CommunicationsResponse:
    try:
        _service(session).mark_invitation_seen(
            account_id=account_id,
            invitation_id=invitation_id,
        )
        return _response(session, account_id)
    except (LookupError, ValueError) as error:
        raise _communication_error(error) from error


@router.put(
    "/conversations/{application_id}/draft",
    response_model=CommunicationsResponse,
)
def save_reply_draft(
    application_id: int,
    values: ReplyDraftUpdate,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> CommunicationsResponse:
    service = _service(session)
    try:
        _require_application(session, account_id, application_id)
        outgoing = next(
            (
                message
                for message in service.messages(account_id)
                if message.application_id == application_id
                and message.direction is MessageDirection.OUTGOING
            ),
            None,
        )
        if outgoing is None or outgoing.state is RecruiterMessageState.SENT:
            service.create_outgoing_draft(
                application_id=application_id,
                body=values.body,
            )
        elif outgoing.state is RecruiterMessageState.UNKNOWN_RESULT:
            raise CommunicationStateError("Сначала уточните результат предыдущей отправки")
        else:
            service.edit_outgoing_draft(
                account_id=account_id,
                message_id=outgoing.id,
                body=values.body,
            )
        return _response(session, account_id)
    except (LookupError, ValueError) as error:
        raise _communication_error(error) from error


@router.post(
    "/messages/{message_id}/confirm",
    response_model=CommunicationsResponse,
)
def confirm_reply(
    message_id: int,
    values: ReplyConfirmation,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> CommunicationsResponse:
    try:
        _service(session).confirm_outgoing_draft(
            account_id=account_id,
            message_id=message_id,
            content_version=values.content_version,
            content_hash=values.content_hash,
        )
        return _response(session, account_id)
    except (LookupError, ValueError) as error:
        raise _communication_error(error) from error


@router.put("/notifications", response_model=CommunicationsResponse)
def update_notification_settings(
    values: NotificationSettingsUpdate,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> CommunicationsResponse:
    try:
        updated = UiCommunicationService(session).update_notification_settings(
            account_id=account_id,
            windows_enabled=values.windows_enabled,
            telegram_enabled=values.telegram_enabled,
            email_enabled=values.email_enabled,
            events=values.events,
        )
        return CommunicationsResponse.model_validate(updated)
    except (LookupError, ValueError) as error:
        raise _communication_error(error) from error


@router.put("/ai-prompts", response_model=CommunicationsResponse)
def update_ai_prompt_settings(
    values: AiPromptSettingsUpdate,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> CommunicationsResponse:
    try:
        updated = UiCommunicationService(session).update_ai_prompt_settings(
            account_id=account_id,
            resume=values.resume,
            cover_letter=values.cover_letter,
            recruiter_reply=values.recruiter_reply,
        )
        return CommunicationsResponse.model_validate(updated)
    except (LookupError, ValueError) as error:
        raise _communication_error(error) from error


@router.put("/ai-model", response_model=CommunicationsResponse)
def update_ai_model_settings(
    values: AiModelSettingsUpdate,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> CommunicationsResponse:
    try:
        updated = UiCommunicationService(session).update_ai_model_settings(
            account_id=account_id,
            model=values.model,
            reasoning_effort=values.reasoning_effort,
        )
        return CommunicationsResponse.model_validate(updated)
    except (LookupError, ValueError) as error:
        raise _communication_error(error) from error


@router.post("/ai-prompts/reset", response_model=CommunicationsResponse)
def reset_ai_prompt_settings(
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> CommunicationsResponse:
    try:
        updated = UiCommunicationService(session).reset_ai_prompt_settings(account_id=account_id)
        return CommunicationsResponse.model_validate(updated)
    except (LookupError, ValueError) as error:
        raise _communication_error(error) from error
