from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import CandidateProfileModel, ResumeModel, VerifiedFactModel
from hugin.domain.applications import ApplicationEventType, ApplicationState
from hugin.domain.content import ConfirmationState
from hugin.domain.directions import VacancyState
from hugin.domain.hh import HhApplyResult, HhApplyStatus
from hugin.domain.tasks import SystemState, TaskState
from hugin.domain.vacancies import VacancyData
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    QueueTaskRepository,
    ResumeRepository,
    SystemStateRepository,
    VacancyRepository,
)
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.application_exposure import application_profile_snapshot
from hugin.services.vacancy_analysis import RULES_VERSION

pytestmark = pytest.mark.integration


def _fact(
    session: Session,
    profile_id: int,
    content: str,
    *,
    resume_id: int | None = None,
    direction_id: int | None = None,
    state: ConfirmationState = ConfirmationState.CONFIRMED,
) -> VerifiedFactModel:
    fact = VerifiedFactModel(
        profile_id=profile_id,
        category="project",
        content=content,
        source_type="manual",
        resume_id=resume_id,
        direction_id=direction_id,
        state=state,
    )
    session.add(fact)
    session.flush()
    return fact


@pytest.mark.parametrize("recovery", [None, "startup", "supervised_lease_expired"])
def test_profile_snapshot_is_scoped_versioned_and_preserved_through_unknown_result(
    settings: Settings,
    recovery: str | None,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Candidate")
            other = AccountRepository(session).create("Other")
            resumes = ResumeRepository(session)
            resume = resumes.upsert(account.id, "resume", "Python")
            second_resume = resumes.upsert(account.id, "other-resume", "Other")
            model = session.get(ResumeModel, resume.id)
            assert model is not None
            model.content_text = "Confirmed local resume"
            profile = CandidateProfileModel(account_id=account.id, display_name="Candidate")
            other_profile = CandidateProfileModel(account_id=other.id, display_name="Other")
            session.add_all([profile, other_profile])
            session.flush()
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            other_direction = directions.create(account.id, "Other direction")
            directions.attach_resume(direction.id, resume.id)
            global_fact = _fact(session, profile.id, "Shared confirmed fact")
            _fact(session, profile.id, "Selected resume fact", resume_id=resume.id)
            _fact(session, profile.id, "Another resume fact", resume_id=second_resume.id)
            _fact(session, profile.id, "Another direction fact", direction_id=other_direction.id)
            _fact(session, profile.id, "Pending fact", state=ConfirmationState.PENDING)
            _fact(session, other_profile.id, "Other account fact")
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "exposure",
                    "Python developer",
                    "https://hh.ru/vacancy/exposure",
                    description="Original requirements",
                    details_fetched_at=datetime.now(UTC),
                    region="Russia",
                    work_format="remote",
                )
            )
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.QUEUED,
                score=90,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            app = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(app.id, 90)
            SystemStateRepository(session).transition(SystemState.RUNNING)
            service = ApplicationAutomationService(session)
            job = service.claim_next(direction.id)
            assert job is not None and job.profile_snapshot is not None
            original = deepcopy(job.profile_snapshot)
            facts = original["profile_facts"]
            assert isinstance(facts, list)
            assert {item["content"] for item in facts} == {
                "Shared confirmed fact",
                "Selected resume fact",
            }
            global_fact.content = "Changed after claim"
            model.content_text = "Changed local resume"
            session.flush()
            changed = application_profile_snapshot(session, app)
            assert changed["resume_content_sha256"] != original["resume_content_sha256"]
            assert changed["profile_facts_sha256"] != original["profile_facts_sha256"]
            if recovery is None:
                service.record_result(
                    job,
                    HhApplyResult(HhApplyStatus.UNKNOWN_RESULT, vacancy.source_url),
                )
            else:
                QueueTaskRepository(session).recover_running(recovery=recovery)
            assert QueueTaskRepository(session).get(job.task.id).state is TaskState.UNKNOWN_RESULT
            repository = ApplicationRepository(session)
            unknown = repository.list_events(app.id)[-1]
            assert unknown.event_type is ApplicationEventType.UNKNOWN_RESULT
            if recovery is None:
                context = unknown.payload["selection_snapshot"]
            else:
                assert "selection_snapshot" not in unknown.payload
                attempt = next(
                    event
                    for event in reversed(repository.list_events(app.id))
                    if event.payload.get("source") == "hugin_attempt"
                )
                context = attempt.payload["selection_snapshot"]
            assert isinstance(context, dict)
            assert context["outcome_context"]["profile"] == original
            repository.transition_state(
                app.id,
                ApplicationState.APPLIED,
                {"source": "hugin_reconciliation", "hh_status": "APPLIED", "task_id": job.task.id},
            )
            saved = repository.list_events(app.id)[-1].payload["outcome_context"]
            assert isinstance(saved, dict)
            assert saved["profile"] == original
            assert saved["profile"]["resume_content"] == "Confirmed local resume"
            assert saved["vacancy"]["region"] == "Russia"
            assert saved["vacancy"]["description"] == "Original requirements"
            assert datetime.fromisoformat(str(original["captured_at"])) <= datetime.now(UTC)
    finally:
        database.close()
