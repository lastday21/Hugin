from hugin.services.semantic_results import StoredSelection, stored_decision
from hugin.services.semantic_role import ROLE_SELECTION_VERSION
from hugin.services.semantic_selection import SEMANTIC_SELECTION_VERSION, Extraction, Matching
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
