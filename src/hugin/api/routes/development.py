from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from hugin.api.dependencies import require_session_key, write_session
from hugin.domain.development import (
    DevelopmentItemKind,
    DevelopmentItemStatus,
    DevelopmentPriority,
    QualityLevel,
)
from hugin.services.development import DevelopmentService


class AssessmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    direction_key: str
    score: float
    confidence: QualityLevel
    evidence: str
    next_step: str
    author: str
    created_at: datetime


class DirectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    block_key: str
    block_name: str
    block_position: int
    name: str
    position: int
    rule: str
    metric: str
    criticality: QualityLevel
    current_assessment: AssessmentResponse
    assessment_count: int


class BlockResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    name: str
    position: int
    score: float
    confidence: QualityLevel
    bottleneck_key: str
    bottleneck_name: str
    directions: tuple[DirectionResponse, ...]


class ItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    external_key: str | None
    kind: DevelopmentItemKind
    title: str
    direction_key: str
    status: DevelopmentItemStatus
    priority: DevelopmentPriority
    expected_metric: str
    evidence: str
    verification_method: str
    next_step: str
    actual_result: str
    reference_codes: tuple[str, ...]
    author: str
    created_at: datetime
    updated_at: datetime


class DevelopmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    blocks: tuple[BlockResponse, ...]
    items: tuple[ItemResponse, ...]
    assessments: tuple[AssessmentResponse, ...]


class AssessmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0, le=5)
    confidence: QualityLevel
    evidence: str = Field(min_length=1, max_length=10_000)
    next_step: str = Field(min_length=1, max_length=10_000)
    author: str = Field(default="Пользователь", max_length=128)


class ItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: DevelopmentItemKind
    title: str = Field(min_length=1, max_length=500)
    direction_key: str = Field(min_length=1, max_length=64)
    status: DevelopmentItemStatus = DevelopmentItemStatus.IDEA
    priority: DevelopmentPriority = DevelopmentPriority.UNASSIGNED
    expected_metric: str = Field(min_length=1, max_length=10_000)
    evidence: str = Field(default="", max_length=20_000)
    verification_method: str = Field(default="", max_length=20_000)
    next_step: str = Field(default="", max_length=20_000)
    actual_result: str = Field(default="", max_length=20_000)
    reference_codes: tuple[str, ...] = Field(default=(), max_length=30)
    author: str = Field(default="Пользователь", max_length=128)


class ItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: DevelopmentItemKind
    title: str = Field(min_length=1, max_length=500)
    direction_key: str = Field(min_length=1, max_length=64)
    status: DevelopmentItemStatus
    priority: DevelopmentPriority
    expected_metric: str = Field(min_length=1, max_length=10_000)
    evidence: str = Field(default="", max_length=20_000)
    verification_method: str = Field(default="", max_length=20_000)
    next_step: str = Field(default="", max_length=20_000)
    actual_result: str = Field(default="", max_length=20_000)
    reference_codes: tuple[str, ...] = Field(default=(), max_length=30)
    author: str = Field(default="Пользователь", max_length=128)


router = APIRouter(prefix="/api/development", tags=["development"])
WriteSession = Annotated[Session, Depends(write_session)]
SessionGuard = Annotated[None, Depends(require_session_key)]


@router.get("", response_model=DevelopmentResponse)
def development(session: WriteSession) -> DevelopmentResponse:
    return DevelopmentResponse.model_validate(DevelopmentService(session).snapshot())


@router.post("/directions/{direction_key}/assessments", response_model=DevelopmentResponse)
def assess_direction(
    direction_key: str,
    values: AssessmentCreate,
    session: WriteSession,
    _guard: SessionGuard,
) -> DevelopmentResponse:
    try:
        snapshot = DevelopmentService(session).assess_direction(
            direction_key,
            score=values.score,
            confidence=values.confidence,
            evidence=values.evidence,
            next_step=values.next_step,
            author=values.author,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return DevelopmentResponse.model_validate(snapshot)


@router.post("/items", response_model=DevelopmentResponse)
def create_item(
    values: ItemCreate,
    session: WriteSession,
    _guard: SessionGuard,
) -> DevelopmentResponse:
    try:
        snapshot = DevelopmentService(session).create_item(**values.model_dump())
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return DevelopmentResponse.model_validate(snapshot)


@router.put("/items/{item_id}", response_model=DevelopmentResponse)
def update_item(
    item_id: int,
    values: ItemUpdate,
    session: WriteSession,
    _guard: SessionGuard,
) -> DevelopmentResponse:
    try:
        snapshot = DevelopmentService(session).update_item(item_id, **values.model_dump())
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return DevelopmentResponse.model_validate(snapshot)
