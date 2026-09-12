from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Protocol

from pydantic import BaseModel, ValidationError

from hugin.services.decision_evidence import canonical_json, fingerprint
from hugin.services.semantic_prompts import EXTRACTION_INSTRUCTIONS, MATCHING_INSTRUCTIONS
from hugin.services.semantic_role import RoleAssessment
from hugin.services.semantic_selection import (
    Extraction,
    ExtractionDraft,
    Matching,
    ProfileFact,
    SemanticDecision,
    SourceLine,
    assess_requirements,
    attach_source_lines,
    draft_errors,
    expected_path_scopes,
    matching_errors,
    required_entry_ids,
)

FAILED_STAGE_RETRY_AFTER = timedelta(minutes=30)


class StructuredClient(Protocol):
    @property
    def request_identity(self) -> str: ...

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, object],
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class StageRecord:
    cache_key: str
    stage: str
    model: str
    request: dict[str, object]
    response_text: str
    response_sha256: str
    errors: tuple[str, ...]
    created_at: datetime
    duration_seconds: float


class StageCache(Protocol):
    def get(self, key: str) -> StageRecord | None: ...

    def put(self, record: StageRecord) -> None: ...


class MemoryStageCache:
    def __init__(self) -> None:
        self.records: dict[str, StageRecord] = {}

    def get(self, key: str) -> StageRecord | None:
        return self.records.get(key)

    def put(self, record: StageRecord) -> None:
        self.records[record.cache_key] = record


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    decision: SemanticDecision
    extraction: Extraction | None
    matching: Matching | None
    stages: tuple[StageRecord, ...]
    model_calls: int
    budget_exhausted: bool = False
    assessment: RoleAssessment | None = None


class StageAnalyzer:
    def __init__(self, cache: StageCache, *, max_calls: int = 1, force: bool = False) -> None:
        if not 1 <= max_calls <= 6:
            raise ValueError("Число обращений должно быть от 1 до 6")
        self._cache = cache
        self._max_calls = max_calls
        self._force = force
        self._calls = 0
        self._budget_exhausted = False
        self._stages: list[StageRecord] = []

    def _stage[T: BaseModel](
        self,
        stage: str,
        client: StructuredClient,
        instructions: str,
        payload: dict[str, object],
        record_type: type[T],
        validate: Callable[[T], tuple[str, ...]],
    ) -> tuple[T | None, tuple[str, ...]]:
        schema = record_type.model_json_schema()
        request: dict[str, object] = {
            "model": client.request_identity,
            "instructions": instructions,
            "payload": payload,
            "schema": schema,
        }
        key = fingerprint(request)
        record = None if self._force else self._cache.get(key)
        if record is not None and (
            record.cache_key != key
            or fingerprint(record.request) != key
            or fingerprint(record.response_text) != record.response_sha256
            or (record.errors and datetime.now(UTC) - record.created_at >= FAILED_STAGE_RETRY_AFTER)
        ):
            record = None
        if record is None:
            if self._calls >= self._max_calls:
                self._budget_exhausted = True
                return None, ("Достигнут предел обращений при исправлении разбора",)
            self._calls += 1
            started = monotonic()
            response = ""
            try:
                response = client.complete_json(instructions, canonical_json(payload), schema)
                _answer, errors = self._parse(response, record_type, validate)
            except (RuntimeError, OSError) as error:
                errors = (str(error) or "Не удалось получить разбор вакансии",)
            record = StageRecord(
                key,
                stage,
                client.request_identity,
                request,
                response,
                fingerprint(response),
                errors,
                datetime.now(UTC),
                monotonic() - started,
            )
            self._cache.put(record)
        self._stages.append(record)
        if not record.response_text:
            return None, record.errors or ("Модель вернула пустой ответ",)
        return self._parse(record.response_text, record_type, validate)

    @staticmethod
    def _parse[T: BaseModel](
        response: str,
        record_type: type[T],
        validate: Callable[[T], tuple[str, ...]],
    ) -> tuple[T | None, tuple[str, ...]]:
        try:
            answer = record_type.model_validate_json(response)
        except ValidationError as error:
            messages = tuple(
                f"Поле {'.'.join(map(str, item['loc'])) or 'ответ'}: {item['msg']}"
                for item in error.errors(include_input=False)
            )
            return None, messages
        errors = validate(answer)
        return (None, errors) if errors else (answer, ())


