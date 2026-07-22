from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher

from hugin.domain.vacancies import VacancyRecord


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    canonical: VacancyRecord
    similarity: float


class VacancyDuplicateDetector:
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
        title = self._text_similarity(left.title, right.title)
        left_body = left.responsibilities or left.description or ""
        right_body = right.responsibilities or right.description or ""
        body = self._text_similarity(left_body, right_body)
        if title < 0.82 or body < 0.78 or not self._salary_compatible(left, right):
            return None
        salary = self._salary_similarity(left, right)
        combined = title * 0.35 + body * 0.5 + salary * 0.15
        return combined if combined >= 0.82 else None

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
