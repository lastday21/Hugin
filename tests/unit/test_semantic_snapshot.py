from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from hugin.domain.directions import DirectionRecord
from hugin.domain.vacancies import VacancyData
from hugin.services.semantic_snapshot import selection_config, source_lines


def direction(config: dict[str, object]) -> DirectionRecord:
    now = datetime.now(UTC)
    return DirectionRecord(1, 1, "Python backend", None, config, True, now, now)


def test_selection_is_opt_in_and_invalid_configuration_cannot_fall_back() -> None:
    assert selection_config(direction({})) is None
    assert selection_config(direction({"semantic_selection": {"enabled": False}})) is None
    config = selection_config(direction({"semantic_selection": {"enabled": True}}))
    assert config is not None
    assert config.extraction_model == "gpt-5.6-luna"
    assert config.matching_model == "gpt-5.6-sol"
    assert config.timeout_seconds == 300
    with pytest.raises(ValueError):
        selection_config(direction({"semantic_selection": "yes"}))
    with pytest.raises(ValueError):
        selection_config(direction({"semantic_selection": {"enabled": "true"}}))


def test_source_preserves_every_field_and_does_not_depend_on_fetch_time() -> None:
    vacancy = VacancyData(
        "1",
        "Python",
        "https://hh.ru/vacancy/1",
        description="First\n\nLast",
        responsibilities="Build",
        required_qualifications="SQL",
        preferred_qualifications="Docker",
        key_skills=("Python", "PostgreSQL"),
    )
    lines = source_lines(vacancy)
    assert [line.id for line in lines] == list(range(len(lines)))
    assert [line.text for line in lines] == [
        "Python",
        "First",
        "Last",
        "Build",
        "SQL",
        "Docker",
        "Python",
        "PostgreSQL",
    ]
    assert {line.field for line in lines} == {
        "title",
        "description",
        "responsibilities",
        "required_qualifications",
        "preferred_qualifications",
        "key_skills",
    }
    assert source_lines(replace(vacancy, details_fetched_at=datetime.now(UTC))) == lines
