from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import CandidateProfileModel, VerifiedFactModel
from hugin.domain.content import ConfirmationState
from hugin.domain.vacancies import VacancyData
from hugin.repositories.directions import AccountRepository, DirectionRepository, ResumeRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.semantic_processing import SemanticSelectionProcessor
from hugin.services.semantic_snapshot import selection_snapshot
from hugin.services.vacancy_analysis import VacancyAnalysisService

pytestmark = pytest.mark.integration


def seed(settings: Settings) -> tuple[int, int, int, int, int]:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Test", "semantic-account")
            directions = DirectionRepository(session)
            direction = directions.create(
                account.id,
                "Python backend",
                scoring_config={
                    "semantic_selection": {"enabled": True},
                },
            )
            resume = ResumeRepository(session).upsert(account.id, "resume-python", "Python")
            directions.attach_resume(direction.id, resume.id)
            profile = CandidateProfileModel(
                account_id=account.id, active_resume_id=resume.id, display_name="Test"
            )
            session.add(profile)
            session.flush()
            fact = VerifiedFactModel(
                profile_id=profile.id,
                category="project",
                source_type="user",
                content="Создал API на Python",
                resume_id=resume.id,
                state=ConfirmationState.CONFIRMED,
            )
            session.add(fact)
            session.flush()
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "semantic-vacancy",
                    "Разработчик",
                    "https://hh.ru/vacancy/semantic-vacancy",
                    description="Создавать API на Python",
                    details_fetched_at=datetime.now(UTC),
                )
            )
            directions.track_vacancy(direction.id, vacancy.id)
            return account.id, direction.id, vacancy.id, resume.id, fact.id
    finally:
        database.close()


class Client:
    request_identity = "test:model"

    def __init__(self, callback: Callable[[], None] | None = None) -> None:
        self.calls = 0
        self.callback = callback

    def complete_json(self, system: str, user: str, schema: dict[str, object]) -> str:
        self.calls += 1
        payload = json.loads(user)
        if "extraction" not in payload:
            return json.dumps(
                {
                    "scopes": [
                        {
                            "id": "common",
                            "label": "Общие",
                            "alternative_group": "",
                            "source_lines": [],
                        }
                    ],
                    "entries": [
                        {
                            "line": 1,
                            "subject": "Python API",
                            "scope": "common",
                            "kind": "duty",
                            "activity": "development",
                            "level": "working",
                            "relation": "all",
                            "terms": ["Python"],
                        }
                    ],
                    "excluded_lines": [{"line": 0, "reason": "heading"}],
                }
            )
        if self.callback:
            self.callback()
        return json.dumps(
            {
                "source_issues": [],
                "matches": [
                    {
                        "entry_id": 0,
                        "status": "confirmed",
                        "profile_fact_ids": [payload["profile"]["facts"][0]["id"]],
                        "gap": "none",
                        "reason": "Подтверждено проектом",
                    }
                ],
                "paths": [
                    {
                        "scope": "common",
                        "profession": "applied_python",
                        "core_entry_ids": [0],
                        "reason": "Python API",
                    }
                ],
            }
        )


def edit_fact(settings: Settings, fact_id: int) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            fact = session.get(VerifiedFactModel, fact_id)
            assert fact is not None
            fact.content = "Дополнительный подтверждённый проект Python"
    finally:
        database.close()


