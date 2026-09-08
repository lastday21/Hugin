from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from hugin.services.semantic_selection import (
    Extraction,
    Matching,
    ProfileFact,
    SemanticDecision,
    SourceLine,
    assess_requirements,
)


def example() -> tuple[list[SourceLine], list[ProfileFact], dict[str, Any], dict[str, Any]]:
    lines = [
        SourceLine(id=0, field="title", text="Разработчик"),
        SourceLine(id=1, field="description", text="Разрабатывать API на Python"),
        SourceLine(id=2, field="description", text="Разрабатывать приложения на Ruby"),
    ]
    facts = [ProfileFact(id=7, category="project", content="Создал сервис на Python")]
    extraction = {
        "scopes": [
            {"id": "common", "label": "Общие условия", "alternative_group": "", "source_lines": [0]}
        ],
        "entries": [
            {
                "line": line.id,
                "quote": line.text,
                "subject": line.text,
                "scope": "common",
                "kind": "duty",
                "activity": "development",
                "level": "working",
                "relation": "all",
                "terms": ["Python" if line.id == 1 else "Ruby"],
            }
            for line in lines[1:]
        ],
        "excluded_lines": [{"line": 0, "reason": "heading"}],
    }
    matching = {
        "source_issues": [],
        "matches": [
            {
                "entry_id": 0,
                "status": "confirmed",
                "profile_fact_ids": [7],
                "gap": "none",
                "reason": "Python-разработка подтверждена проектом",
            },
            {
                "entry_id": 1,
                "status": "unconfirmed",
                "profile_fact_ids": [],
                "gap": "other_development",
                "reason": "Разработка Ruby не подтверждена",
            },
        ],
        "paths": [
            {
                "scope": "common",
                "profession": "applied_python",
                "core_entry_ids": [0, 1],
                "reason": "Работа содержит две обязательные части разработки",
            }
        ],
    }
    return lines, facts, extraction, matching


def assess(
    data: tuple[list[SourceLine], list[ProfileFact], dict[str, Any], dict[str, Any]],
) -> SemanticDecision:
    lines, facts, extraction, matching = data
    return assess_requirements(
        lines,
        facts,
        Extraction.model_validate(extraction),
        Matching.model_validate(matching),
    )


def test_general_python_fit_cannot_cancel_missing_core_specialization() -> None:
    result = assess(example())
    assert result.status == "REJECT"
    assert result.fit_tier is None
    assert result.blocking_entry_ids == (1,)
    assert "Ruby" in " ".join(result.reasons)


def test_confirmed_career_interest_does_not_lower_priority_or_claim_skill() -> None:
    from hugin.domain.vacancy_priority import FitTier

    data = example()
    data[0][2] = SourceLine(
        id=2, field="description", text="Желание в будущем развиваться в машинном обучении"
    )
    data[2]["entries"][1].update(
        quote=data[0][2].text,
        subject="Направление развития",
        kind="required",
        activity="other",
        terms=["машинное обучение"],
    )
    data[3]["matches"][1].update(
        gap="career_interest", reason="Кандидат подтвердил готовность развиваться в этой области"
    )
    data[3]["paths"][0]["core_entry_ids"] = [0]
    result = assess(data)
    assert result.status == "ALLOW" and result.fit_tier is FitTier.DIRECT
    assert data[3]["matches"][1]["status"] == "unconfirmed"
    data[2]["entries"][1].update(kind="duty", activity="model_training")
    data[3]["matches"][1].update(gap="model_research", reason="Нет опыта обучения моделей")
    data[3]["paths"][0]["core_entry_ids"] = [0, 1]
    assert assess(data).status == "REJECT"


@pytest.mark.parametrize("gap", ["career_interest", "selection_step"])
def test_intention_or_hiring_step_cannot_hide_missing_technical_skill(gap: str) -> None:
    data = example()
    data[3]["matches"][1]["gap"] = gap
    result = assess(data)
    assert result.status == "REVIEW"
    assert "профессиональный навык" in " ".join(result.reasons)


