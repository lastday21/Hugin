from __future__ import annotations

import json
from typing import Any

from hugin.domain.directions import DirectionScope
from hugin.domain.vacancies import VacancyData
from hugin.domain.vacancy_priority import FIT_TIER_LABELS, FitTier
from hugin.services.semantic_selection import SemanticDecision
from hugin.services.vacancy_analysis import (
    PythonBackendRules,
    RuleCategory,
    RuleComponent,
    RuleContext,
    RuleEvaluation,
)
from hugin.services.vacancy_fit import FitAssessment


def independent_constraints(vacancy: VacancyData, context: RuleContext) -> tuple[str, ...]:
    return PythonBackendRules().independent_constraints(vacancy, context)


def semantic_evaluation(
    vacancy: VacancyData,
    context: RuleContext,
    scope: DirectionScope,
    decision: SemanticDecision,
    target_scope: DirectionScope | None,
) -> RuleEvaluation:
    rules = PythonBackendRules()
    constraints = independent_constraints(vacancy, context)
    reasons = [*constraints, *decision.reasons]
    components: list[RuleComponent] = []
    for name, score, weight, label in (
        ("region", rules._region_score(vacancy, context), 10, "регион"),
        ("format", rules._work_format_score(vacancy, context), 10, "формат работы"),
        ("salary", rules._salary_score(vacancy, context), 10, "зарплата"),
        (
            "experience",
            rules._experience_score(rules._normalize_experience(vacancy.experience)),
            10,
            "требования к опыту снижают приоритет, но не запрещают отклик",
        ),
        ("freshness", rules._freshness_score(vacancy.published_at), 5, "свежесть"),
    ):
        if score is not None:
            rules._component(components, reasons, name, score, weight, label)
    fit = None
    route = None
    if constraints or decision.status == "REJECT":
        category = RuleCategory.REJECTED
    elif decision.status == "REVIEW":
        category = RuleCategory.REVIEW
    else:
        fit = FitAssessment(decision.fit_tier or FitTier.POSSIBLE, "; ".join(decision.reasons), ())
        category = RuleCategory.MATCH if fit.tier is FitTier.DIRECT else RuleCategory.STRETCH
        if target_scope is not None and scope is not target_scope:
            category = RuleCategory.ROUTED
            route = target_scope
    score = 0.0
    if fit is not None:
        conditions_score = rules._weighted_score(components)
        lower, upper = {
            FitTier.DIRECT: (70, 100),
            FitTier.RELATED: (40, 69),
            FitTier.POSSIBLE: (0, 39),
        }[fit.tier]
        score = round(lower + (upper - lower) * conditions_score / 100, 2)
        reasons.append(
            f"{FIT_TIER_LABELS[fit.tier]}: диапазон рейтинга {lower}–{upper}; "
            f"условия внутри диапазона: {conditions_score:.2f}/100"
        )
    return RuleEvaluation(score, category, tuple(reasons), tuple(components), route, fit)


def replay_semantic_evaluation(
    vacancy: VacancyData,
    context: RuleContext,
    scope: DirectionScope,
    evidence: dict[str, Any],
) -> RuleEvaluation:
    from hugin.services.semantic_results import (
        StoredSelection,
        stored_decision,
        target_from_selection,
    )
    from hugin.services.semantic_selection import (
        SEMANTIC_SELECTION_VERSION,
        ProfileFact,
        SourceLine,
    )

    if evidence.get("status") == "CONFIGURATION_ERROR":
        return RuleEvaluation(0, RuleCategory.REVIEW, ("Некорректная настройка смыслового отбора",))
    if "stored" not in evidence:
        decision = SemanticDecision(
            "REVIEW", None, ("Ожидает смыслового разбора текущей вакансии и профиля",)
        )
        target = None
    else:
        stored = StoredSelection.model_validate_json(json.dumps(evidence["stored"]))
        request = evidence["request"]
        lines = [SourceLine.model_validate(item) for item in request["source"]]
        facts = [
            ProfileFact(id=item["id"], category=item["category"], content=item["content"])
            for item in request["profile"]["facts"]
            if item["content"].strip()
        ]
        decision = stored_decision(
            lines, facts, stored, str(request.get("version", SEMANTIC_SELECTION_VERSION))
        )
        target = target_from_selection(stored, decision)
    if "routing_target_scope" in evidence:
        saved_target = evidence["routing_target_scope"]
        target = DirectionScope(saved_target) if saved_target is not None else None
    return semantic_evaluation(vacancy, context, scope, decision, target)
