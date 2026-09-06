from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import (
    DevelopmentAssessmentModel,
    DevelopmentDirectionModel,
    DevelopmentItemModel,
)
from hugin.domain.development import (
    DevelopmentAssessmentRecord,
    DevelopmentDirectionRecord,
    DevelopmentItemKind,
    DevelopmentItemRecord,
    DevelopmentItemStatus,
    DevelopmentPriority,
    QualityLevel,
)
from hugin.domain.time import as_utc


def _assessment_record(model: DevelopmentAssessmentModel) -> DevelopmentAssessmentRecord:
    return DevelopmentAssessmentRecord(
        id=model.id,
        direction_key=model.direction_key,
        score=model.score,
        confidence=model.confidence,
        evidence=model.evidence,
        next_step=model.next_step,
        author=model.author,
        created_at=as_utc(model.created_at),
    )


def _item_record(model: DevelopmentItemModel) -> DevelopmentItemRecord:
    return DevelopmentItemRecord(
        id=model.id,
        external_key=model.external_key,
        kind=model.kind,
        title=model.title,
        direction_key=model.direction_key,
        status=model.status,
        priority=model.priority,
        expected_metric=model.expected_metric,
        evidence=model.evidence,
        verification_method=model.verification_method,
        next_step=model.next_step,
        actual_result=model.actual_result,
        reference_codes=tuple(model.reference_codes),
        author=model.author,
        created_at=as_utc(model.created_at),
        updated_at=as_utc(model.updated_at),
    )


class DevelopmentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_directions(self) -> list[DevelopmentDirectionRecord]:
        models = list(
            self._session.scalars(
                select(DevelopmentDirectionModel).order_by(
                    DevelopmentDirectionModel.block_position,
                    DevelopmentDirectionModel.position,
                )
            )
        )
        assessments = list(
            self._session.scalars(
                select(DevelopmentAssessmentModel).order_by(
                    DevelopmentAssessmentModel.created_at.desc(),
                    DevelopmentAssessmentModel.id.desc(),
                )
            )
        )
        latest: dict[str, DevelopmentAssessmentModel] = {}
        counts = Counter[str]()
        for assessment in assessments:
            counts[assessment.direction_key] += 1
            latest.setdefault(assessment.direction_key, assessment)

        records: list[DevelopmentDirectionRecord] = []
        for model in models:
            current_assessment = latest.get(model.key)
            if current_assessment is None:
                raise RuntimeError(f"У направления {model.key} нет исходной оценки")
            records.append(
                DevelopmentDirectionRecord(
                    key=model.key,
                    block_key=model.block_key,
                    block_name=model.block_name,
                    block_position=model.block_position,
                    name=model.name,
                    position=model.position,
                    rule=model.rule,
                    metric=model.metric,
                    criticality=model.criticality,
                    current_assessment=_assessment_record(current_assessment),
                    assessment_count=counts[model.key],
                )
            )
        return records

    def list_assessments(self, *, limit: int = 250) -> list[DevelopmentAssessmentRecord]:
        models = self._session.scalars(
            select(DevelopmentAssessmentModel)
            .order_by(
                DevelopmentAssessmentModel.created_at.desc(),
                DevelopmentAssessmentModel.id.desc(),
            )
            .limit(limit)
        )
        return [_assessment_record(model) for model in models]

    def create_assessment(
        self,
        direction_key: str,
        *,
        score: float,
        confidence: QualityLevel,
        evidence: str,
        next_step: str,
        author: str,
    ) -> DevelopmentAssessmentRecord:
        self._direction(direction_key)
        model = DevelopmentAssessmentModel(
            direction_key=direction_key,
            score=score,
            confidence=confidence,
            evidence=evidence,
            next_step=next_step,
            author=author,
        )
        self._session.add(model)
        self._session.flush()
        return _assessment_record(model)

    def list_items(self) -> list[DevelopmentItemRecord]:
        models = self._session.scalars(
            select(DevelopmentItemModel).order_by(
                DevelopmentItemModel.updated_at.desc(),
                DevelopmentItemModel.id.desc(),
            )
        )
        return [_item_record(model) for model in models]

    def create_item(
        self,
        *,
        kind: DevelopmentItemKind,
        title: str,
        direction_key: str,
        status: DevelopmentItemStatus,
        priority: DevelopmentPriority,
        expected_metric: str,
        evidence: str,
        verification_method: str,
        next_step: str,
        actual_result: str,
        reference_codes: Sequence[str],
        author: str,
    ) -> DevelopmentItemRecord:
        self._direction(direction_key)
        model = DevelopmentItemModel(
            kind=kind,
            title=title,
            direction_key=direction_key,
            status=status,
            priority=priority,
            expected_metric=expected_metric,
            evidence=evidence,
            verification_method=verification_method,
            next_step=next_step,
            actual_result=actual_result,
            reference_codes=list(reference_codes),
            author=author,
        )
        self._session.add(model)
        self._session.flush()
        return _item_record(model)

    def update_item(
        self,
        item_id: int,
        *,
        kind: DevelopmentItemKind,
        title: str,
        direction_key: str,
        status: DevelopmentItemStatus,
        priority: DevelopmentPriority,
        expected_metric: str,
        evidence: str,
        verification_method: str,
        next_step: str,
        actual_result: str,
        reference_codes: Sequence[str],
        author: str,
    ) -> DevelopmentItemRecord:
        self._direction(direction_key)
        model = self._session.get(DevelopmentItemModel, item_id)
        if model is None:
            raise LookupError("Рабочая запись не найдена")
        model.kind = kind
        model.title = title
        model.direction_key = direction_key
        model.status = status
        model.priority = priority
        model.expected_metric = expected_metric
        model.evidence = evidence
        model.verification_method = verification_method
        model.next_step = next_step
        model.actual_result = actual_result
        model.reference_codes = list(reference_codes)
        model.author = author
        self._session.flush()
        return _item_record(model)

    def _direction(self, direction_key: str) -> DevelopmentDirectionModel:
        model = self._session.get(DevelopmentDirectionModel, direction_key)
        if model is None:
            raise LookupError("Направление развития не найдено")
        return model