def test_unclear_required_part_cannot_disappear_from_the_decision() -> None:
    data = example()
    data[2]["entries"][1]["kind"] = "unclear"
    data[3]["matches"].pop()
    data[3]["paths"][0]["core_entry_ids"] = [0]
    result = assess(data)
    assert result.status == "ALLOW" and result.fit_tier == 3
    assert "Ruby" in " ".join(result.reasons)


def test_clear_alternative_can_be_selected_when_other_path_is_unclear() -> None:
    data = example()
    extraction, matching = data[2:]
    for index, scope in enumerate(("python", "ruby")):
        extraction["scopes"].append(
            {
                "id": scope,
                "label": scope,
                "alternative_group": "choice",
                "source_lines": [index + 1],
            }
        )
        extraction["entries"][index]["scope"] = scope
    extraction["entries"][1]["kind"] = "unclear"
    matching["matches"].pop()
    matching["paths"] = [
        {
            "scope": "python",
            "profession": "applied_python",
            "core_entry_ids": [0],
            "reason": "Python",
        },
        {
            "scope": "ruby",
            "profession": "unclear",
            "core_entry_ids": [],
            "reason": "Нужно уточнение",
        },
    ]
    result = assess(data)
    assert result.status == "ALLOW" and result.selected_scopes == ("python",)


@pytest.mark.parametrize("gap", ["tool", "experience"])
def test_tools_and_experience_reduce_priority_without_rejection(gap: str) -> None:
    data = example()
    data[3]["matches"][1]["gap"] = gap
    result = assess(data)
    assert result.status == "ALLOW"
    assert result.fit_tier == 3


def test_confirmed_main_work_gets_direct_priority() -> None:
    data = example()
    data[3]["matches"][1].update(status="confirmed", profile_fact_ids=[7], gap="none")
    result = assess(data)
    assert result.status == "ALLOW"
    assert result.fit_tier == 1


@pytest.mark.parametrize("profession,tier", [("adjacent_it", 2), ("other_it", 3)])
def test_profession_priority(profession: str, tier: int) -> None:
    data = example()
    data[3]["matches"][1].update(status="confirmed", profile_fact_ids=[7], gap="none")
    data[3]["paths"][0]["profession"] = profession
    assert assess(data).fit_tier == tier


def test_explicit_alternative_accepts_one_suitable_path() -> None:
    data = example()
    extraction, matching = data[2:]
    for index, scope in enumerate(("python", "ruby")):
        extraction["scopes"].append(
            {"id": scope, "label": scope, "alternative_group": "choice", "source_lines": [0]}
        )
        extraction["entries"][index]["scope"] = scope
    matching["paths"] = [
        {"scope": scope, "profession": "applied_python", "core_entry_ids": [index], "reason": scope}
        for index, scope in enumerate(("python", "ruby"))
    ]
    result = assess(data)
    assert result.status == "ALLOW"
    assert result.selected_scopes == ("python",)
    assert result.fit_tier == 1

    extraction["scopes"][1]["alternative_group"] = ""
    extraction["scopes"][2]["alternative_group"] = ""
    assert assess(data).status == "REJECT"


def test_missing_education_outside_core_reduces_priority() -> None:
    data = example()
    data[2]["entries"][1].update(kind="required", activity="education")
    data[3]["matches"][1]["gap"] = "education"
    data[3]["paths"][0]["core_entry_ids"] = [0]
    result = assess(data)
    assert result.status == "ALLOW" and result.fit_tier == 3


@pytest.mark.parametrize("gap", ["unclear", "education"])
def test_unclear_core_condition_is_accepted_with_lower_priority(gap: str) -> None:
    data = example()
    data[3]["matches"][1]["gap"] = gap
    result = assess(data)
    assert result.status == "ALLOW" and result.fit_tier == 3
    assert result.reasons


