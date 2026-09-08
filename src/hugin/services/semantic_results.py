from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy.orm import Session

from hugin.domain.directions import DirectionScope
from hugin.services.decision_evidence import canonical_json, fingerprint
from hugin.services.semantic_analyzer import (
    FAILED_STAGE_RETRY_AFTER,
    AnalysisResult,
    StageRecord,
)
from hugin.services.semantic_cache import load_stage
from hugin.services.semantic_selection import (
    SEMANTIC_SELECTION_VERSION,
    Extraction,
    Matching,
    SemanticDecision,
    StrictRecord,
    assess_requirements,
)
from hugin.services.semantic_snapshot import SelectionSnapshot


class StoredSelection(StrictRecord):
    extraction: Extraction | None
    matching: Matching | None
    errors: list[str]
    stage_keys: list[str]
    model_calls: int
    retryable: bool


@dataclass(frozen=True, slots=True)
class SelectionResult:
    decision: SemanticDecision
    target_scope: DirectionScope | None
    evidence: dict[str, object]
    due: bool


def final_stage(snapshot: SelectionSnapshot, result: AnalysisResult) -> StageRecord:
    errors = list(result.decision.reasons) if result.decision.status == "REVIEW" else []
    retryable = (
        result.extraction is None
        or result.matching is None
        or any(stage.errors for stage in result.stages)
    )
    if result.extraction is not None and result.matching is not None:
        recomputed = assess_requirements(
            snapshot.lines, snapshot.facts, result.extraction, result.matching
        )
        retryable = retryable or recomputed != result.decision
    stored = StoredSelection(
        extraction=result.extraction,
        matching=result.matching,
        errors=errors,
        stage_keys=[stage.cache_key for stage in result.stages],
        model_calls=result.model_calls,
        retryable=retryable,
    )
    response = stored.model_dump_json()
    return StageRecord(
        snapshot.key,
        "selection",
        f"program:{SEMANTIC_SELECTION_VERSION}",
        snapshot.request,
        response,
        fingerprint(response),
        tuple(errors),
        datetime.now(UTC),
        sum(stage.duration_seconds for stage in result.stages),
    )


def read_selection(session: Session, snapshot: SelectionSnapshot) -> SelectionResult:
    record = load_stage(session, snapshot.account_id, snapshot.vacancy_id, snapshot.key)
    stored = None
    if (
        record is not None
        and record.stage == "selection"
        and (
            fingerprint(record.request) == snapshot.key
            and fingerprint(record.response_text) == record.response_sha256
        )
    ):
        with suppress(ValidationError):
            stored = StoredSelection.model_validate_json(record.response_text)
    if stored is None or record is None:
        return SelectionResult(
            SemanticDecision(
                "REVIEW", None, ("Ожидает смыслового разбора текущей вакансии и профиля",)
            ),
            None,
            {"key": snapshot.key, "status": "PENDING"},
            True,
        )
    decision = decision_from_stored(snapshot, stored)
    target = target_from_matching(stored.matching, decision)
    evidence: dict[str, object] = {
        "key": snapshot.key,
        "status": decision.status,
        "request": snapshot.request,
        "stored": stored.model_dump(mode="json"),
        "decision": canonical_json(decision),
        "created_at": record.created_at.isoformat(),
    }
    due = stored.retryable and datetime.now(UTC) - record.created_at >= FAILED_STAGE_RETRY_AFTER
    return SelectionResult(decision, target, evidence, due)


def decision_from_stored(snapshot: SelectionSnapshot, stored: StoredSelection) -> SemanticDecision:
    if stored.errors or stored.extraction is None or stored.matching is None:
        return SemanticDecision(
            "REVIEW", None, tuple(stored.errors) or ("Неполный разбор вакансии",)
        )
    return assess_requirements(snapshot.lines, snapshot.facts, stored.extraction, stored.matching)


def target_from_matching(
    matching: Matching | None, decision: SemanticDecision
) -> DirectionScope | None:
    if decision.status != "ALLOW" or matching is None or matching.source_issues:
        return None
    paths = [path for path in matching.paths if path.scope in decision.selected_scopes]
    if not paths or any(path.profession == "unclear" for path in paths):
        return None
    return (
        DirectionScope.PYTHON_BACKEND
        if any(path.profession == "applied_python" for path in paths)
        else DirectionScope.IT_ADJACENT
    )
