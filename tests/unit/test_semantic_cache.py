from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import SemanticStageModel
from hugin.domain.vacancies import VacancyData
from hugin.repositories import AccountRepository, VacancyRepository
from hugin.services.decision_evidence import fingerprint
from hugin.services.semantic_analyzer import StageRecord
from hugin.services.semantic_cache import DatabaseStageCache

pytestmark = pytest.mark.integration


def test_stage_cache_survives_reopen_reuses_identical_input_and_keeps_account_boundary(
    settings: Settings,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Первый")
            other_account = AccountRepository(session).create("Второй")
            repository = VacancyRepository(session)
            first = repository.upsert(VacancyData("first", "Python", "https://hh.ru/vacancy/first"))
            second = repository.upsert(
                VacancyData("second", "Python", "https://hh.ru/vacancy/second")
            )
        request: dict[str, object] = {"model": "example:medium", "payload": {"text": "Python"}}
        response = '{"entries": []}'
        record = StageRecord(
            fingerprint(request),
            "extract",
            "example:medium",
            request,
            response,
            fingerprint(response),
            (),
            datetime.now(UTC),
            1.5,
        )
        DatabaseStageCache(settings, account.id, first.id).put(record)
        reopened = DatabaseStageCache(settings, account.id, first.id)
        assert reopened.get(record.cache_key) == record
        assert (
            DatabaseStageCache(settings, other_account.id, first.id).get(record.cache_key) is None
        )
        reused = DatabaseStageCache(settings, account.id, second.id).get(record.cache_key)
        assert reused is not None
        assert reused.request == record.request
        assert reused.response_text == record.response_text
        assert reused.duration_seconds == 0
        assert DatabaseStageCache(settings, account.id, second.id).get(record.cache_key) == reused
        reopened.put(record)
        with database.sessions.begin() as session:
            assert session.scalar(select(func.count()).select_from(SemanticStageModel)) == 3
    finally:
        database.close()


@pytest.mark.parametrize("invalid", ["request", "response", "errors"])
def test_cross_publication_cache_rejects_damaged_or_failed_evidence(
    settings: Settings, invalid: str
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Контроль")
            repository = VacancyRepository(session)
            first = repository.upsert(VacancyData("first", "Python", "https://hh.ru/vacancy/first"))
            second = repository.upsert(
                VacancyData("second", "Python", "https://hh.ru/vacancy/second")
            )
        request: dict[str, object] = {"payload": "unchanged"}
        response = "{}"
        record = StageRecord(
            fingerprint(request),
            "assess",
            "model",
            request,
            response,
            fingerprint(response),
            (),
            datetime.now(UTC),
            1,
        )
        if invalid == "request":
            record = replace(record, request={"payload": "changed"})
        elif invalid == "response":
            record = replace(record, response_text='{"changed":true}')
        else:
            record = replace(record, errors=("invalid response",))
        DatabaseStageCache(settings, account.id, first.id).put(record)
        assert DatabaseStageCache(settings, account.id, second.id).get(record.cache_key) is None
    finally:
        database.close()
