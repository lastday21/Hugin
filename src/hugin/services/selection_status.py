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
from hugin.services.semantic_role import ROLE_SELECTION_VERSION
from hugin.services.vacancy_analysis import RULES_VERSION

SelectionRows = Sequence[
    Row[tuple[DirectionVacancyModel, VacancyModel, CareerDirectionModel]]
    | tuple[DirectionVacancyModel, VacancyModel, CareerDirectionModel]
]
SelectionEvidence = dict[tuple[int, int], dict[str, object]]


def semantic_statuses(
    session: Session,
    account_id: int,
    rows: SelectionRows,
    *,
    evidence_by_identity: SelectionEvidence | None = None,
) -> dict[tuple[int, int], str]:
    from hugin.repositories.directions import _direction_record
    from hugin.repositories.vacancies import _to_record
    from hugin.services.decision_evidence import fingerprint
    from hugin.services.semantic_results import StoredSelection, decision_from_stored
    from hugin.services.semantic_snapshot import selection_snapshot, selection_source_lines

    relevant = [
        (tracked, vacancy, direction)
        for tracked, vacancy, direction in rows
        if isinstance(config := direction.scoring_config.get("semantic_selection"), dict)
        and config.get("enabled", True)
        and vacancy.details_fetched_at is not None
        and tracked.rules_version == RULES_VERSION
    ]
    if not relevant:
        return {
            (direction.id, vacancy.id): "PENDING"
            for tracked, vacancy, direction in rows
            if isinstance(config := direction.scoring_config.get("semantic_selection"), dict)
            and config.get("enabled", True)
        }
    evidence_by_identity = (
        evidence_by_identity
        if evidence_by_identity is not None
        else {
            (direction.id, vacancy.id): tracked.rules_details
            for tracked, vacancy, direction in relevant
        }
    )
    result = {(direction.id, vacancy.id): "PENDING" for _, vacancy, direction in relevant}
    keys = set()
    for current_details in evidence_by_identity.values():
        selection = current_details.get("semantic_selection")
        if isinstance(selection, dict) and isinstance(selection.get("key"), str):
            keys.add(selection["key"])
    if not keys:
        return result
    latest = {}
    for row in session.execute(
        select(
            SemanticStageModel.id,
            SemanticStageModel.vacancy_id,
            SemanticStageModel.cache_key,
            SemanticStageModel.stage,
            SemanticStageModel.request["version"].as_string(),
        )
        .where(
            SemanticStageModel.account_id == account_id,
            SemanticStageModel.vacancy_id.in_({vacancy.id for _, vacancy, _ in relevant}),
            SemanticStageModel.cache_key.in_(keys),
        )
        .order_by(SemanticStageModel.id)
    ):
        latest[(row.vacancy_id, row.cache_key)] = row
    current_ids = [
        row.id
        for row in latest.values()
        if row.stage == "selection" and row[4] == ROLE_SELECTION_VERSION
    ]
    if not current_ids:
        return result
    stages = {
        (row.vacancy_id, row.cache_key): row
        for row in session.scalars(
            select(SemanticStageModel)
            .where(
                SemanticStageModel.id.in_(current_ids),
            )
            .order_by(SemanticStageModel.id)
        )
    }
    if not stages:
        return result
    current_vacancies = {vacancy_id for vacancy_id, _ in stages}
    session.scalars(select(VacancyModel).where(VacancyModel.id.in_(current_vacancies))).all()
    assessments: dict[int, list[SemanticStageModel]] = {}
    for assessment in session.scalars(
        select(SemanticStageModel)
        .where(
            SemanticStageModel.account_id == account_id,
            SemanticStageModel.vacancy_id.in_(current_vacancies),
            SemanticStageModel.stage == "assess",
        )
        .order_by(SemanticStageModel.id)
    ):
        assessments.setdefault(assessment.vacancy_id, []).append(assessment)
    templates = {}
    for _tracked, vacancy, direction in relevant:
        identity = (direction.id, vacancy.id)
        if vacancy.id not in current_vacancies:
            continue
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
            details = evidence_by_identity.get(identity, {})
            evidence = details.get("semantic_selection")
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
            if details.get("manual_override") == "ACCEPT":
                result[identity] = "ALLOW"
        except ValueError:
            continue
    return result
