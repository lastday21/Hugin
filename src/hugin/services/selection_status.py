from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from hugin.database.models import (
    CareerDirectionModel,
    DirectionVacancyModel,
    SemanticStageModel,
    VacancyModel,
)
from hugin.services.vacancy_analysis import RULES_VERSION


def semantic_statuses(
    session: Session,
    account_id: int,
    rows: Sequence[Row[tuple[DirectionVacancyModel, VacancyModel, CareerDirectionModel]]],
) -> dict[tuple[int, int], str]:
    from hugin.repositories.directions import _direction_record
    from hugin.repositories.vacancies import _to_record
    from hugin.services.decision_evidence import fingerprint
    from hugin.services.semantic_results import StoredSelection, decision_from_stored
    from hugin.services.semantic_snapshot import selection_snapshot, selection_source_lines

    relevant = [
        (tracked, vacancy, direction)
        for tracked, vacancy, direction in rows
        if isinstance(direction.scoring_config.get("semantic_selection"), dict)
        and direction.scoring_config["semantic_selection"].get("enabled", True)
        and vacancy.details_fetched_at is not None
        and tracked.rules_version == RULES_VERSION
    ]
    if not relevant:
        return {
            (direction.id, vacancy.id): "PENDING"
            for tracked, vacancy, direction in rows
            if isinstance(direction.scoring_config.get("semantic_selection"), dict)
            and direction.scoring_config["semantic_selection"].get("enabled", True)
        }
    stages = {}
    assessments: dict[int, list[SemanticStageModel]] = {}
    for row in session.scalars(
        select(SemanticStageModel)
        .where(
            SemanticStageModel.account_id == account_id,
            SemanticStageModel.vacancy_id.in_({vacancy.id for _, vacancy, _ in relevant}),
        )
        .order_by(SemanticStageModel.id)
    ):
        stages[(row.vacancy_id, row.cache_key)] = row
        if row.stage == "assess":
            assessments.setdefault(row.vacancy_id, []).append(row)
    templates, result = {}, {}
    for tracked, vacancy, direction in relevant:
        identity = (direction.id, vacancy.id)
        result[identity] = "PENDING"
        try:
            if direction.id not in templates:
                templates[direction.id] = selection_snapshot(
                    session, _direction_record(direction), _to_record(vacancy)
                )
            template = templates[direction.id]
            if template is None:
                continue
            lines = selection_source_lines(
                session,
                account_id,
                _to_record(vacancy),
                previous_stages=assessments.get(vacancy.id, ()),
            )
            snapshot = replace(
                template,
                vacancy_id=vacancy.id,
                lines=lines,
                request={**template.request, "source": [line.model_dump() for line in lines]},
            )
            evidence = tracked.rules_details.get("semantic_selection")
            if not isinstance(evidence, dict) or evidence.get("key") != snapshot.key:
                continue
            stage = stages.get((vacancy.id, snapshot.key))
            if (
                stage is None
                or stage.stage != "selection"
                or fingerprint(stage.request) != snapshot.key
                or fingerprint(stage.response_text) != stage.response_sha256
            ):
                continue
            stored = StoredSelection.model_validate_json(stage.response_text)
            if stored.retryable:
                continue
            result[identity] = decision_from_stored(snapshot, stored).status
            if tracked.rules_details.get("manual_override") == "ACCEPT":
                result[identity] = "ALLOW"
        except ValueError:
            continue
    return result
