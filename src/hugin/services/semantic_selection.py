from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hugin.domain.vacancy_priority import FitTier

SEMANTIC_SELECTION_VERSION = "requirements_v2"
type SelectionStatus = Literal["ALLOW", "REJECT", "REVIEW"]
BLOCKING_GAPS = frozenset(
    {
        "other_development",
        "model_research",
        "data_platforms",
        "specialized_operations",
        "hardware",
        "non_it",
    }
)


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SourceLine(StrictRecord):
    id: int = Field(ge=0)
    field: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ProfileFact(StrictRecord):
    id: int = Field(gt=0)
    category: str = Field(min_length=1)
    content: str = Field(min_length=1)
    actual_at: str | None = None


class RequirementScope(StrictRecord):
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=240)
    alternative_group: str = Field(max_length=80)
    source_lines: list[int]


class RequirementDraft(StrictRecord):
    line: int = Field(ge=0)
    subject: str = Field(min_length=1, max_length=400)
    scope: str = Field(min_length=1, max_length=80)
    kind: Literal["duty", "required", "preferred", "context", "unclear"]
    activity: Literal[
        "development",
        "data",
        "testing",
        "support",
        "infrastructure",
        "hardware",
        "model_training",
        "management",
        "business",
        "education",
        "other",
    ]
    level: Literal["familiarity", "working", "advanced", "unspecified"]
    relation: Literal["all", "one_of", "examples", "unclear"]
    terms: list[str]


class RequirementEntry(RequirementDraft):
    quote: str = Field(min_length=1)


class ExcludedLine(StrictRecord):
    line: int = Field(ge=0)
    reason: Literal["heading", "company", "work_conditions", "personal_qualities", "nontechnical"]


class Extraction(StrictRecord):
    scopes: list[RequirementScope]
    entries: list[RequirementEntry]
    excluded_lines: list[ExcludedLine]


class ExtractionDraft(StrictRecord):
    scopes: list[RequirementScope]
    entries: list[RequirementDraft]
    excluded_lines: list[ExcludedLine]


def attach_source_lines(lines: list[SourceLine], draft: ExtractionDraft) -> Extraction:
    source = {line.id: line.text for line in lines}
    return Extraction(
        scopes=draft.scopes,
        entries=[
            RequirementEntry(**entry.model_dump(), quote=source[entry.line])
            for entry in draft.entries
        ],
        excluded_lines=draft.excluded_lines,
    )


def draft_errors(lines: list[SourceLine], draft: ExtractionDraft) -> tuple[str, ...]:
    known = {line.id for line in lines}
    if any(entry.line not in known for entry in draft.entries):
        return ("Условие ссылается на неизвестную исходную строку",)
    return extraction_errors(lines, attach_source_lines(lines, draft))


class SourceIssue(StrictRecord):
    line: int = Field(ge=0)
    issue: str = Field(min_length=1, max_length=1000)


class RequirementMatch(StrictRecord):
    entry_id: int = Field(ge=0)
    status: Literal["confirmed", "partial", "unconfirmed"]
    profile_fact_ids: list[int]
    gap: Literal[
        "none",
        "tool",
        "experience",
        "career_interest",
        "selection_step",
        "other_development",
        "model_research",
        "data_platforms",
        "specialized_operations",
        "hardware",
        "non_it",
        "education",
        "unclear",
    ]
    reason: str = Field(min_length=1, max_length=1000)


class ProfessionalPath(StrictRecord):
    scope: str = Field(min_length=1, max_length=80)
    profession: Literal["applied_python", "adjacent_it", "other_it", "non_it", "unclear"]
    core_entry_ids: list[int]
    reason: str = Field(min_length=1, max_length=1000)


class Matching(StrictRecord):
    source_issues: list[SourceIssue]
    matches: list[RequirementMatch]
    paths: list[ProfessionalPath]


@dataclass(frozen=True, slots=True)
class SemanticDecision:
    status: SelectionStatus
    fit_tier: FitTier | None
    reasons: tuple[str, ...]
    selected_scopes: tuple[str, ...] = ()
    blocking_entry_ids: tuple[int, ...] = ()


def required_entry_ids(extraction: Extraction) -> set[int]:
    return {
        index
        for index, entry in enumerate(extraction.entries)
        if entry.kind in {"duty", "required"}
    }


