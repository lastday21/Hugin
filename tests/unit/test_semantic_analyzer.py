from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from hugin.services.semantic_analyzer import MemoryStageCache, SemanticAnalyzer
from hugin.services.semantic_selection import ProfileFact, SourceLine

LINES = [SourceLine(id=0, field="description", text="Разрабатывать API на Python")]
FACTS = [ProfileFact(id=7, category="project", content="Разрабатывал API на Python")]
EXTRACTION: dict[str, Any] = {
    "scopes": [{"id": "common", "label": "Общие", "alternative_group": "", "source_lines": []}],
    "entries": [
        {
            "line": 0,
            "subject": "Python API",
            "scope": "common",
            "kind": "duty",
            "activity": "development",
            "level": "working",
            "relation": "all",
            "terms": ["Python", "API"],
        }
    ],
    "excluded_lines": [],
}
MATCHING: dict[str, Any] = {
    "source_issues": [],
    "matches": [
        {
            "entry_id": 0,
            "status": "confirmed",
            "profile_fact_ids": [7],
            "gap": "none",
            "reason": "Python и API подтверждены проектом",
        }
    ],
    "paths": [
        {
            "scope": "common",
            "profession": "applied_python",
            "core_entry_ids": [0],
            "reason": "Основная прикладная Python-разработка",
        }
    ],
}


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


def test_analysis_and_profile_change_reuse_only_appropriate_stage() -> None:
    extractor = Client(EXTRACTION)
    matcher = Client(MATCHING, MATCHING)
    cache = MemoryStageCache()
    analyzer = SemanticAnalyzer(extractor, matcher, cache)
    first = analyzer.analyze(LINES, FACTS)
    assert first.decision.status == "ALLOW"
    assert first.model_calls == 2
    again = analyzer.analyze(LINES, FACTS)
    assert again.model_calls == 0
    assert again.decision == first.decision
    changed = analyzer.analyze(
        LINES,
        [FACTS[0].model_copy(update={"content": "Другая подтверждённая работа с API"})],
    )
    assert changed.model_calls == 1
    assert len(extractor.calls) == 1
    assert len(matcher.calls) == 2
    assert "Другая подтверждённая" in matcher.calls[-1][1]


def test_invalid_line_is_repaired_with_automatic_feedback() -> None:
    invalid = {**EXTRACTION, "entries": [{**EXTRACTION["entries"][0], "line": 99}]}
    extractor = Client(invalid, EXTRACTION)
    matcher = Client(MATCHING)
    result = SemanticAnalyzer(extractor, matcher, MemoryStageCache()).analyze(LINES, FACTS)
    assert result.decision.status == "ALLOW"
    assert result.model_calls == 3
    assert "previous_response" in extractor.calls[1][1]
    assert "неизвестную исходную строку" in extractor.calls[1][1]
    assert result.extraction is not None
    assert result.extraction.entries[0].quote == LINES[0].text


def test_source_issue_gets_one_bounded_extraction_revision() -> None:
    issue = {**MATCHING, "source_issues": [{"line": 0, "issue": "Обязанность искажена"}]}
    wrong_kind = {**EXTRACTION, "entries": [{**EXTRACTION["entries"][0], "kind": "required"}]}
    extractor = Client(wrong_kind, EXTRACTION)
    matcher = Client(issue, MATCHING)
    result = SemanticAnalyzer(extractor, matcher, MemoryStageCache()).analyze(LINES, FACTS)
    assert result.decision.status == "ALLOW"
    assert result.model_calls == 4
    assert "Обязанность искажена" in extractor.calls[1][1]


def test_revision_with_invalid_coverage_is_repaired_before_final_matching() -> None:
    issue = {**MATCHING, "source_issues": [{"line": 0, "issue": "Обязанность искажена"}]}
    wrong_kind = {**EXTRACTION, "entries": [{**EXTRACTION["entries"][0], "kind": "required"}]}
    incomplete = {**EXTRACTION, "entries": []}
    extractor = Client(wrong_kind, incomplete, EXTRACTION)
    matcher = Client(issue, MATCHING)
    result = SemanticAnalyzer(extractor, matcher, MemoryStageCache()).analyze(LINES, FACTS)
    assert result.decision.status == "ALLOW" and result.model_calls == 5
    assert "validation_errors" in extractor.calls[2][1]
    assert result.stages[-2].stage == "extract_revision_repair"