class SemanticAnalyzer(StageAnalyzer):
    def __init__(
        self,
        extractor: StructuredClient,
        matcher: StructuredClient,
        cache: StageCache,
        *,
        max_calls: int = 6,
        force: bool = False,
    ) -> None:
        super().__init__(cache, max_calls=max_calls, force=force)
        self._extractor = extractor
        self._matcher = matcher

    def analyze(self, lines: list[SourceLine], facts: list[ProfileFact]) -> AnalysisResult:
        self._calls = 0
        self._budget_exhausted = False
        self._stages = []
        if not lines or not facts:
            return self._result(
                None, None, ("Нет полного текста или подтверждённых сведений профиля",)
            )
        source: dict[str, object] = {"vacancy_lines": [line.model_dump() for line in lines]}
        extraction, errors = self._extract(lines, source)
        if extraction is None:
            return self._result(None, None, errors)
        matching, errors = self._match(lines, facts, extraction)
        if matching is None:
            return self._result(extraction, None, errors)
        if matching.source_issues:
            revision = {
                **source,
                "previous_response": extraction.model_dump(
                    exclude={"entries": {"__all__": {"quote"}}}
                ),
                "review_issues": [issue.model_dump() for issue in matching.source_issues],
                "revision_task": (
                    "Сверь замечания с полным исходным текстом. Исправь только доказанные ошибки. "
                    "Сохрани правильные условия; замечание не заменяет источник."
                ),
            }
            revised, errors = self._stage(
                "extract_revision",
                self._extractor,
                EXTRACTION_INSTRUCTIONS,
                revision,
                ExtractionDraft,
                lambda answer: draft_errors(lines, answer),
            )
            if revised is None and self._stages and self._stages[-1].response_text:
                revised, errors = self._stage(
                    "extract_revision_repair",
                    self._extractor,
                    EXTRACTION_INSTRUCTIONS,
                    {
                        **revision,
                        "previous_response": self._stages[-1].response_text,
                        "validation_errors": list(errors),
                    },
                    ExtractionDraft,
                    lambda answer: draft_errors(lines, answer),
                )
            if revised is None:
                return self._result(extraction, matching, errors)
            extraction = attach_source_lines(lines, revised)
            matching, errors = self._match(lines, facts, extraction)
            if matching is None:
                return self._result(extraction, None, errors)
        return AnalysisResult(
            assess_requirements(lines, facts, extraction, matching),
            extraction,
            matching,
            tuple(self._stages),
            self._calls,
        )

    def _extract(
        self,
        lines: list[SourceLine],
        source: dict[str, object],
    ) -> tuple[Extraction | None, tuple[str, ...]]:
        answer, errors = self._stage(
            "extract",
            self._extractor,
            EXTRACTION_INSTRUCTIONS,
            source,
            ExtractionDraft,
            lambda value: draft_errors(lines, value),
        )
        if answer is None and self._stages and self._stages[-1].response_text:
            answer, errors = self._stage(
                "extract_repair",
                self._extractor,
                EXTRACTION_INSTRUCTIONS,
                {
                    **source,
                    "previous_response": self._stages[-1].response_text,
                    "validation_errors": list(errors),
                },
                ExtractionDraft,
                lambda value: draft_errors(lines, value),
            )
        return (attach_source_lines(lines, answer) if answer is not None else None), errors

    def _match(
        self,
        lines: list[SourceLine],
        facts: list[ProfileFact],
        extraction: Extraction,
    ) -> tuple[Matching | None, tuple[str, ...]]:
        payload: dict[str, object] = {
            "vacancy_lines": [line.model_dump() for line in lines],
            "extraction": {
                **extraction.model_dump(),
                "entries": [
                    {"entry_id": index, **entry.model_dump()}
                    for index, entry in enumerate(extraction.entries)
                ],
            },
            "required_entry_ids": sorted(required_entry_ids(extraction)),
            "expected_path_scopes": sorted(expected_path_scopes(extraction)),
            "profile": {"facts": [fact.model_dump() for fact in facts]},
        }
        answer, errors = self._stage(
            "match",
            self._matcher,
            MATCHING_INSTRUCTIONS,
            payload,
            Matching,
            lambda value: matching_errors(lines, facts, extraction, value),
        )
        if answer is None and self._stages and self._stages[-1].response_text:
            answer, errors = self._stage(
                "match_repair",
                self._matcher,
                MATCHING_INSTRUCTIONS,
                {
                    **payload,
                    "previous_response": self._stages[-1].response_text,
                    "validation_errors": list(errors),
                },
                Matching,
                lambda value: matching_errors(lines, facts, extraction, value),
            )
        return answer, errors

    def _result(
        self,
        extraction: Extraction | None,
        matching: Matching | None,
        errors: tuple[str, ...],
    ) -> AnalysisResult:
        return AnalysisResult(
            SemanticDecision("REVIEW", None, errors),
            extraction,
            matching,
            tuple(self._stages),
            self._calls,
            self._budget_exhausted,
        )