def expected_path_scopes(extraction: Extraction) -> set[str]:
    return {scope.id for scope in extraction.scopes if scope.id != "common"} or {"common"}


def extraction_errors(lines: list[SourceLine], extraction: Extraction) -> tuple[str, ...]:
    errors: list[str] = []
    source = {line.id: line.text for line in lines}
    if len(source) != len(lines) or not source:
        errors.append("Исходные строки отсутствуют или их номера повторяются")
    scopes = {scope.id for scope in extraction.scopes}
    if len(scopes) != len(extraction.scopes) or "common" not in scopes:
        errors.append("Направления повторяются или отсутствуют общие условия")
    for scope in extraction.scopes:
        if not set(scope.source_lines) <= source.keys():
            errors.append(f"Направление {scope.id}: неверные номера исходных строк")
        if scope.id != "common" and not scope.source_lines:
            errors.append(f"Направление {scope.id}: нет основания для выделения")
        if scope.id == "common" and scope.alternative_group:
            errors.append("Общие условия нельзя объявлять альтернативным направлением")
    included: set[int] = set()
    for index, entry in enumerate(extraction.entries):
        included.add(entry.line)
        line = source.get(entry.line, "")
        if not line or entry.quote != line:
            errors.append(f"Условие {index}: цитата не совпадает со строкой {entry.line}")
        if any(not term.strip() for term in entry.terms):
            errors.append(f"Условие {index}: пустое название предмета")
        if entry.scope not in scopes:
            errors.append(f"Условие {index}: неизвестное направление")
    excluded = {line.line for line in extraction.excluded_lines}
    if len(excluded) != len(extraction.excluded_lines):
        errors.append("Номера исключённых строк повторяются")
    if included & excluded:
        errors.append("Одна строка одновременно разобрана и исключена")
    covered = included | excluded
    if covered != source.keys():
        details = []
        if missing := source.keys() - covered:
            details.append("пропущены " + ", ".join(map(str, sorted(missing))))
        if unexpected := covered - source.keys():
            details.append("лишние " + ", ".join(map(str, sorted(unexpected))))
        errors.append("Разбор не покрывает точный набор исходных строк: " + "; ".join(details))
    return tuple(errors)


def matching_errors(
    lines: list[SourceLine],
    facts: list[ProfileFact],
    extraction: Extraction,
    matching: Matching,
) -> tuple[str, ...]:
    errors: list[str] = []
    required = required_entry_ids(extraction)
    ids = [item.entry_id for item in matching.matches]
    if set(ids) != required or len(ids) != len(set(ids)):
        errors.append("Не все обязательные условия сопоставлены ровно один раз")
    known_facts = {fact.id for fact in facts}
    if len(known_facts) != len(facts):
        errors.append("Номера подтверждённых фактов повторяются")
    for item in matching.matches:
        if not set(item.profile_fact_ids) <= known_facts:
            errors.append(f"Условие {item.entry_id}: неизвестный факт профиля")
        if item.status in {"confirmed", "partial"} and not item.profile_fact_ids:
            errors.append(f"Условие {item.entry_id}: нет подтверждающих фактов")
        if (item.status == "confirmed") != (item.gap == "none"):
            errors.append(f"Условие {item.entry_id}: подтверждение противоречит пробелу")
        if (
            item.gap in {"career_interest", "selection_step"}
            and item.entry_id in required
            and extraction.entries[item.entry_id].activity != "other"
        ):
            errors.append(
                f"Условие {item.entry_id}: профессиональный навык нельзя заменить намерением"
            )
    scopes = expected_path_scopes(extraction)
    if {path.scope for path in matching.paths} != scopes or len(matching.paths) != len(scopes):
        errors.append("Не все направления оценены ровно один раз")
    for path in matching.paths:
        allowed = {
            index for index in required if extraction.entries[index].scope in {"common", path.scope}
        }
        only_unclear = not allowed and any(
            entry.kind == "unclear" and entry.scope in {"common", path.scope}
            for entry in extraction.entries
        )
        if (not path.core_entry_ids and not only_unclear) or not set(
            path.core_entry_ids
        ) <= allowed:
            errors.append(f"Направление {path.scope}: неверные основные условия")
        if len(path.core_entry_ids) != len(set(path.core_entry_ids)):
            errors.append(f"Направление {path.scope}: основные условия повторяются")
    known_lines = {line.id for line in lines}
    if any(issue.line not in known_lines for issue in matching.source_issues):
        errors.append("Замечание к разбору ссылается на неизвестную строку")
    return tuple(errors)