def test_persistent_failure_does_not_fall_back_to_acceptance_or_loop() -> None:
    extractor = Client(RuntimeError("Поставщик недоступен"), RuntimeError("Поставщик недоступен"))
    matcher = Client(MATCHING)
    analyzer = SemanticAnalyzer(extractor, matcher, MemoryStageCache())
    result = analyzer.analyze(LINES, FACTS)
    assert result.decision.status == "REVIEW"
    assert result.model_calls <= 2
    assert len(matcher.calls) == 0
    assert analyzer.analyze(LINES, FACTS).model_calls == 0


def test_tampered_cache_is_not_used_as_model_evidence() -> None:
    cache = MemoryStageCache()
    extractor = Client(EXTRACTION, EXTRACTION)
    matcher = Client(MATCHING)
    analyzer = SemanticAnalyzer(extractor, matcher, cache)
    analyzer.analyze(LINES, FACTS)
    key = next(key for key, value in cache.records.items() if value.stage == "extract")
    cache.records[key] = replace(cache.records[key], response_text="{}")
    result = analyzer.analyze(LINES, FACTS)
    assert result.decision.status == "ALLOW"
    assert len(extractor.calls) == 2


def test_empty_professional_profile_does_not_call_model() -> None:
    extractor, matcher = Client(EXTRACTION), Client(MATCHING)
    result = SemanticAnalyzer(extractor, matcher, MemoryStageCache()).analyze(LINES, [])
    assert result.decision.status == "REVIEW"
    assert result.model_calls == 0


def test_model_change_invalidates_matching_cache() -> None:
    cache = MemoryStageCache()
    extractor = Client(EXTRACTION)
    SemanticAnalyzer(extractor, Client(MATCHING), cache).analyze(LINES, FACTS)
    other = Client(MATCHING, name="other-model:medium")
    result = SemanticAnalyzer(extractor, other, cache).analyze(LINES, FACTS)
    assert result.model_calls == 1
    assert len(extractor.calls) == 1


def test_invalid_matching_repairs_missing_required_condition() -> None:
    matcher = Client({**MATCHING, "matches": []}, MATCHING)
    result = SemanticAnalyzer(Client(EXTRACTION), matcher, MemoryStageCache()).analyze(LINES, FACTS)
    assert result.decision.status == "ALLOW" and result.model_calls == 3
    assert "validation_errors" in matcher.calls[1][1]


def test_call_limit_keeps_last_incomplete_revision_under_review() -> None:
    issue = {**MATCHING, "source_issues": [{"line": 0, "issue": "Обязанность искажена"}]}
    result = SemanticAnalyzer(
        Client(EXTRACTION), Client(issue), MemoryStageCache(), max_calls=2
    ).analyze(LINES, FACTS)
    assert result.decision.status == "REVIEW" and result.model_calls == 2
    assert "предел обращений" in result.decision.reasons[0]


def test_expired_provider_failure_retries_and_recovers() -> None:
    from datetime import UTC, datetime, timedelta

    cache = MemoryStageCache()
    extractor = Client(OSError("Temporary failure"), EXTRACTION)
    analyzer = SemanticAnalyzer(extractor, Client(MATCHING), cache)
    assert analyzer.analyze(LINES, FACTS).decision.status == "REVIEW"
    for key, record in cache.records.items():
        cache.records[key] = replace(record, created_at=datetime.now(UTC) - timedelta(hours=1))
    result = analyzer.analyze(LINES, FACTS)
    assert result.decision.status == "ALLOW" and result.model_calls == 2


@pytest.mark.parametrize("max_calls", [0, 7])
def test_invalid_call_limit_is_rejected(max_calls: int) -> None:
    with pytest.raises(ValueError):
        SemanticAnalyzer(Client(), Client(), MemoryStageCache(), max_calls=max_calls)


def test_schema_failure_cannot_be_used_as_matching_result() -> None:
    result = SemanticAnalyzer(Client(EXTRACTION), Client({}, {}), MemoryStageCache()).analyze(
        LINES, FACTS
    )
    assert result.decision.status == "REVIEW" and result.matching is None
    assert result.model_calls == 3
