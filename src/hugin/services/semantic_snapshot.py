from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from pydantic import Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from hugin.database.models import CandidateProfileModel, SemanticStageModel, VerifiedFactModel
from hugin.domain.content import ConfirmationState
from hugin.domain.directions import DirectionRecord
from hugin.domain.vacancies import VacancyData, VacancyRecord
from hugin.repositories.directions import DirectionRepository
from hugin.services.decision_evidence import fingerprint
from hugin.services.semantic_role import ROLE_INSTRUCTIONS, ROLE_SELECTION_VERSION, RoleAssessment
from hugin.services.semantic_selection import (
    ProfileFact,
    SourceLine,
    StrictRecord,
)

PROFESSIONAL_FACT_CATEGORIES = frozenset(
    {
        "desired_position",
        "skills",
        "technology",
        "work_experience",
        "project",
        "about",
        "courses",
        "education",
        "languages",
        "language",
        "english_level",
        "portfolio",
        "experience",
    }
)


class SelectionConfig(StrictRecord):
    enabled: bool = True
    extraction_model: str = Field(default="gpt-5.6-luna", min_length=1)
    matching_model: str = Field(default="gpt-5.6-sol", min_length=1)
    assessment_model: str | None = Field(default=None, min_length=1)
    reasoning_effort: Literal["low", "medium", "high"] = "medium"
    timeout_seconds: int = Field(default=300, ge=30, le=300)

    @property
    def model(self) -> str:
        return self.assessment_model or "gpt-5.6-terra"


def selection_config(direction: DirectionRecord) -> SelectionConfig | None:
    if "semantic_selection" not in direction.scoring_config:
        return None
    config = SelectionConfig.model_validate(direction.scoring_config["semantic_selection"])
    return config if config.enabled else None


def source_lines(
    vacancy: VacancyData | VacancyRecord,
    *,
    include_repeated_fields: bool = False,
) -> list[SourceLine]:
    result: list[SourceLine] = []
    description = {line.strip() for line in (vacancy.description or "").splitlines()}
    for field in (
        "title",
        "description",
        "responsibilities",
        "required_qualifications",
        "preferred_qualifications",
        "key_skills",
    ):
        value = getattr(vacancy, field)
        values = value if isinstance(value, tuple) else (value or "",)
        for text in values:
            for line in text.splitlines():
                content = line.strip()
                if not content:
                    continue
                if (
                    not include_repeated_fields
                    and field not in {"title", "description"}
                    and content in description
                ):
                    continue
                result.append(SourceLine(id=len(result), field=field, text=content))
    return result


def selection_source_lines(
    session: Session,
    account_id: int,
    vacancy: VacancyRecord,
    *,
    previous_stages: Iterable[SemanticStageModel] | None = None,
) -> list[SourceLine]:
    compact = source_lines(vacancy)
    original = source_lines(vacancy, include_repeated_fields=True)
    if compact == original:
        return compact
    original_payload = [line.model_dump() for line in original]
    if previous_stages is None:
        previous_stages = session.scalars(
            select(SemanticStageModel)
            .where(
                SemanticStageModel.account_id == account_id,
                SemanticStageModel.vacancy_id == vacancy.id,
                SemanticStageModel.stage == "assess",
            )
            .order_by(SemanticStageModel.id.desc())
        )
    for stage in previous_stages:
        payload = stage.request.get("payload")
        if (
            stage.account_id == account_id
            and stage.vacancy_id == vacancy.id
            and stage.stage == "assess"
            and isinstance(payload, dict)
            and payload.get("vacancy_lines") == original_payload
            and fingerprint(stage.request) == stage.cache_key
            and fingerprint(stage.response_text) == stage.response_sha256
        ):
            # Сохраняем нумерацию начатого разбора. Актуальность модели и ответа
            # проверяет обычный механизм повторного использования ступеней.
            return original
    return compact


@dataclass(frozen=True, slots=True)
class SelectionSnapshot:
    account_id: int
    direction_id: int
    vacancy_id: int
    resume_id: int | None
    config: SelectionConfig
    lines: list[SourceLine]
    facts: list[ProfileFact]
    request: dict[str, object]

    @property
    def key(self) -> str:
        return fingerprint(self.request)


def selection_snapshot(
    session: Session,
    direction: DirectionRecord,
    vacancy: VacancyRecord,
) -> SelectionSnapshot | None:
    from hugin.services.vacancy_analysis import RULES_VERSION

    config = selection_config(direction)
    if config is None:
        return None
    resume = next(
        (
            item
            for item in DirectionRepository(session).list_resumes(direction.id)
            if item.is_active and item.account_id == direction.account_id
        ),
        None,
    )
    profile = session.scalar(
        select(CandidateProfileModel).where(
            CandidateProfileModel.account_id == direction.account_id,
        )
    )
    rows = (
        list(
            session.scalars(
                select(VerifiedFactModel)
                .where(
                    VerifiedFactModel.profile_id == profile.id,
                    VerifiedFactModel.state == ConfirmationState.CONFIRMED,
                    VerifiedFactModel.category.in_(PROFESSIONAL_FACT_CATEGORIES),
                    or_(
                        VerifiedFactModel.source_reference.is_(None),
                        VerifiedFactModel.source_reference.not_like("screening:%"),
                    ),
                    or_(
                        VerifiedFactModel.resume_id == resume.id,
                        VerifiedFactModel.resume_id.is_(None),
                    ),
                    or_(
                        VerifiedFactModel.direction_id == direction.id,
                        VerifiedFactModel.direction_id.is_(None),
                    ),
                )
                .order_by(VerifiedFactModel.id)
            )
        )
        if profile is not None and resume is not None
        else []
    )
    facts = [
        ProfileFact(
            id=row.id,
            category=row.category,
            content=row.content,
            actual_at=row.actual_at.isoformat() if row.actual_at else None,
        )
        for row in rows
        if row.content.strip()
    ]
    lines = selection_source_lines(session, direction.account_id, vacancy)
    request: dict[str, object] = {
        "version": ROLE_SELECTION_VERSION,
        "rules_version": RULES_VERSION,
        "config": {
            "enabled": config.enabled,
            "model": config.model,
            "reasoning_effort": config.reasoning_effort,
            "timeout_seconds": config.timeout_seconds,
        },
        "instructions": fingerprint(ROLE_INSTRUCTIONS),
        "schemas": fingerprint(
            [
                RoleAssessment.model_json_schema(),
                ProfileFact.model_json_schema(),
            ]
        ),
        "source": [line.model_dump() for line in lines],
        "profile": {
            "profile_id": profile.id if profile else None,
            "resume_id": resume.id if resume else None,
            "resume_hh_id": resume.hh_id if resume else None,
            "resume_title": resume.title if resume else None,
            "facts": [
                {
                    "id": row.id,
                    "category": row.category,
                    "content": row.content,
                    "actual_at": row.actual_at.isoformat() if row.actual_at else None,
                    "source_type": row.source_type,
                    "resume_id": row.resume_id,
                    "direction_id": row.direction_id,
                }
                for row in rows
            ],
        },
    }
    return SelectionSnapshot(
        direction.account_id,
        direction.id,
        vacancy.id,
        resume.id if resume else None,
        config,
        lines,
        facts,
        request,
    )
