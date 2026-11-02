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
    CareerDirectionModel,
    DirectionVacancyModel,
    RecruiterMessageModel,
    ResumeModel,
    SemanticStageModel,
    VacancyModel,
)
from hugin.domain.applications import ApplicationEventType
from hugin.domain.vacancies import VacancyData
from hugin.repositories import ApplicationRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.background_processes import BackgroundProcessService
from hugin.services.screening_forms import ScreeningDraftService
from hugin.services.search_outcomes import SearchOutcomeService
from hugin.services.ui_communications import UiCommunicationService
from hugin.services.ui_workspace import UiWorkspaceService
from tests.unit.test_workspace_api import seed_workspace

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "section", ["processes", "queue", "sent", "rejected", "communications", "outcomes", "forms"]
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
            if section == "processes":
                direction = session.scalar(
                    select(CareerDirectionModel).where(CareerDirectionModel.account_id == account)
                )
                assert direction is not None
                direction.scoring_config = {
                    **direction.scoring_config,
                    "historical_notes": "saved config " * 8_000,
                }
            application_vacancy = session.get(VacancyModel, application.vacancy_id)
            assert application_vacancy is not None
            application_vacancy.description = "Unused description " * 100_000
            if section == "forms":
                resume = session.get(ResumeModel, application.resume_id)
                assert resume is not None
                resume.content_text = "Unused source resume " * 100_000
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
            if section == "outcomes":
                future_vacancy = VacancyRepository(session).upsert(
                    VacancyData("future-summary", "Future", "https://hh.ru/vacancy/future-summary")
                )
                future_application = ApplicationRepository(session).create_apply_intent(
                    account, future_vacancy.id, application.resume_id
                )
                session.add_all(
                    [
                        ApplicationEventModel(
                            application_id=future_application.id,
                            event_type=ApplicationEventType.UNKNOWN_RESULT,
                            payload={"unused_evidence": "Unconfirmed source " * 100_000 + "\x00"},
                        ),
                        ApplicationEventModel(
                            application_id=future_application.id,
                            event_type=ApplicationEventType.APPLIED,
                            created_at=datetime.now(UTC) + timedelta(days=1),
                            payload={"source": "hugin_send", "hh_status": "APPLIED"},
                        ),
                    ]
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
                if section == "forms":
                    return ScreeningDraftService(session).list_pending(account)
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
        elif section == "forms":
            assert before[0].vacancy_title != after[0].vacancy_title == "Updated queued vacancy"
            assert before[0].questions == after[0].questions
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
    ("content", "unused", "context_shape"),
    [
        ("Confirmed " * 200_000, "safe", "object"),
        ("old source\x00tail", "safe", "object"),
        ("\ud800", "safe", "object"),
        ("", "safe", "object"),
        ("Confirmed", "old source\x00tail", "object"),
        (["historical content"], "safe", "object"),
        ("Confirmed", "safe", "scalar"),
        ("Confirmed", "safe", "profile-list"),
        ("Confirmed", "safe", "boolean-version"),
    ],
    ids=[
        "large",
        "nul",
        "surrogate",
        "empty",
        "legacy-outside-context",
        "non-string-content",
        "scalar-context",
        "profile-list",
        "boolean-version",
    ],
)
def test_outcome_summary_handles_large_and_legacy_json_content(
    settings: Settings, content: Any, unused: str, context_shape: str
) -> None:
    account, _, _ = seed_workspace(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            application = session.scalar(select(ApplicationModel).order_by(ApplicationModel.id))
            assert application is not None
            context: Any = {
                "schema_version": True if context_shape == "boolean-version" else 1,
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
            }
            if context_shape == "scalar":
                context = "historical malformed context"
            elif context_shape == "profile-list":
                context["profile"] = [context["profile"]]
            session.add(
                ApplicationEventModel(
                    application_id=application.id,
                    event_type=ApplicationEventType.APPLIED,
                    created_at=datetime.now(UTC) - timedelta(days=30),
                    payload={
                        "source": "hugin_send",
                        "hh_status": "APPLIED",
                        "rules_version": "test",
                        "unused_evidence": unused,
                        "outcome_context": context,
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
            assert version.with_outcome_context == int(
                bool(content) and context_shape in {"object", "boolean-version"}
            )
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


@pytest.mark.parametrize("history_case", ["unambiguous", "compact", "legacy"])
def test_process_summary_reads_only_history_needed_for_source_numbering(
    settings: Settings,
    history_case: str,
) -> None:
    from hugin.repositories.vacancies import _to_record
    from hugin.services.decision_evidence import fingerprint
    from hugin.services.semantic_processing import SemanticSelectionProcessor
    from hugin.services.semantic_snapshot import source_lines
    from tests.unit.test_semantic_processing import Client, seed

    account, direction, vacancy, _, _ = seed(settings)
    if history_case != "unambiguous":
        setup_database = create_database(settings)
        try:
            with setup_database.sessions.begin() as session:
                row = session.get(VacancyModel, vacancy)
                assert row is not None
                row.responsibilities = row.description
        finally:
            setup_database.close()
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: Client())
    assert processor.process(account, direction, vacancy).applied
    database = create_database(settings)
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

    try:
        with database.sessions.begin() as session:
            request: dict[str, object] = {"unused_evidence": "old numbering " * 200_000}
            if history_case == "legacy":
                current_vacancy = session.get(VacancyModel, vacancy)
                assert current_vacancy is not None
                request = {
                    "payload": {
                        "vacancy_lines": [
                            line.model_dump()
                            for line in source_lines(
                                _to_record(current_vacancy), include_repeated_fields=True
                            )
                        ]
                    }
                }
            session.add(
                SemanticStageModel(
                    account_id=account,
                    vacancy_id=vacancy,
                    stage="assess",
                    cache_key=fingerprint(request),
                    model="historical",
                    request=request,
                    response_text="{}",
                    response_sha256=fingerprint("{}"),
                    errors=[],
                    created_at=datetime.now(UTC),
                    duration_seconds=1,
                )
            )
        event.listen(database.engine, "after_cursor_execute", capture)
        with database.sessions() as session:
            result = BackgroundProcessService(session, account).snapshot()
            funnel = cast(dict[str, Any], result["funnel"])
            counts = {stage["key"]: stage["count"] for stage in funnel["stages"]}
            assert counts["ready"] == int(history_case != "legacy")
            if history_case == "legacy":
                assert counts["awaiting_evaluation"] == 1
            assert received < 150_000, f"Process summary transferred {received} bytes"
    finally:
        database.close()
