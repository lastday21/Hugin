from __future__ import annotations

from typing import Any

from pydantic import Field

from hugin.services.semantic_analyzer import (
    AnalysisResult,
    StageAnalyzer,
    StageCache,
    StructuredClient,
)
from hugin.services.semantic_role import (
    ROLE_BODY_FIELDS,
    ROLE_INSTRUCTIONS,
    ROLE_REPAIR_TASK,
    ROLE_REVIEW_INSTRUCTIONS,
    CoreDuty,
    ProfessionBasis,
    RoleAssessment,
    assess_role,
    role_errors,
)
from hugin.services.semantic_selection import ProfileFact, SemanticDecision, SourceLine


def assessment_type(lines: list[SourceLine], facts: list[ProfileFact]) -> type[RoleAssessment]:
    class CaseAssessment(RoleAssessment):
        profession_basis: ProfessionBasis
        core_duties: list[CoreDuty] = Field(min_length=1, max_length=12)

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            schema = super().model_json_schema(*args, **kwargs)
            properties = schema["properties"]
            properties["source_line_ids"]["items"]["enum"] = [line.id for line in lines]
            properties["profile_fact_ids"]["items"]["enum"] = [fact.id for fact in facts]
            schema["$defs"]["RoleBlocker"]["properties"]["source_line_ids"]["items"]["enum"] = [
                line.id for line in lines if line.field in ROLE_BODY_FIELDS and line.text.strip()
            ]
            duties = schema["$defs"]["CoreDuty"]["properties"]
            duties["source_line_ids"]["items"]["enum"] = [
                line.id for line in lines if line.field in ROLE_BODY_FIELDS and line.text.strip()
            ]
            duties["profile_fact_ids"]["items"]["enum"] = [fact.id for fact in facts]
            schema["$defs"]["ProfessionBasis"]["properties"]["source_line_ids"]["items"]["enum"] = [
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
        super().__init__(cache, max_calls=max_calls, force=force, retry_invalid_responses=False)
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
            assessment, errors = self._assess(
                "assess",
                ROLE_INSTRUCTIONS,
                payload,
                lines,
                facts,
            )
            if (
                assessment is not None
                and assessment.fit == "reject"
                and assessment.profession != "other_it"
            ):
                assessment, errors = self._assess(
                    "assess_review",
                    ROLE_REVIEW_INSTRUCTIONS,
                    {**payload, "proposed_assessment": assessment.model_dump()},
                    lines,
                    facts,
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

    def _assess(
        self,
        stage: str,
        instructions: str,
        payload: dict[str, object],
        lines: list[SourceLine],
        facts: list[ProfileFact],
    ) -> tuple[RoleAssessment | None, tuple[str, ...]]:
        record_type = assessment_type(lines, facts)

        def validate(answer: RoleAssessment) -> tuple[str, ...]:
            return role_errors(lines, facts, answer)

        assessment, errors = self._stage(
            stage, self._client, instructions, payload, record_type, validate
        )
        if (
            assessment is None
            and self._stages
            and self._stages[-1].stage == stage
            and self._stages[-1].response_text
        ):
            assessment, errors = self._stage(
                f"{stage}_repair",
                self._client,
                instructions,
                {
                    **payload,
                    "previous_response": self._stages[-1].response_text,
                    "validation_errors": list(errors),
                    "revision_task": ROLE_REPAIR_TASK,
                },
                record_type,
                validate,
            )
        return assessment, errors