def _path_decision(
    path: ProfessionalPath,
    extraction: Extraction,
    matches: dict[int, RequirementMatch],
) -> SemanticDecision:
    relevant = {
        index: item
        for index, item in matches.items()
        if extraction.entries[index].scope in {"common", path.scope}
    }
    blocking = tuple(index for index in path.core_entry_ids if matches[index].gap in BLOCKING_GAPS)
    if blocking or path.profession == "non_it":
        reasons = tuple(matches[index].reason for index in blocking) or (path.reason,)
        return SemanticDecision("REJECT", None, reasons, blocking_entry_ids=blocking)
    unresolved = tuple(
        item.reason for item in relevant.values() if item.gap in {"unclear", "education"}
    )
    unresolved += tuple(
        f"Неоднозначное условие: {entry.subject}"
        for entry in extraction.entries
        if entry.kind == "unclear" and entry.scope in {"common", path.scope}
    )
    if unresolved or path.profession == "unclear":
        return SemanticDecision(
            "ALLOW", FitTier.POSSIBLE, (path.reason, *unresolved), (path.scope,)
        )
    tier = {
        "applied_python": FitTier.DIRECT,
        "adjacent_it": FitTier.RELATED,
        "other_it": FitTier.POSSIBLE,
    }[path.profession]
    gaps = tuple(
        item.reason
        for item in relevant.values()
        if item.gap not in {"none", "career_interest", "selection_step"}
    )
    if gaps:
        tier = FitTier.POSSIBLE
    return SemanticDecision("ALLOW", tier, (path.reason, *gaps), (path.scope,))


def assess_requirements(
    lines: list[SourceLine],
    facts: list[ProfileFact],
    extraction: Extraction,
    matching: Matching,
) -> SemanticDecision:
    errors = (
        *extraction_errors(lines, extraction),
        *matching_errors(lines, facts, extraction, matching),
    )
    if errors:
        return SemanticDecision("REVIEW", None, errors)
    if matching.source_issues:
        return SemanticDecision(
            "ALLOW",
            FitTier.POSSIBLE,
            tuple(f"Строка {issue.line}: {issue.issue}" for issue in matching.source_issues),
        )
    matches = {item.entry_id: item for item in matching.matches}
    paths = {path.scope: _path_decision(path, extraction, matches) for path in matching.paths}
    groups: dict[str, list[SemanticDecision]] = {}
    for scope in extraction.scopes:
        if scope.id not in paths:
            continue
        key = f"alternative:{scope.alternative_group}" if scope.alternative_group else scope.id
        groups.setdefault(key, []).append(paths[scope.id])
    selected: list[SemanticDecision] = []
    for choices in groups.values():
        allowed = [choice for choice in choices if choice.status == "ALLOW"]
        if allowed:
            selected.append(
                min(allowed, key=lambda choice: int(choice.fit_tier or FitTier.POSSIBLE))
            )
        else:
            status: SelectionStatus = (
                "REVIEW" if any(choice.status == "REVIEW" for choice in choices) else "REJECT"
            )
            selected.append(
                SemanticDecision(
                    status,
                    None,
                    tuple(reason for choice in choices for reason in choice.reasons),
                    blocking_entry_ids=tuple(
                        index for choice in choices for index in choice.blocking_entry_ids
                    ),
                )
            )
    for status in ("REJECT", "REVIEW"):
        failures = [choice for choice in selected if choice.status == status]
        if failures:
            return SemanticDecision(
                status,
                None,
                tuple(reason for choice in failures for reason in choice.reasons),
                blocking_entry_ids=tuple(
                    sorted({index for choice in failures for index in choice.blocking_entry_ids})
                ),
            )
    return SemanticDecision(
        "ALLOW",
        max(choice.fit_tier or FitTier.POSSIBLE for choice in selected),
        tuple(reason for choice in selected for reason in choice.reasons),
        tuple(scope for choice in selected for scope in choice.selected_scopes),
    )
