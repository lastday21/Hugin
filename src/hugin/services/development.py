from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy.orm import Session

from hugin.domain.development import (
    DevelopmentBlockRecord,
    DevelopmentDirectionRecord,
    DevelopmentItemKind,
    DevelopmentItemStatus,
    DevelopmentPriority,
    DevelopmentSnapshot,
    QualityLevel,
)
from hugin.repositories.development import DevelopmentRepository

_LEVEL_WEIGHT = {
    QualityLevel.LOW: 1,
    QualityLevel.MEDIUM: 2,
    QualityLevel.HIGH: 3,
}


class DevelopmentService:
    def __init__(self, session: Session) -> None:
        self._repository = DevelopmentRepository(session)

    def snapshot(self) -> DevelopmentSnapshot:
        directions = self._repository.list_directions()
        by_block: dict[str, list[DevelopmentDirectionRecord]] = defaultdict(list)
        for direction in directions:
            by_block[direction.block_key].append(direction)

        blocks = []
        for rows in by_block.values():
            first = rows[0]
            weight_sum = sum(_LEVEL_WEIGHT[row.criticality] for row in rows)
            score = round(
                sum(row.current_assessment.score * _LEVEL_WEIGHT[row.criticality] for row in rows)
                / weight_sum,
                1,
            )
            confidence_average = sum(
                _LEVEL_WEIGHT[row.current_assessment.confidence] for row in rows
            ) / len(rows)
            confidence = (
                QualityLevel.HIGH
                if confidence_average >= 2.5
                else QualityLevel.MEDIUM
                if confidence_average >= 1.75
                else QualityLevel.LOW
            )
            bottleneck = min(
                rows,
                key=lambda row: (
                    row.current_assessment.score,
                    -_LEVEL_WEIGHT[row.criticality],
                    row.position,
                ),
            )
            blocks.append(
                DevelopmentBlockRecord(
                    key=first.block_key,
                    name=first.block_name,
                    position=first.block_position,
                    score=score,
                    confidence=confidence,
                    bottleneck_key=bottleneck.key,
                    bottleneck_name=bottleneck.name,
                    directions=tuple(rows),
                )
            )

        return DevelopmentSnapshot(
            blocks=tuple(blocks),
            items=tuple(self._repository.list_items()),
            assessments=tuple(self._repository.list_assessments()),
        )

    def assess_direction(
        self,
        direction_key: str,
        *,
        score: float,
        confidence: QualityLevel,
        evidence: str,
        next_step: str,
        author: str,
    ) -> DevelopmentSnapshot:
        if not 0 <= score <= 5:
            raise ValueError("Оценка должна быть от 0 до 5")
        self._repository.create_assessment(
            direction_key,
            score=score,
            confidence=confidence,
            evidence=_required(evidence, "Подтверждение"),
            next_step=_required(next_step, "Следующий шаг"),
            author=_author(author),
        )
        return self.snapshot()

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
    ) -> DevelopmentSnapshot:
        _validate_finished_result(status, actual_result)
        self._repository.create_item(
            kind=kind,
            title=_required(title, "Название"),
            direction_key=direction_key,
            status=status,
            priority=priority,
            expected_metric=_required(expected_metric, "Ожидаемый показатель"),
            evidence=evidence.strip(),
            verification_method=verification_method.strip(),
            next_step=next_step.strip(),
            actual_result=actual_result.strip(),
            reference_codes=_reference_codes(reference_codes),
            author=_author(author),
        )
        return self.snapshot()

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
    ) -> DevelopmentSnapshot:
        _validate_finished_result(status, actual_result)
        self._repository.update_item(
            item_id,
            kind=kind,
            title=_required(title, "Название"),
            direction_key=direction_key,
            status=status,
            priority=priority,
            expected_metric=_required(expected_metric, "Ожидаемый показатель"),
            evidence=evidence.strip(),
            verification_method=verification_method.strip(),
            next_step=next_step.strip(),
            actual_result=actual_result.strip(),
            reference_codes=_reference_codes(reference_codes),
            author=_author(author),
        )
        return self.snapshot()


def _required(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"Поле «{label}» не заполнено")
    return normalized


def _author(value: str) -> str:
    return value.strip() or "Пользователь"


def _reference_codes(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _validate_finished_result(status: DevelopmentItemStatus, actual_result: str) -> None:
    if status is DevelopmentItemStatus.DONE and not actual_result.strip():
        raise ValueError("Для завершённой работы укажите фактический результат")
