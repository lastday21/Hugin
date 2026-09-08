from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import VacancyChangeModel

_DECISION_TIME: ContextVar[datetime | None] = ContextVar("decision_time", default=None)


def decision_now() -> datetime:
    return _DECISION_TIME.get() or datetime.now(UTC)


@contextmanager
def decision_time(value: datetime) -> Iterator[None]:
    if value.tzinfo is None:
        raise ValueError("Decision time must include a timezone")
    token = _DECISION_TIME.set(value)
    try:
        yield
    finally:
        _DECISION_TIME.reset(token)


def _json_default(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    raise TypeError(f"Unsupported evidence value: {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def source_fingerprint() -> str:
    root = Path(__file__).resolve().parents[1]
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.py"))
    }
    return fingerprint(files)


def save_ranking_evidence(
    session: Session,
    *,
    vacancy_id: int,
    account_id: int,
    direction_id: int,
    scope: str,
    vacancy: object,
    context: object,
    evaluation: object,
    rules_version: str,
    observed_at: datetime,
    duration_ms: float,
    applied: object,
    semantic: object = None,
) -> int:
    evidence = {
        "schema_version": 1,
        "kind": "vacancy_ranking",
        "source_sha256": source_fingerprint(),
        "rules_version": rules_version,
        "observed_at": observed_at.isoformat(),
        "account_id": account_id,
        "direction_id": direction_id,
        "inputs": {
            "scope": scope,
            "vacancy": vacancy,
            "context": context,
            **({"semantic": semantic} if semantic is not None else {}),
        },
        "output": evaluation,
        "applied": applied,
        "duration_ms": round(duration_ms, 3),
        "provenance": "program_decision",
    }
    payload = json.loads(canonical_json(evidence))
    previous = session.scalar(
        select(VacancyChangeModel)
        .where(
            VacancyChangeModel.vacancy_id == vacancy_id,
            VacancyChangeModel.event_type == "RULES_EVALUATED",
            VacancyChangeModel.changes["direction_id"].as_integer() == direction_id,
        )
        .order_by(VacancyChangeModel.id.desc())
        .limit(1)
    )
    stable_keys = ("source_sha256", "rules_version", "inputs", "output", "applied", "account_id")
    if previous is not None and all(
        previous.changes.get(key) == payload[key] for key in stable_keys
    ):
        return previous.id
    payload["sha256"] = fingerprint(payload)
    row = VacancyChangeModel(vacancy_id=vacancy_id, event_type="RULES_EVALUATED", changes=payload)
    session.add(row)
    session.flush()
    return row.id


def replay_ranking(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Reevaluate saved inputs without a database, browser, or model call."""
    from hugin.domain.directions import DirectionScope, SearchRegion, WorkFormat
    from hugin.domain.vacancies import VacancyAvailability, VacancyData
    from hugin.services.vacancy_analysis import RULES_VERSION, AdjacentItRules, PythonBackendRules

    if evidence.get("schema_version") != 1 or evidence.get("kind") != "vacancy_ranking":
        raise ValueError("Unsupported decision evidence")
    unsigned = {key: value for key, value in evidence.items() if key != "sha256"}
    if fingerprint(unsigned) != evidence.get("sha256"):
        raise ValueError("Decision evidence checksum mismatch")
    inputs = evidence["inputs"]
    values = dict(inputs["vacancy"])
    for key in ("published_at", "details_fetched_at"):
        if values.get(key):
            values[key] = datetime.fromisoformat(values[key])
    for key in ("salary_from", "salary_to"):
        if values.get(key) is not None:
            values[key] = Decimal(values[key])
    values["key_skills"] = tuple(values["key_skills"])
    values["availability"] = VacancyAvailability(values["availability"])
    from hugin.services.vacancy_analysis import RuleContext

    context_values = dict(inputs["context"])
    context_values["skills"] = tuple(context_values["skills"])
    context_values["candidate_locations"] = tuple(context_values["candidate_locations"])
    context_values["work_formats"] = tuple(
        WorkFormat(item) for item in context_values["work_formats"]
    )
    context_values["regions"] = tuple(SearchRegion(**item) for item in context_values["regions"])
    scope = DirectionScope(inputs["scope"])
    rules = PythonBackendRules() if scope is DirectionScope.PYTHON_BACKEND else AdjacentItRules()
    with decision_time(datetime.fromisoformat(evidence["observed_at"])):
        if "semantic" in inputs:
            from hugin.services.semantic_ranking import replay_semantic_evaluation

            evaluated = replay_semantic_evaluation(
                VacancyData(**values), RuleContext(**context_values), scope, inputs["semantic"]
            )
        else:
            evaluated = rules.evaluate(VacancyData(**values), RuleContext(**context_values))
        actual = json.loads(canonical_json(evaluated))
    expected = evidence["output"]
    return {
        "matches": actual == expected,
        "saved_rules_version": evidence["rules_version"],
        "current_rules_version": RULES_VERSION,
        "same_source": evidence["source_sha256"] == source_fingerprint(),
        "expected": expected,
        "actual": actual,
        "external_actions": 0,
    }
