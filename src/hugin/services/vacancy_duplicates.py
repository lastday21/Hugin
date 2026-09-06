from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher

from hugin.domain.vacancies import VacancyRecord
from hugin.services.requirement_sections import (
    RequirementKind,
    mandatory_text,
    primary_duties,
    requirement_sections,
)

_LANGUAGES = {
    "python": r"\bpython\b",
    "java": r"\bjava\b",
    "javascript": r"\b(?:javascript|typescript|node\.?js)\b",
    "go": r"\b(?:go|golang)\b",
    "php": r"\bphp\b",
    "ruby": r"\bruby\b",
    "rust": r"\brust\b",
    "scala": r"\bscala\b",
    "cpp": r"(?<!\w)[cс]\+\+(?!\w)",
    "csharp": r"(?<!\w)c#(?!\w)",
    "c": r"(?<!\w)c(?![\w+#])",
    "1c": r"(?<!\w)1[сc](?!\w)",
}
_PROFESSIONS = (
    ("teaching", r"преподавател|наставник|учитель|\binstructor\b"),
    ("testing", r"тестиров|тестирован|\b(?:qa|aqa|sdet)\b|автотест"),
    ("operations", r"\b(?:devops|sre|администратор)\b|инженер инфраструктуры"),
    ("analysis", r"аналитик|\banalyst\b"),
    ("support", r"поддержк|сопровожден|\bsupport\b"),
    ("development", r"разработ|программист|\bdeveloper\b|software engineer"),
)


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    canonical: VacancyRecord
    similarity: float


class VacancyDuplicateDetector:
    def conflict_reason(self, left: VacancyRecord, right: VacancyRecord) -> str | None:
        left_employer = self._normalized(left.employer_name)
        right_employer = self._normalized(right.employer_name)
        if left_employer and right_employer and left_employer != right_employer:
            return "different_employers"
        if not self._compatible_profession(left, right):
            return "different_profession_or_required_language"
        left_duties = primary_duties(left.description, left.responsibilities)
        right_duties = primary_duties(right.description, right.responsibilities)
        if left_duties and right_duties and self._text_similarity(left_duties, right_duties) < 0.78:
            return "different_responsibilities"
        return None

    def find(
        self,
        vacancy: VacancyRecord,
        candidates: list[VacancyRecord],
    ) -> DuplicateMatch | None:
        best: DuplicateMatch | None = None
        for candidate in candidates:
            similarity = self._similarity(vacancy, candidate)
            if similarity is None or (best is not None and similarity <= best.similarity):
                continue
            best = DuplicateMatch(candidate, similarity)
        return best

    def _similarity(self, left: VacancyRecord, right: VacancyRecord) -> float | None:
        if self._normalized(left.employer_name) != self._normalized(right.employer_name):
            return None
        if not self._compatible_profession(left, right):
            return None
        title = self._text_similarity(left.title, right.title)
        left_body = primary_duties(left.description, left.responsibilities)
        right_body = primary_duties(right.description, right.responsibilities)
        body = self._text_similarity(left_body, right_body)
        if not self._salary_compatible(left, right):
            return None
        salary = self._salary_similarity(left, right)
        if body >= 0.97:
            return body * 0.85 + salary * 0.15
        if title < 0.82 or body < 0.78:
            return None
        combined = title * 0.35 + body * 0.5 + salary * 0.15
        return combined if combined >= 0.82 else None

    @staticmethod
    def _compatible_profession(left: VacancyRecord, right: VacancyRecord) -> bool:
        def profession(vacancy: VacancyRecord) -> str | None:
            return next(
                (name for name, pattern in _PROFESSIONS if re.search(pattern, vacancy.title, re.I)),
                None,
            )

        left_profession, right_profession = profession(left), profession(right)
        if left_profession and right_profession and left_profession != right_profession:
            return False

        def languages(text: str) -> set[str]:
            return {name for name, pattern in _LANGUAGES.items() if re.search(pattern, text, re.I)}

        left_title, right_title = languages(left.title), languages(right.title)
        if left_title and right_title and not left_title & right_title:
            return False

        def requirements(vacancy: VacancyRecord) -> tuple[set[str], list[set[str]]]:
            required: set[str] = set()
            alternatives: list[set[str]] = []
            for text, unlabelled in (
                (vacancy.required_qualifications or "", True),
                (vacancy.description or "", False),
            ):
                for section in requirement_sections(text):
                    if section.kind is not RequirementKind.REQUIRED and not (
                        unlabelled and section.kind is RequirementKind.UNLABELLED
                    ):
                        continue
                    for clause in re.split(r"[\n;]+|(?<=[.!?])\s+", section.text):
                        clause = mandatory_text(clause, include_unlabelled=True)
                        found = languages(clause)
                        choice = re.search(r"\b(?:одном|одного|один|любом|любого)\s+из\b", clause)
                        if choice and not languages(clause[: choice.start()]):
                            if found:
                                alternatives.append(found)
                        else:
                            required.update(found)
            return required, alternatives

        left_required, left_options = requirements(left)
        right_required, right_options = requirements(right)
        if left_required and right_required and left_required != right_required:
            return False
        if any(left_required and not left_required & option for option in right_options):
            return False
        if any(right_required and not right_required & option for option in left_options):
            return False
        return all(a & b for a in left_options for b in right_options)

    @staticmethod
    def _normalized(value: str | None) -> str:
        return re.sub(r"[^a-zа-яё0-9]+", " ", (value or "").casefold()).strip()

    def _text_similarity(self, left: str, right: str) -> float:
        normalized_left = self._normalized(left)
        normalized_right = self._normalized(right)
        if not normalized_left or not normalized_right:
            return 0.0
        sequence = SequenceMatcher(None, normalized_left, normalized_right, autojunk=False).ratio()
        left_tokens = set(normalized_left.split())
        right_tokens = set(normalized_right.split())
        union = left_tokens | right_tokens
        jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
        return max(sequence, jaccard)

    @classmethod
    def _salary_compatible(cls, left: VacancyRecord, right: VacancyRecord) -> bool:
        if (
            left.salary_currency
            and right.salary_currency
            and left.salary_currency != right.salary_currency
        ):
            return False
        left_range = cls._salary_range(left)
        right_range = cls._salary_range(right)
        if left_range is None or right_range is None:
            return True
        return max(left_range[0], right_range[0]) <= min(left_range[1], right_range[1])

    @classmethod
    def _salary_similarity(cls, left: VacancyRecord, right: VacancyRecord) -> float:
        left_range = cls._salary_range(left)
        right_range = cls._salary_range(right)
        if left_range is None or right_range is None:
            return 0.5
        intersection = min(left_range[1], right_range[1]) - max(left_range[0], right_range[0])
        union = max(left_range[1], right_range[1]) - min(left_range[0], right_range[0])
        if union == 0:
            return 1.0
        return float(max(intersection, Decimal(0)) / union)

    @staticmethod
    def _salary_range(vacancy: VacancyRecord) -> tuple[Decimal, Decimal] | None:
        if vacancy.salary_from is None and vacancy.salary_to is None:
            return None
        lower = vacancy.salary_from or vacancy.salary_to
        upper = vacancy.salary_to or vacancy.salary_from
        if lower is None or upper is None:
            return None
        return lower, upper