def test_preferred_specialization_does_not_require_matching() -> None:
    data = example()
    data[2]["entries"][1]["kind"] = "preferred"
    data[3]["matches"].pop()
    data[3]["paths"][0]["core_entry_ids"] = [0]
    assert assess(data).status == "ALLOW"


@pytest.mark.parametrize(
    "corruption",
    [
        "quote",
        "term",
        "line",
        "missing_line",
        "both_included_and_excluded",
        "unknown_scope",
        "unknown_fact",
        "duplicate_match",
        "missing_match",
        "invented_match",
        "preferred_core",
        "missing_core",
        "wrong_core",
        "missing_path",
        "duplicate_path",
        "no_evidence",
        "confirmed_gap",
        "unconfirmed_no_gap",
        "invalid_issue_line",
    ],
)
def test_corrupt_evidence_requires_review(corruption: str) -> None:
    data = deepcopy(example())
    extraction, matching = data[2:]
    match corruption:
        case "quote":
            extraction["entries"][0]["quote"] = "Текст, которого нет в вакансии"
        case "term":
            extraction["entries"][0]["terms"] = [""]
        case "line":
            extraction["entries"][0]["line"] = 99
        case "missing_line":
            extraction["excluded_lines"] = []
        case "both_included_and_excluded":
            extraction["excluded_lines"].append({"line": 1, "reason": "heading"})
        case "unknown_scope":
            extraction["entries"][0]["scope"] = "invented"
        case "unknown_fact":
            matching["matches"][0]["profile_fact_ids"] = [99]
        case "duplicate_match":
            matching["matches"].append(deepcopy(matching["matches"][0]))
        case "missing_match":
            matching["matches"].pop()
        case "invented_match":
            matching["matches"][0]["entry_id"] = 99
        case "preferred_core":
            extraction["entries"][0]["kind"] = "preferred"
            matching["matches"].pop(0)
        case "missing_core":
            matching["paths"][0]["core_entry_ids"] = []
        case "wrong_core":
            matching["paths"][0]["core_entry_ids"] = [99]
        case "missing_path":
            matching["paths"] = []
        case "duplicate_path":
            matching["paths"].append(deepcopy(matching["paths"][0]))
        case "no_evidence":
            matching["matches"][0]["profile_fact_ids"] = []
        case "confirmed_gap":
            matching["matches"][0]["gap"] = "tool"
        case "unconfirmed_no_gap":
            matching["matches"][1]["gap"] = "none"
        case "invalid_issue_line":
            matching["source_issues"] = [{"line": 99, "issue": "Потеряно условие"}]
    result = assess(data)
    assert result.status == "REVIEW"
    assert result.fit_tier is None
    assert result.reasons


def test_non_it_and_unknown_profession() -> None:
    data = example()
    data[3]["matches"][1].update(status="confirmed", profile_fact_ids=[7], gap="none")
    data[3]["paths"][0]["profession"] = "non_it"
    assert assess(data).status == "REJECT"
    data[3]["paths"][0]["profession"] = "unclear"
    result = assess(data)
    assert result.status == "ALLOW" and result.fit_tier == 3


def test_source_disagreement_is_accepted_without_claiming_profile_match() -> None:
    data = example()
    data[3]["source_issues"] = [{"line": 2, "issue": "Неясна обязательность Ruby"}]
    result = assess(data)
    assert result.status == "ALLOW" and result.fit_tier == 3
    assert "Неясна обязательность Ruby" in " ".join(result.reasons)
    assert not result.blocking_entry_ids
    assert data[3]["matches"][1]["status"] == "unconfirmed"


def test_uncertain_profession_does_not_cancel_established_core_gap() -> None:
    data = example()
    data[3]["paths"][0]["profession"] = "unclear"
    assert assess(data).status == "REJECT"
