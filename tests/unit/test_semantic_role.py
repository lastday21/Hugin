from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from hugin.domain.vacancy_priority import FitTier
from hugin.services.semantic_role import RoleAssessment, assess_role, role_errors
from hugin.services.semantic_selection import ProfileFact, SourceLine

LINES = [
    SourceLine(id=0, field="title", text="Разработчик внутренних сервисов"),
    SourceLine(id=1, field="description", text="Разрабатывать сервисы на Python и SQL"),
    SourceLine(id=2, field="description", text="Обязательна самостоятельная разработка на Ruby"),
]
FACTS = [ProfileFact(id=7, category="project", content="Создал сервис на Python и PostgreSQL")]
ANSWER: dict[str, Any] = {
    "fit": "direct",
    "profession": "applied_python",
    "role": "Разработка внутренних сервисов",
    "reason": "Основная работа совпадает с подтверждённым проектом",
    "source_line_ids": [1],
    "profile_fact_ids": [7],
    "gaps": [],
    "blocker": None,
}


@pytest.mark.parametrize(
    ("fit", "tier"),
    [("direct", FitTier.DIRECT), ("related", FitTier.RELATED), ("possible", FitTier.POSSIBLE)],
)
def test_whole_role_preserves_supported_priority(fit: str, tier: FitTier) -> None:
    answer = RoleAssessment.model_validate({**ANSWER, "fit": fit})
    result = assess_role(LINES, FACTS, answer)
    assert result.status == "ALLOW" and result.fit_tier is tier
    assert ANSWER["reason"] in result.reasons
    assert result.blocking_entry_ids == ()


def test_rejection_cites_the_actual_source_without_invented_requirement() -> None:
    answer = RoleAssessment.model_validate(
        {
            **ANSWER,
            "fit": "reject",
            "profile_fact_ids": [],
            "blocker": {
                "source_line_ids": [2],
                "reason": "Основная разработка Ruby не подтверждена",
            },
        }
    )
    result = assess_role(LINES, FACTS, answer)
    assert result.status == "REJECT" and result.fit_tier is None
    assert any(LINES[2].text in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "changes",
    [
        {"source_line_ids": [99]},
        {"source_line_ids": [0]},
        {"source_line_ids": [1, 1]},
        {"profile_fact_ids": [999]},
        {"profile_fact_ids": [7, 7]},
        {"profile_fact_ids": []},
        {"role": "   "},
        {"reason": "   "},
        {"gaps": ["   "]},
        {"profession": "non_it"},
        {"profession": "unclear"},
        {"fit": "reject"},
        {"blocker": {"source_line_ids": [2], "reason": "Чужая основная работа"}},
        {
            "fit": "reject",
            "blocker": {"source_line_ids": [99], "reason": "Несуществующая строка"},
        },
        {
            "fit": "reject",
            "blocker": {"source_line_ids": [2, 2], "reason": "Повтор основания"},
        },
        {"fit": "reject", "blocker": {"source_line_ids": [2], "reason": "   "}},
        {"fit": "reject", "blocker": {"source_line_ids": [0], "reason": "Только название"}},
    ],
)
def test_invalid_grounding_cannot_enter_the_queue(changes: dict[str, Any]) -> None:
    answer = RoleAssessment.model_validate({**deepcopy(ANSWER), **changes})
    assert role_errors(LINES, FACTS, answer)
    result = assess_role(LINES, FACTS, answer)
    assert result.status == "REVIEW" and result.fit_tier is None


def test_missing_tool_remains_an_explicit_gap_in_an_allowed_role() -> None:
    answer = RoleAssessment.model_validate(
        {**ANSWER, "fit": "possible", "gaps": ["Опыт нового средства не подтверждён"]}
    )
    result = assess_role(LINES, FACTS, answer)
    assert result.status == "ALLOW" and result.fit_tier is FitTier.POSSIBLE
    assert any("Опыт нового средства не подтверждён" in reason for reason in result.reasons)


def test_unclear_profession_can_only_have_low_priority() -> None:
    answer = RoleAssessment.model_validate({**ANSWER, "fit": "possible", "profession": "unclear"})
    assert assess_role(LINES, FACTS, answer).fit_tier is FitTier.POSSIBLE


@pytest.mark.parametrize("empty", ["source", "profile", "duplicate_source", "duplicate_profile"])
def test_missing_or_ambiguous_input_does_not_produce_a_decision(empty: str) -> None:
    lines = [] if empty == "source" else LINES + (LINES[:1] if empty == "duplicate_source" else [])
    facts = [] if empty == "profile" else FACTS + (FACTS if empty == "duplicate_profile" else [])
    assert assess_role(lines, facts, RoleAssessment.model_validate(ANSWER)).status == "REVIEW"


def test_preferred_skills_cannot_be_the_only_ground_for_rejection() -> None:
    lines = [*LINES, SourceLine(id=3, field="preferred_qualifications", text="Ruby будет плюсом")]
    answer = RoleAssessment.model_validate(
        {**ANSWER, "fit": "reject", "blocker": {"source_line_ids": [3], "reason": "Нет Ruby"}}
    )
    assert assess_role(lines, FACTS, answer).status == "REVIEW"
