from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from hugin.adapters.resume_documents import ResumeDocumentError, ResumeDocumentReader
from hugin.api.dependencies import read_session, require_session_key, write_session
from hugin.core.settings import Settings
from hugin.services.resume_profile import (
    ProfileFactService,
    ProfileQuestionService,
    ResumeImportService,
    ResumeProfileExtractor,
)
from hugin.services.ui_profile import UiProfileService


class ResumeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    hh_id: str
    title: str
    source_type: str | None
    source_original_name: str | None
    source_size_bytes: int | None
    source_page_count: int | None
    imported_at: datetime | None


class ProfileFactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    content: str
    state: str
    allow_in_letters: bool
    allow_in_forms: bool
    allow_in_messages: bool


class ProfileQuestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    question: str
    answer: str | None
    state: str


class AnswerTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    question: str
    answer: str


class ProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_label: str
    display_name: str
    active_resume: ResumeResponse | None
    facts: tuple[ProfileFactResponse, ...]
    questions: tuple[ProfileQuestionResponse, ...]
    answers: tuple[AnswerTemplateResponse, ...]


class ProfileAnswerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=4000)


class ProfileFactConfirmUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_in_letters: bool
    allow_in_forms: bool
    allow_in_messages: bool


class ResumePreviewFactResponse(BaseModel):
    category: str
    content: str


class ResumePreviewQuestionResponse(BaseModel):
    key: str
    question: str


class ResumePreviewResponse(BaseModel):
    token: str
    original_name: str
    source_type: str
    title: str
    page_count: int | None
    facts: tuple[ResumePreviewFactResponse, ...]
    questions: tuple[ResumePreviewQuestionResponse, ...]


class ResumeImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=200)


@dataclass(slots=True)
class _ResumePreview:
    account_id: int
    path: Path
    original_name: str
    created_at: datetime


router = APIRouter(prefix="/api/profile", tags=["profile"])
ReadSession = Annotated[Session, Depends(read_session)]
WriteSession = Annotated[Session, Depends(write_session)]
SessionGuard = Annotated[None, Depends(require_session_key)]


def _profile(session: Session, account_id: int) -> ProfileResponse:
    return ProfileResponse.model_validate(UiProfileService(session).get(account_id))


def _preview_store(request: Request) -> dict[str, _ResumePreview]:
    value = getattr(request.app.state, "resume_previews", None)
    if value is None:
        value = {}
        request.app.state.resume_previews = value
    if not isinstance(value, dict):
        raise RuntimeError("Хранилище предварительного просмотра недоступно")
    cutoff = datetime.now(UTC) - timedelta(minutes=30)
    for token, preview in tuple(value.items()):
        if not isinstance(preview, _ResumePreview) or preview.created_at < cutoff:
            if isinstance(preview, _ResumePreview):
                preview.path.unlink(missing_ok=True)
            value.pop(token, None)
    return value


@router.get("", response_model=ProfileResponse)
def profile(
    session: ReadSession,
    account_id: int = Query(default=1, ge=1),
) -> ProfileResponse:
    try:
        return _profile(session, account_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/resume/preview", response_model=ResumePreviewResponse)
async def preview_resume(
    request: Request,
    _guard: SessionGuard,
    file: Annotated[UploadFile, File()],
    account_id: int = Query(default=1, ge=1),
) -> ResumePreviewResponse:
    filename = file.filename or ""
    suffix = Path(filename).suffix.casefold()
    if suffix not in {".pdf", ".docx"}:
        raise HTTPException(status_code=422, detail="Поддерживаются только файлы PDF и DOCX")

    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise RuntimeError("Настройки приложения недоступны")
    upload_dir = settings.data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            dir=upload_dir,
            prefix="resume-",
            suffix=suffix,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            total = 0
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > 20 * 1024 * 1024:
                    raise HTTPException(
                        status_code=413,
                        detail="Файл резюме превышает 20 МБ",
                    )
                temporary.write(chunk)
        document = ResumeDocumentReader().read(temporary_path)
        extracted = ResumeProfileExtractor().extract(document)
        token = secrets.token_urlsafe(32)
        _preview_store(request)[token] = _ResumePreview(
            account_id=account_id,
            path=temporary_path,
            original_name=filename,
            created_at=datetime.now(UTC),
        )
        temporary_path = None
        return ResumePreviewResponse(
            token=token,
            original_name=filename,
            source_type=document.source_type.value,
            title=extracted.title,
            page_count=document.page_count,
            facts=tuple(
                ResumePreviewFactResponse(category=fact.category, content=fact.content)
                for fact in extracted.facts
            ),
            questions=tuple(
                ResumePreviewQuestionResponse(key=question.key, question=question.question)
                for question in extracted.missing_questions
            ),
        )
    except HTTPException:
        raise
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ResumeDocumentError, RuntimeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        await file.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@router.post("/resume/import", response_model=ProfileResponse)
def import_resume(
    values: ResumeImportRequest,
    request: Request,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> ProfileResponse:
    preview = _preview_store(request).get(values.token)
    if preview is None or preview.account_id != account_id:
        raise HTTPException(
            status_code=404,
            detail="Предварительная проверка файла истекла; выберите файл снова",
        )
    try:
        ResumeImportService(session, request.app.state.settings.data_dir).import_file(
            account_id,
            preview.path,
            original_name=preview.original_name,
        )
        _preview_store(request).pop(values.token, None)
        preview.path.unlink(missing_ok=True)
        return _profile(session, account_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ResumeDocumentError, RuntimeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/facts/{fact_id}/confirm", response_model=ProfileResponse)
def confirm_fact(
    fact_id: int,
    values: ProfileFactConfirmUpdate,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> ProfileResponse:
    try:
        ProfileFactService(session).confirm(
            account_id,
            fact_id,
            allow_in_letters=values.allow_in_letters,
            allow_in_forms=values.allow_in_forms,
            allow_in_messages=values.allow_in_messages,
        )
        return _profile(session, account_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/facts/{fact_id}/reject", response_model=ProfileResponse)
def reject_fact(
    fact_id: int,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> ProfileResponse:
    try:
        ProfileFactService(session).reject(account_id, fact_id)
        return _profile(session, account_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put("/questions/{key}", response_model=ProfileResponse)
def answer_question(
    key: str,
    values: ProfileAnswerUpdate,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> ProfileResponse:
    try:
        ProfileQuestionService(session).answer(account_id, key, values.answer)
        return _profile(session, account_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/questions/{key}/dismiss", response_model=ProfileResponse)
def dismiss_question(
    key: str,
    session: WriteSession,
    _guard: SessionGuard,
    account_id: int = Query(default=1, ge=1),
) -> ProfileResponse:
    try:
        ProfileQuestionService(session).dismiss(account_id, key)
        return _profile(session, account_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
