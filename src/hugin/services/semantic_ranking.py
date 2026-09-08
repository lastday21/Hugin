from __future__ import annotations

import json
import re
from typing import Any

from hugin.domain.directions import DirectionScope
from hugin.domain.vacancies import VacancyAvailability, VacancyData
from hugin.domain.vacancy_priority import FitTier
from hugin.services.semantic_selection import SemanticDecision
from hugin.services.vacancy_analysis import (
    PythonBackendRules,
    RuleCategory,
    RuleComponent,
    RuleContext,
    RuleEvaluation,
    _normalize_rule_text,
)
from hugin.services.vacancy_fit import FitAssessment


def independent_constraints(vacancy: VacancyData, context: RuleContext) -> tuple[str, ...]:
    rules = PythonBackendRules()
    body = " ".join(
        filter(
            None,
            (
                vacancy.description,
                vacancy.responsibilities,
                vacancy.required_qualifications,
                vacancy.preferred_qualifications,
            ),
        )
    )
    text = _normalize_rule_text(" ".join((vacancy.title, body, *vacancy.key_skills)))
    reasons: list[str] = []
    if not body.strip():
        reasons.append("описание вакансии отсутствует")
    if vacancy.availability is not VacancyAvailability.ACTIVE:
        reasons.append(f"вакансия недоступна: {vacancy.availability.value}")
    if rules._is_too_old(vacancy.published_at):
        reasons.append("вакансия опубликована более 30 дней назад")
    scam = next((marker for marker in rules._scam_markers if marker in text), None)
    if scam is not None:
        reasons.append(f"подозрительное требование: {scam}")
    unpaid_training = (
        re.search(r"\bстаж[её]р\w*|\bстажиров\w*", vacancy.title.casefold())
        and re.search(
            r"\bобучени\w*\s+не\s*оплачиваем(?:ое|о)\b|\bне\s*оплачиваемое\s+обучени\w*\b",
            text,
        )
        and re.search(
            r"\b(?:зарплат\w*|оплат\w*)[^.!?]{0,80}\bпосле\s+трудоустройств\w*\b|"
            r"\b(?:после|по итогам|по окончании)\s+обучени\w*[^.!?]{0,150}"
            r"\b(?:трудоустройств\w*|при[её]м\w*\s+в\s+штат|приглаш\w*)\b|"
            r"\bпоследующ\w*\s+трудоустройств\w*\b",
            text,
        )
    )
    if (
        rules._unpaid_compensation_pattern.search(text)
        or re.search(r"\bработ\w*\s+без\s+(?:денежной\s+)?оплат\w*", text)
        or unpaid_training
    ):
        reasons.append("работа явно не предусматривает денежную оплату")
    if rules._negative_candidate_exclusion_pattern.search(_normalize_rule_text(body)):
        reasons.append("работодатель прямо исключил кандидатов с текущим профилем разработки")
    if rules._relocation_conflicts(text, context):
        reasons.append("обязательный переезд противоречит подтверждённым настройкам")
    if rules._location_conflicts(vacancy, context):
        reasons.append("офис или гибрид находится вне выбранных регионов")
    if rules._work_format_score(vacancy, context) == 0:
        reasons.append("обязательный формат работы противоречит настройкам")
    return tuple(reasons)


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
    return RuleEvaluation(
        rules._weighted_score(components), category, tuple(reasons), tuple(components), route, fit
    )


def replay_semantic_evaluation(
    vacancy: VacancyData,
    context: RuleContext,
    scope: DirectionScope,
    evidence: dict[str, Any],
) -> RuleEvaluation:
    from hugin.services.semantic_results import StoredSelection, target_from_matching
    from hugin.services.semantic_selection import ProfileFact, SourceLine, assess_requirements

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
        if stored.errors or stored.extraction is None or stored.matching is None:
            decision = SemanticDecision(
                "REVIEW", None, tuple(stored.errors) or ("Неполный разбор вакансии",)
            )
        else:
            decision = assess_requirements(lines, facts, stored.extraction, stored.matching)
        target = target_from_matching(stored.matching, decision)
    if "routing_target_scope" in evidence:
        saved_target = evidence["routing_target_scope"]
        target = DirectionScope(saved_target) if saved_target is not None else None
    return semantic_evaluation(vacancy, context, scope, decision, target)
