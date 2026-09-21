from hugin.services.semantic_results import StoredSelection, stored_decision
from hugin.services.semantic_role import ROLE_SELECTION_VERSION, RoleAssessment
from hugin.services.semantic_selection import SEMANTIC_SELECTION_VERSION, Extraction, Matching
from tests.unit.test_semantic_role import ANSWER, FACTS, LINES
from tests.unit.test_semantic_selection import assess, example


def test_saved_legacy_result_replays_without_becoming_a_current_assessment() -> None:
    data = example()
    lines, facts, extraction, matching = data
    stored = StoredSelection(
        extraction=Extraction.model_validate(extraction),
        matching=Matching.model_validate(matching),
        errors=[],
        stage_keys=["old-extract", "old-match"],
        model_calls=2,
        retryable=False,
    )
    assert stored_decision(lines, facts, stored, SEMANTIC_SELECTION_VERSION) == assess(data)
    assert stored_decision(lines, facts, stored, ROLE_SELECTION_VERSION).status == "REVIEW"
    assert stored_decision(lines, facts, stored, "unknown").status == "REVIEW"


def test_saved_whole_role_v2_replays_without_becoming_a_current_assessment() -> None:
    old_answer = {key: value for key, value in ANSWER.items() if key != "core_duties"}
    stored = StoredSelection(
        extraction=None,
        matching=None,
        errors=[],
        stage_keys=["old-role"],
        model_calls=1,
        retryable=False,
        assessment=RoleAssessment.model_validate(old_answer),
    )
    assert stored_decision(LINES, FACTS, stored, "whole_role_v2").status == "ALLOW"
    assert stored_decision(LINES, FACTS, stored, ROLE_SELECTION_VERSION).status == "REVIEW"


def test_saved_whole_role_v3_preserves_its_supported_daily_work() -> None:
    stored = StoredSelection(
        extraction=None,
        matching=None,
        errors=[],
        stage_keys=["old-core-role"],
        model_calls=1,
        retryable=False,
        assessment=RoleAssessment.model_validate(ANSWER),
    )
    assert stored_decision(LINES, FACTS, stored, "whole_role_v3").status == "ALLOW"


def test_saved_v3_transferable_direct_priority_uses_its_historical_contract() -> None:
    stored = StoredSelection(
        extraction=None,
        matching=None,
        errors=[],
        stage_keys=["old-core-role"],
        model_calls=1,
        retryable=False,
        assessment=RoleAssessment.model_validate(
            {
                **ANSWER,
                "core_duties": [{**ANSWER["core_duties"][0], "support": "transferable"}],
            }
        ),
    )
    assert stored_decision(LINES, FACTS, stored, "whole_role_v3").status == "ALLOW"
    assert stored_decision(LINES, FACTS, stored, ROLE_SELECTION_VERSION).status == "REVIEW"
