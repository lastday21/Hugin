from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from hugin.services.role_analyzer import RoleAnalyzer
from hugin.services.semantic_analyzer import MemoryStageCache
from hugin.services.semantic_role import ROLE_INSTRUCTIONS
from tests.unit.test_semantic_role import ANSWER, FACTS, LINES


class Client:
    def __init__(self, *answers: dict[str, Any] | Exception, name: str = "model:medium") -> None:
        self.answers = list(answers)
        self.request_identity = name
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def complete_json(self, system: str, user: str, schema: dict[str, object]) -> str:
        self.calls.append((system, user, schema))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer, ensure_ascii=False)


def test_full_source_and_profile_are_assessed_in_one_call() -> None:
    client = Client(ANSWER)
    result = RoleAnalyzer(client, MemoryStageCache()).analyze(LINES, FACTS)
    assert result.decision.status == "ALLOW" and result.model_calls == 1
    assert result.assessment is not None
    assert result.extraction is None and result.matching is None
    assert [stage.stage for stage in result.stages] == ["assess"]
    system, user, _schema = client.calls[0]
    payload = json.loads(user)
    assert system == ROLE_INSTRUCTIONS
    assert payload["vacancy_lines"] == [line.model_dump() for line in LINES]
    assert payload["profile"]["facts"] == [fact.model_dump() for fact in FACTS]


def test_same_input_is_free_and_changed_profile_requires_one_new_assessment() -> None:
    client = Client(ANSWER, ANSWER)
    analyzer = RoleAnalyzer(client, MemoryStageCache())
    first = analyzer.analyze(LINES, FACTS)
    assert first.model_calls == 1
    repeated = analyzer.analyze(LINES, FACTS)
    assert repeated.model_calls == 0 and repeated.decision == first.decision
    changed = analyzer.analyze(LINES, [FACTS[0].model_copy(update={"content": "Другой проект"})])
    assert changed.model_calls == 1 and len(client.calls) == 2


@pytest.mark.parametrize("answer", [{}, {**ANSWER, "source_line_ids": [99]}, RuntimeError("Сбой")])
def test_invalid_answer_is_not_repaired_by_extra_calls(answer: dict[str, Any] | Exception) -> None:
    client = Client(answer)
    analyzer = RoleAnalyzer(client, MemoryStageCache(), max_calls=6)
    result = analyzer.analyze(LINES, FACTS)
    assert result.decision.status == "REVIEW" and result.assessment is None
    assert result.model_calls == 1 and not result.budget_exhausted
    assert analyzer.analyze(LINES, FACTS).model_calls == 0
    assert len(client.calls) == 1


@pytest.mark.parametrize("tampered", ["request", "response", "key"])
def test_corrupt_saved_response_cannot_produce_a_free_decision(tampered: str) -> None:
    cache = MemoryStageCache()
    client = Client(ANSWER, ANSWER)
    analyzer = RoleAnalyzer(client, cache)
    analyzer.analyze(LINES, FACTS)
    key, stage = next(iter(cache.records.items()))
    changes: dict[str, dict[str, Any]] = {
        "request": {"request": {}},
        "response": {"response_text": "{}"},
        "key": {"cache_key": "other"},
    }
    cache.records[key] = replace(stage, **changes[tampered])
    assert analyzer.analyze(LINES, FACTS).model_calls == 1


def test_failed_request_is_retried_once_after_the_delay() -> None:
    cache = MemoryStageCache()
    analyzer = RoleAnalyzer(Client(OSError("Недоступно"), ANSWER), cache)
    assert analyzer.analyze(LINES, FACTS).decision.status == "REVIEW"
    for key, stage in cache.records.items():
        cache.records[key] = replace(stage, created_at=datetime.now(UTC) - timedelta(hours=1))
    result = analyzer.analyze(LINES, FACTS)
    assert result.decision.status == "ALLOW" and result.model_calls == 1


@pytest.mark.parametrize("change", ["model", "text", "force"])
def test_changed_inputs_or_explicit_force_invalidate_saved_assessment(change: str) -> None:
    cache = MemoryStageCache()
    RoleAnalyzer(Client(ANSWER), cache).analyze(LINES, FACTS)
    client = Client(ANSWER, name="other" if change == "model" else "model:medium")
    lines = [*LINES, LINES[0].model_copy(update={"id": 5})] if change == "text" else LINES
    assert (
        RoleAnalyzer(client, cache, force=change == "force").analyze(lines, FACTS).model_calls == 1
    )


@pytest.mark.parametrize("empty", ["source", "profile"])
def test_incomplete_input_does_not_spend_a_model_call(empty: str) -> None:
    result = RoleAnalyzer(Client(), MemoryStageCache()).analyze(
        [] if empty == "source" else LINES, [] if empty == "profile" else FACTS
    )
    assert result.decision.status == "REVIEW" and result.model_calls == 0


def test_title_without_job_description_does_not_spend_a_call() -> None:
    result = RoleAnalyzer(Client(ANSWER), MemoryStageCache()).analyze(LINES[:1], FACTS)
    assert result.decision.status == "REVIEW" and result.model_calls == 0


@pytest.mark.parametrize("reject", [False, True])
def test_long_evidence_lists_are_preserved_and_schema_limits_known_references(reject: bool) -> None:
    lines = [LINES[0], *[LINES[1].model_copy(update={"id": i}) for i in range(1, 16)]]
    answer = {**ANSWER, "source_line_ids": list(range(1, 16))}
    if reject:
        answer.update(
            fit="reject",
            blocker={"source_line_ids": list(range(1, 16)), "reason": "Чужая основная работа"},
        )
    client = Client(answer)
    result = RoleAnalyzer(client, MemoryStageCache()).analyze(lines, FACTS)
    assert result.decision.status == ("REJECT" if reject else "ALLOW")
    assert result.model_calls == 1
    assert result.assessment is not None
    assert result.assessment.source_line_ids == list(range(1, 16))
    schema = cast(dict[str, Any], client.calls[0][2])
    assert schema["properties"]["source_line_ids"]["items"]["enum"] == list(range(16))
    assert schema["properties"]["profile_fact_ids"]["items"]["enum"] == [7]
    blockers = [
        definition
        for definition in schema["$defs"].values()
        if "fit" not in definition.get("properties", {})
    ]
    assert blockers[0]["properties"]["source_line_ids"]["items"]["enum"] == list(range(1, 16))


def test_each_request_schema_uses_its_own_source_and_profile_numbers() -> None:
    client = Client(ANSWER, {**ANSWER, "source_line_ids": [11], "profile_fact_ids": [17]})
    analyzer = RoleAnalyzer(client, MemoryStageCache())
    assert analyzer.analyze(LINES, FACTS).decision.status == "ALLOW"
    assert (
        analyzer.analyze(
            [line.model_copy(update={"id": line.id + 10}) for line in LINES],
            [FACTS[0].model_copy(update={"id": 17})],
        ).decision.status
        == "ALLOW"
    )
    schemas = [cast(dict[str, Any], call[2]) for call in client.calls]
    assert schemas[0]["properties"]["source_line_ids"]["items"]["enum"] == [0, 1, 2]
    assert schemas[1]["properties"]["source_line_ids"]["items"]["enum"] == [10, 11, 12]
