from __future__ import annotations

from typing import Any

from hugin.services.semantic_analyzer import (
    AnalysisResult,
    StageAnalyzer,
    StageCache,
    StructuredClient,
)
from hugin.services.semantic_role import (
    ROLE_BODY_FIELDS,
    ROLE_INSTRUCTIONS,
    RoleAssessment,
    assess_role,
    role_errors,
)
from hugin.services.semantic_selection import ProfileFact, SemanticDecision, SourceLine


def assessment_type(lines: list[SourceLine], facts: list[ProfileFact]) -> type[RoleAssessment]:
    class CaseAssessment(RoleAssessment):
        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            schema = super().model_json_schema(*args, **kwargs)
            properties = schema["properties"]
            properties["source_line_ids"]["items"]["enum"] = [line.id for line in lines]
            properties["profile_fact_ids"]["items"]["enum"] = [fact.id for fact in facts]
            schema["$defs"]["RoleBlocker"]["properties"]["source_line_ids"]["items"]["enum"] = [
                line.id for line in lines if line.field in ROLE_BODY_FIELDS and line.text.strip()
            ]
            return schema

    return CaseAssessment


class RoleAnalyzer(StageAnalyzer):
    def __init__(
        self,
        client: StructuredClient,
        cache: StageCache,
        *,
        max_calls: int = 1,
        force: bool = False,
    ) -> None:
        super().__init__(cache, max_calls=max_calls, force=force)
        self._client = client

    def analyze(self, lines: list[SourceLine], facts: list[ProfileFact]) -> AnalysisResult:
        self._calls = 0
        self._budget_exhausted = False
        self._stages = []
        assessment = None
        errors: tuple[str, ...] = ("Нет полного текста или подтверждённых сведений профиля",)
        if facts and any(line.field in ROLE_BODY_FIELDS and line.text.strip() for line in lines):
            payload: dict[str, object] = {
                "vacancy_lines": [line.model_dump() for line in lines],
                "profile": {"facts": [fact.model_dump() for fact in facts]},
            }
            assessment, errors = self._stage(
                "assess",
                self._client,
                ROLE_INSTRUCTIONS,
                payload,
                assessment_type(lines, facts),
                lambda answer: role_errors(lines, facts, answer),
            )
        decision = (
            assess_role(lines, facts, assessment)
            if assessment is not None
            else SemanticDecision("REVIEW", None, errors)
        )
        return AnalysisResult(
            decision,
            None,
            None,
            tuple(self._stages),
            self._calls,
            self._budget_exhausted,
            assessment,
        )
