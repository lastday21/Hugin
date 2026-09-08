from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from hugin.database.models import CandidateProfileModel, VerifiedFactModel
from hugin.domain.content import ConfirmationState
from hugin.domain.directions import DirectionRecord
from hugin.domain.vacancies import VacancyData, VacancyRecord
from hugin.repositories.directions import DirectionRepository
from hugin.services.decision_evidence import fingerprint
from hugin.services.semantic_prompts import EXTRACTION_INSTRUCTIONS, MATCHING_INSTRUCTIONS
from hugin.services.semantic_selection import (
    SEMANTIC_SELECTION_VERSION,
    ExtractionDraft,
    Matching,
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
        "screening_answer",
    }
)


class SelectionConfig(StrictRecord):
    enabled: bool = True
    extraction_model: str = Field(default="gpt-5.6-luna", min_length=1)
    matching_model: str = Field(default="gpt-5.6-sol", min_length=1)
    reasoning_effort: Literal["low", "medium", "high"] = "medium"
    timeout_seconds: int = Field(default=300, ge=30, le=300)


def selection_config(direction: DirectionRecord) -> SelectionConfig | None:
    if "semantic_selection" not in direction.scoring_config:
        return None
    config = SelectionConfig.model_validate(direction.scoring_config["semantic_selection"])
    return config if config.enabled else None


def source_lines(vacancy: VacancyData | VacancyRecord) -> list[SourceLine]:
    result: list[SourceLine] = []
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
                if line.strip():
                    result.append(SourceLine(id=len(result), field=field, text=line.strip()))
    return result


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
    lines = source_lines(vacancy)
    request: dict[str, object] = {
        "version": SEMANTIC_SELECTION_VERSION,
        "rules_version": RULES_VERSION,
        "config": config.model_dump(),
        "instructions": fingerprint([EXTRACTION_INSTRUCTIONS, MATCHING_INSTRUCTIONS]),
        "schemas": fingerprint(
            [
                ExtractionDraft.model_json_schema(),
                Matching.model_json_schema(),
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