def test_pending_then_allowed_and_cached_with_profile_change_guard(settings: Settings) -> None:
    account_id, direction_id, vacancy_id, resume_id, fact_id = seed(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            initial = VacancyAnalysisService(session).reanalyze_one(
                account_id, direction_id, vacancy_id
            )
            assert initial.evaluation.category.value == "REVIEW"
        client = Client()
        processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
        first = processor.process(account_id, direction_id, vacancy_id)
        assert first.applied and first.status == "MATCH" and first.model_calls == 2
        second = processor.process(account_id, direction_id, vacancy_id)
        assert second.applied and second.model_calls == 0 and client.calls == 2
        with database.sessions.begin() as session:
            from sqlalchemy import select

            from hugin.database.models import VacancyChangeModel
            from hugin.services.decision_evidence import replay_ranking

            events = list(
                session.scalars(
                    select(VacancyChangeModel).where(
                        VacancyChangeModel.vacancy_id == vacancy_id,
                        VacancyChangeModel.event_type == "RULES_EVALUATED",
                    )
                )
            )
            assert len(events) == 2
            assert all(replay_ranking(event.changes)["matches"] for event in events)
            direction = DirectionRepository(session).get_for_account(account_id, direction_id)
            vacancy = VacancyRepository(session).get(vacancy_id)
            assert ApplicationAutomationService(session)._semantic_selection_current(
                direction, vacancy, resume_id
            )
        edit_fact(settings, fact_id)
        with database.sessions.begin() as session:
            assert not ApplicationAutomationService(session)._semantic_selection_current(
                direction, vacancy, resume_id
            )
        third = processor.process(account_id, direction_id, vacancy_id)
        assert third.model_calls == 1 and third.key != first.key
    finally:
        database.close()


def test_profile_changed_during_model_call_keeps_result_unapplied(settings: Settings) -> None:
    account_id, direction_id, vacancy_id, _resume_id, fact_id = seed(settings)
    client = Client(lambda: edit_fact(settings, fact_id))
    result = SemanticSelectionProcessor(settings, client_factory=lambda *_: client).process(
        account_id,
        direction_id,
        vacancy_id,
    )
    assert result.status == "STALE" and not result.applied


@pytest.mark.parametrize("uncertainty", ["requirement", "profession", "source"])
def test_uncertain_fit_creates_queue_task_and_replays_without_new_model_call(
    settings: Settings, uncertainty: str
) -> None:
    from sqlalchemy import select

    from hugin.database.models import ApplicationModel, VacancyChangeModel
    from hugin.domain.vacancy_priority import FitTier
    from hugin.repositories.tasks import QueueTaskRepository
    from hugin.services.decision_evidence import replay_ranking

    class UncertainClient(Client):
        def complete_json(self, system: str, user: str, schema: dict[str, object]) -> str:
            result = json.loads(super().complete_json(system, user, schema))
            if "matches" in result:
                if uncertainty == "requirement":
                    result["matches"][0].update(
                        status="unconfirmed",
                        profile_fact_ids=[],
                        gap="unclear",
                        reason="Неясен требуемый уровень работы с API",
                    )
                elif uncertainty == "profession":
                    result["paths"][0].update(profession="unclear")
                else:
                    result["source_issues"] = [{"line": 1, "issue": "Неясен уровень требования"}]
            return json.dumps(result)

    account_id, direction_id, vacancy_id, _, _ = seed(settings)
    client = UncertainClient()
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    result = processor.process(account_id, direction_id, vacancy_id)
    assert result.status == "STRETCH" and result.applied
    assert processor.process(account_id, direction_id, vacancy_id).model_calls == 0
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            prepared = ApplicationAutomationService(session).prepare_for_account_id(
                account_id=account_id, direction_name="Python backend", include_stretch=True
            )
            assert prepared.created == 1
            application = session.scalar(select(ApplicationModel))
            assert application is not None
            task = QueueTaskRepository(session).get_by_application_id(application.id)
            assert task is not None
            tracked = DirectionRepository(session).get_tracked_vacancy(direction_id, vacancy_id)
            assert tracked.rules_details["fit_tier"] == FitTier.POSSIBLE
            events = list(
                session.scalars(
                    select(VacancyChangeModel).where(
                        VacancyChangeModel.event_type == "RULES_EVALUATED"
                    )
                )
            )
            assert events and all(replay_ranking(event.changes)["matches"] for event in events)
    finally:
        database.close()


def test_snapshot_uses_direction_resume_and_full_confirmed_professional_facts(
    settings: Settings,
) -> None:
    account_id, direction_id, vacancy_id, resume_id, fact_id = seed(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            other_resume = ResumeRepository(session).upsert(account_id, "unselected", "Other")
            fact = session.get(VerifiedFactModel, fact_id)
            assert fact is not None
            profile = session.get(CandidateProfileModel, fact.profile_id)
            assert profile is not None
            profile.active_resume_id = other_resume.id
            fact.content = "Полный профессиональный факт " * 1000
            for category, state, selected_resume in [
                ("skills", ConfirmationState.PENDING, resume_id),
                ("project", ConfirmationState.CONFIRMED, other_resume.id),
                ("contact", ConfirmationState.CONFIRMED, resume_id),
            ]:
                session.add(
                    VerifiedFactModel(
                        profile_id=profile.id,
                        category=category,
                        content="Не должно попасть в разбор",
                        source_type="user",
                        state=state,
                        resume_id=selected_resume,
                    )
                )
            session.flush()
            snapshot = selection_snapshot(
                session,
                DirectionRepository(session).get_for_account(account_id, direction_id),
                VacancyRepository(session).get(vacancy_id),
            )
            assert snapshot is not None and snapshot.resume_id == resume_id
            assert [item.id for item in snapshot.facts] == [fact_id]
            assert snapshot.facts[0].content == fact.content
    finally:
        database.close()


def test_worker_obeys_search_switch_and_does_not_repeat_completed_result(
    settings: Settings,
) -> None:
    from hugin.database.models import ApplicationSettingsModel, SystemStateModel
    from hugin.domain.tasks import SystemState
    from hugin.workers.semantic_selection import SemanticSelectionWorker

    account_id, _direction_id, _vacancy_id, _resume_id, _fact_id = seed(settings)
    client = Client()
    worker = SemanticSelectionWorker(
        settings,
        account_id=account_id,
        processor=SemanticSelectionProcessor(settings, client_factory=lambda *_: client),
    )
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            options = session.get(ApplicationSettingsModel, 1)
            assert options is not None
            options.search_enabled = False
        assert not worker.run_once() and client.calls == 0
        with database.sessions.begin() as session:
            options = session.get(ApplicationSettingsModel, 1)
            assert options is not None
            options.search_enabled = True
            state = session.get(SystemStateModel, 1)
            assert state is not None
            state.state = SystemState.CAPTCHA_REQUIRED
        assert not worker.run_once() and client.calls == 0
        with database.sessions.begin() as session:
            state = session.get(SystemStateModel, 1)
            assert state is not None
            state.state = SystemState.PAUSED
        assert worker.run_once()
        assert client.calls == 2
        assert not worker.run_once()
    finally:
        worker.stop()
        database.close()


def test_new_allow_recovers_only_stale_selection_task_and_preserves_letter(
    settings: Settings,
) -> None:
    from sqlalchemy import select

    from hugin.database.models import ApplicationModel, CoverLetterModel
    from hugin.domain.content import CoverLetterState
    from hugin.domain.tasks import TaskState
    from hugin.repositories.applications import ApplicationRepository
    from hugin.repositories.tasks import QueueTaskRepository
    from hugin.services.application_automation import ApplyJob

    account_id, direction_id, vacancy_id, resume_id, fact_id = seed(settings)
    client = Client()
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    assert processor.process(account_id, direction_id, vacancy_id).applied
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            service = ApplicationAutomationService(session)
            prepared = service.prepare_for_account_id(
                account_id=account_id, direction_name="Python backend", include_stretch=True
            )
            assert prepared.created == 1
            model = session.scalar(
                select(ApplicationModel).where(ApplicationModel.vacancy_id == vacancy_id)
            )
            assert model is not None
            application = ApplicationRepository(session).get(model.id)
            task = QueueTaskRepository(session).get_by_application_id(application.id)
            assert task is not None
            task = QueueTaskRepository(session).claim_exact(task.id)
            assert task is not None
            letter = CoverLetterModel(
                application_id=application.id,
                vacancy_id=vacancy_id,
                direction_id=direction_id,
                resume_id=resume_id,
                instruction_version="test",
                text="Мой сохранённый текст письма",
                model_name="manual",
                state=CoverLetterState.READY,
            )
            session.add(letter)
            session.flush()
            letter_id = letter.id
            job = ApplyJob(
                task=task,
                application=application,
                vacancy=VacancyRepository(session).get(vacancy_id),
                resume=ResumeRepository(session).get(resume_id),
                direction_vacancy=DirectionRepository(session).get_tracked_vacancy(
                    direction_id, vacancy_id
                ),
            )
        edit_fact(settings, fact_id)
        with database.sessions.begin() as session:
            assert ApplicationAutomationService(session)._save_attempt(job) is None
            stopped = QueueTaskRepository(session).get(task.id)
            assert stopped.state is TaskState.REVIEW_REQUIRED
            assert stopped.last_error_code == "SEMANTIC_SELECTION_STALE"
        assert processor.process(account_id, direction_id, vacancy_id).applied
        with database.sessions.begin() as session:
            service = ApplicationAutomationService(session)
            recovered = service.prepare_for_account_id(
                account_id=account_id, direction_name="Python backend", include_stretch=True
            )
            assert recovered.created == 1
            restored = QueueTaskRepository(session).get(task.id)
            assert restored.state is TaskState.PENDING
            assert restored.last_error_code is None
            saved_letter = session.get(CoverLetterModel, letter_id)
            assert saved_letter is not None and saved_letter.text == "Мой сохранённый текст письма"
            assert saved_letter.state is CoverLetterState.READY
            QueueTaskRepository(session).transition(task.id, TaskState.RUNNING)
            QueueTaskRepository(session).transition(
                task.id, TaskState.REVIEW_REQUIRED, error_code="MANUAL_REVIEW_REQUIRED"
            )
            service.prepare_for_account_id(
                account_id=account_id, direction_name="Python backend", include_stretch=True
            )
            assert QueueTaskRepository(session).get(task.id).state is TaskState.REVIEW_REQUIRED
    finally:
        database.close()


@pytest.mark.parametrize("target_enabled", [False, True])
def test_adjacent_role_keeps_semantic_selection_across_directions(
    settings: Settings, target_enabled: bool
) -> None:
    from sqlalchemy import select

    from hugin.database.models import VacancyChangeModel
    from hugin.services.decision_evidence import replay_ranking

    account_id, direction_id, vacancy_id, resume_id, _ = seed(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            directions = DirectionRepository(session)
            target = directions.create(
                account_id,
                "Смежные ИТ-роли",
                scoring_config={
                    "role_scope": "IT_ADJACENT",
                    "semantic_selection": {"enabled": target_enabled},
                },
            )
            directions.attach_resume(target.id, resume_id)

        class AdjacentClient(Client):
            def complete_json(self, system: str, user: str, schema: dict[str, object]) -> str:
                return (
                    super()
                    .complete_json(system, user, schema)
                    .replace('"applied_python"', '"adjacent_it"')
                )

        client = AdjacentClient()
        result = SemanticSelectionProcessor(settings, client_factory=lambda *_: client).process(
            account_id, direction_id, vacancy_id
        )
        assert result.status == ("ROUTED" if target_enabled else "STRETCH")
        with database.sessions.begin() as session:
            events = list(
                session.scalars(
                    select(VacancyChangeModel).where(
                        VacancyChangeModel.vacancy_id == vacancy_id,
                        VacancyChangeModel.event_type == "RULES_EVALUATED",
                    )
                )
            )
            assert all(replay_ranking(event.changes)["matches"] for event in events)
            if target_enabled:
                tracked = DirectionRepository(session).get_tracked_vacancy(target.id, vacancy_id)
                evidence = tracked.rules_details["semantic_selection"]
                assert isinstance(evidence, dict) and evidence["status"] == "ALLOW"
                assert (
                    ApplicationAutomationService(session)
                    .prepare_for_account_id(
                        account_id=account_id, direction_name=target.name, include_stretch=True
                    )
                    .created
                    == 1
                )
        if target_enabled:
            second = SemanticSelectionProcessor(settings, client_factory=lambda *_: client).process(
                account_id, target.id, vacancy_id
            )
            assert second.status == "STRETCH" and second.model_calls == 0
    finally:
        database.close()


def test_worker_start_is_idempotent_and_recovers_after_unexpected_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from hugin.workers.semantic_selection import SemanticSelectionWorker

    worker = SemanticSelectionWorker(settings, poll_seconds=0.01)
    entered = threading.Event()
    release = threading.Event()
    recovered = threading.Event()
    calls = 0

    def run_once() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
            raise RuntimeError("Temporary failure")
        recovered.set()
        return False

    monkeypatch.setattr(worker, "run_once", run_once)
    try:
        worker.start()
        assert entered.wait(5)
        first_thread = worker._thread
        worker.start()
        assert worker._thread is first_thread and worker.running
        release.set()
        assert recovered.wait(5)
    finally:
        release.set()
        worker.stop()
    assert not worker.running


@pytest.mark.parametrize("config", [{"enabled": False}, {"enabled": "invalid"}])
def test_worker_skips_disabled_or_invalid_direction_without_model_calls(
    settings: Settings, config: dict[str, object]
) -> None:
    from hugin.database.models import ApplicationSettingsModel, CareerDirectionModel
    from hugin.workers.semantic_selection import SemanticSelectionWorker

    account_id, direction_id, _, _, _ = seed(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            direction = session.get(CareerDirectionModel, direction_id)
            options = session.get(ApplicationSettingsModel, 1)
            assert direction is not None and options is not None
            direction.scoring_config = {"semantic_selection": config}
            options.search_enabled = True
        client = Client()
        worker = SemanticSelectionWorker(
            settings,
            account_id=account_id,
            processor=SemanticSelectionProcessor(settings, client_factory=lambda *_: client),
        )
        assert not worker.run_once() and client.calls == 0
    finally:
        database.close()


def test_worker_stop_during_model_call_does_not_prepare_application(settings: Settings) -> None:
    from sqlalchemy import func, select

    from hugin.database.models import ApplicationModel, ApplicationSettingsModel
    from hugin.workers.semantic_selection import SemanticSelectionWorker

    account_id, _, _, _, _ = seed(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            options = session.get(ApplicationSettingsModel, 1)
            assert options is not None
            options.search_enabled = True
        client = Client(lambda: worker.stop())
        worker = SemanticSelectionWorker(
            settings,
            account_id=account_id,
            processor=SemanticSelectionProcessor(settings, client_factory=lambda *_: client),
        )
        assert worker.run_once()
        with database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(ApplicationModel)) == 0
        assert not worker.run_once()
    finally:
        database.close()
