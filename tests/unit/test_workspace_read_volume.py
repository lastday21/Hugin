from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import event, select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    DirectionVacancyModel,
    RecruiterMessageModel,
    SemanticStageModel,
    VacancyModel,
)
from hugin.domain.applications import ApplicationEventType
from hugin.services.background_processes import BackgroundProcessService
from hugin.services.search_outcomes import SearchOutcomeService
from hugin.services.ui_communications import UiCommunicationService
from hugin.services.ui_workspace import UiWorkspaceService
from tests.unit.test_workspace_api import seed_workspace

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "section", ["processes", "queue", "sent", "rejected", "communications", "outcomes"]
)
def test_summary_reads_skip_large_unused_evidence_and_keep_updates(
    settings: Settings, section: str
) -> None:
    account, _, _ = seed_workspace(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            vacancy = session.scalar(select(VacancyModel).where(VacancyModel.hh_id == "ui-202"))
            application = session.scalar(select(ApplicationModel).order_by(ApplicationModel.id))
            assert vacancy is not None and application is not None
            application_id = application.id
            application_vacancy = session.get(VacancyModel, application.vacancy_id)
            assert application_vacancy is not None
            application_vacancy.description = "Unused description " * 100_000
            vacancy.description = "Unused description " * 100_000
            tracking = session.scalar(
                select(DirectionVacancyModel).where(DirectionVacancyModel.vacancy_id == vacancy.id)
            )
            assert tracking is not None
            tracking.rules_details = {
                **tracking.rules_details,
                "unused_evidence": "historical response " * 100_000,
            }
            session.add(
                ApplicationEventModel(
                    application_id=application_id,
                    event_type=ApplicationEventType.STATE_CHANGED,
                    payload={"state": "INVITED", "unused_evidence": "old source " * 200_000},
                )
            )
            session.add(
                ApplicationEventModel(
                    application_id=application_id,
                    event_type=ApplicationEventType.APPLIED,
                    created_at=datetime.now(UTC) - timedelta(days=30),
                    payload={"source": "hugin_send", "hh_status": "APPLIED"},
                )
            )
            session.add(
                SemanticStageModel(
                    account_id=account,
                    vacancy_id=vacancy.id,
                    stage="extract",
                    cache_key="a" * 64,
                    model="historical",
                    request={"unused": "old request " * 200_000},
                    response_text="{}",
                    response_sha256="b" * 64,
                    errors=[],
                    created_at=datetime.now(UTC),
                    duration_seconds=1,
                )
            )

        received = 0

        def capture(_connection: Any, cursor: Any, *_args: Any) -> None:
            nonlocal received
            result = cursor.pgresult
            if result is not None:
                received += sum(
                    len(result.get_value(row, column) or b"")
                    for row in range(result.ntuples)
                    for column in range(result.nfields)
                )

        event.listen(database.engine, "after_cursor_execute", capture)

        def read() -> Any:
            with database.sessions() as session:
                if section == "processes":
                    return BackgroundProcessService(session, account).snapshot()
                if section == "rejected":
                    return UiWorkspaceService(session).rejected(account)
                if section == "queue":
                    return UiWorkspaceService(session).queue(account)
                if section == "sent":
                    return UiWorkspaceService(session).sent(account)
                if section == "communications":
                    return UiCommunicationService(session).get(account)
                return SearchOutcomeService(session).snapshot(account)

        before = read()
        assert received < 150_000, f"Summary read transferred {received} bytes"
        event.remove(database.engine, "after_cursor_execute", capture)
        with database.sessions.begin() as session:
            vacancy = session.scalar(select(VacancyModel).where(VacancyModel.hh_id == "ui-202"))
            assert vacancy is not None
            vacancy.title = "Updated vacancy"
            application_vacancy = session.get(VacancyModel, application.vacancy_id)
            assert application_vacancy is not None
            application_vacancy.title = "Updated queued vacancy"
            message = session.scalar(
                select(RecruiterMessageModel).order_by(RecruiterMessageModel.id)
            )
            assert message is not None
            message.body = "Updated message"
            session.add(
                ApplicationEventModel(
                    application_id=application_id,
                    event_type=ApplicationEventType.STATE_CHANGED,
                    payload={"state": "REJECTED"},
                )
            )
            tracking = session.scalar(
                select(DirectionVacancyModel).where(DirectionVacancyModel.vacancy_id == vacancy.id)
            )
            assert tracking is not None
            tracking.rules_details = {"category": "MATCH"}
        after = read()
        if section == "rejected":
            assert before[0].title != after[0].title == "Updated vacancy"
        elif section == "communications":
            assert after.conversations[0].messages[0].body == "Updated message"
        elif section == "outcomes":
            assert after.cohorts[0].rejections > before.cohorts[0].rejections
        elif section == "queue":
            assert after[0].title == "Updated queued vacancy"
        elif section == "sent":
            sent = next(item for item in after if item.application_id == application_id)
            assert sent.title == "Updated queued vacancy"
        else:
            assert after["funnel"]["total"] == before["funnel"]["total"]
    finally:
        database.close()


@pytest.mark.parametrize(
    "content",
    ["Confirmed " * 200_000, "old source\x00tail", "\ud800", ""],
    ids=["large", "nul", "surrogate", "empty"],
)
def test_outcome_summary_handles_large_and_legacy_json_content(
    settings: Settings, content: str
) -> None:
    account, _, _ = seed_workspace(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            application = session.scalar(select(ApplicationModel).order_by(ApplicationModel.id))
            assert application is not None
            session.add(
                ApplicationEventModel(
                    application_id=application.id,
                    event_type=ApplicationEventType.APPLIED,
                    created_at=datetime.now(UTC) - timedelta(days=30),
                    payload={
                        "source": "hugin_send",
                        "hh_status": "APPLIED",
                        "rules_version": "test",
                        "unused_evidence": "old source\x00tail",
                        "outcome_context": {
                            "schema_version": 1,
                            "letter_sha256": "letter",
                            "profile": {
                                "resume_content": content,
                                "resume_content_sha256": "resume",
                                "profile_facts_sha256": "facts",
                            },
                            "vacancy": {
                                "description": content,
                                "description_sha256": "vacancy",
                                "details_fetched_at": "2026-09-01",
                            },
                        },
                    },
                )
            )
        with database.sessions() as session:
            result = SearchOutcomeService(session).snapshot(account)
            version = next(
                item
                for item in result.versions
                if item.rules_version == "test" and item.age_days == 14
            )
            assert version.with_outcome_context == int(bool(content))
    finally:
        database.close()


@pytest.mark.parametrize("damage", ["version", "stage"])
def test_latest_broken_selection_does_not_reveal_an_older_allow(
    settings: Settings, damage: str
) -> None:
    from hugin.services.semantic_processing import SemanticSelectionProcessor
    from tests.unit.test_semantic_processing import Client, seed

    account, direction, vacancy, _, _ = seed(settings)
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: Client())
    assert processor.process(account, direction, vacancy).applied
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            stage = session.scalar(
                select(SemanticStageModel).where(SemanticStageModel.stage == "selection")
            )
            assert stage is not None
            session.add(
                SemanticStageModel(
                    account_id=stage.account_id,
                    vacancy_id=stage.vacancy_id,
                    cache_key=stage.cache_key,
                    stage="broken" if damage == "stage" else stage.stage,
                    model=stage.model,
                    request={**stage.request, "version": "broken"}
                    if damage == "version"
                    else stage.request,
                    response_text=stage.response_text,
                    response_sha256=stage.response_sha256,
                    errors=[],
                    created_at=datetime.now(UTC),
                    duration_seconds=1,
                )
            )
        with database.sessions() as session:
            result = cast(dict[str, Any], BackgroundProcessService(session, account).snapshot())
            counts = {item["key"]: item["count"] for item in result["funnel"]["stages"]}
            assert counts["ready"] == 0 and counts["awaiting_evaluation"] == 1
    finally:
        database.close()
