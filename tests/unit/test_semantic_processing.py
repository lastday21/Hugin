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


@pytest.mark.parametrize("completed", [False, True])
def test_existing_full_source_continues_without_repeating_model_calls(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    completed: bool,
) -> None:
    from hugin.database.models import VacancyModel
    from hugin.services import semantic_snapshot
    from hugin.services.semantic_selection import SourceLine

    account_id, direction_id, vacancy_id, _, fact_id = seed(settings)
    database = create_database(settings)
    with database.sessions.begin() as session:
        vacancy = session.get(VacancyModel, vacancy_id)
        assert vacancy is not None
        vacancy.responsibilities = vacancy.description
    original = [
        SourceLine(id=0, field="title", text="Разработчик"),
        SourceLine(id=1, field="description", text="Создавать API на Python"),
        SourceLine(id=2, field="responsibilities", text="Создавать API на Python"),
    ]

    class LegacyClient(Client):
        def complete_json(self, system: str, user: str, schema: dict[str, object]) -> str:
            result = json.loads(super().complete_json(system, user, schema))
            result["source_line_ids"] = [1, 2]
            return json.dumps(result)

    allowed = True

    def stop_before_apply() -> None:
        nonlocal allowed
        if not completed:
            allowed = False

    client = LegacyClient(stop_before_apply)
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    try:
        with monkeypatch.context() as old_version:
            old_version.setattr(semantic_snapshot, "source_lines", lambda *_, **__: original)
            previous = processor.process(
                account_id,
                direction_id,
                vacancy_id,
                max_calls=6 if completed else 1,
                allowed=lambda: allowed,
            )
        with database.sessions.begin() as session:
            vacancy_record = VacancyRepository(session).get(vacancy_id)
            assert len(semantic_snapshot.source_lines(vacancy_record)) == 2
            snapshot = selection_snapshot(
                session,
                DirectionRepository(session).get_for_account(account_id, direction_id),
                vacancy_record,
            )
            assert snapshot is not None and snapshot.lines == original
        result = processor.process(account_id, direction_id, vacancy_id)
        assert result.status == "MATCH" and result.applied
        assert result.key == previous.key
        assert previous.status == ("MATCH" if completed else "STOPPED")
        assert result.model_calls == 0
        assert client.calls == 1
        with database.sessions.begin() as session:
            from hugin.services.background_processes import BackgroundProcessService

            funnel = BackgroundProcessService(session, account_id)._funnel()
            assert isinstance(funnel["stages"], list)
            assert (
                next(stage["count"] for stage in funnel["stages"] if stage["key"] == "ready") == 1
            )
        edit_fact(settings, fact_id)
        changed = processor.process(account_id, direction_id, vacancy_id)
        assert changed.model_calls == 1 and changed.key != result.key
        assert client.calls == 2
    finally:
        database.close()


def test_new_source_is_compact_and_replay_keeps_context_without_more_calls(
    settings: Settings,
) -> None:
    from sqlalchemy import select

    from hugin.database.models import VacancyChangeModel, VacancyModel
    from hugin.services.decision_evidence import replay_ranking

    account_id, direction_id, vacancy_id, _, _ = seed(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            vacancy = session.get(VacancyModel, vacancy_id)
            assert vacancy is not None
            vacancy.responsibilities = vacancy.description
        client = Client()
        processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
        first = processor.process(account_id, direction_id, vacancy_id)
        assert first.status == "MATCH" and first.model_calls == 1
        second = processor.process(account_id, direction_id, vacancy_id)
        assert second.model_calls == 0 and second.key == first.key
        with database.sessions.begin() as session:
            events = session.scalars(
                select(VacancyChangeModel).where(
                    VacancyChangeModel.vacancy_id == vacancy_id,
                    VacancyChangeModel.event_type == "RULES_EVALUATED",
                )
            ).all()
            assert events and all(replay_ranking(event.changes)["matches"] for event in events)
        assert client.calls == 1
    finally:
        database.close()


@pytest.mark.parametrize("damage", ["request", "response", "source", "other_vacancy", "legacy"])
def test_unrelated_or_damaged_legacy_stage_does_not_select_old_source(
    settings: Settings,
    damage: str,
) -> None:
    from hugin.database.models import SemanticStageModel, VacancyModel
    from hugin.services.decision_evidence import fingerprint
    from hugin.services.semantic_snapshot import source_lines

    account_id, direction_id, vacancy_id, _, _ = seed(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            vacancy = session.get(VacancyModel, vacancy_id)
            assert vacancy is not None
            vacancy.responsibilities = vacancy.description
            source = source_lines(
                VacancyRepository(session).get(vacancy_id),
                include_repeated_fields=True,
            )
            request = {"payload": {"vacancy_lines": [line.model_dump() for line in source]}}
            key = fingerprint(request)
            if damage == "request":
                key = "wrong-key"
            elif damage == "source":
                vacancy.description = "Создавать API на Python\nНовое условие"
            stage_vacancy = vacancy_id
            if damage == "other_vacancy":
                stage_vacancy = (
                    VacancyRepository(session)
                    .upsert(VacancyData("other", "Other", "https://hh.ru/vacancy/other"))
                    .id
                )
            session.add(
                SemanticStageModel(
                    account_id=account_id,
                    vacancy_id=stage_vacancy,
                    cache_key=key,
                    stage="extract" if damage == "legacy" else "assess",
                    model="test:model",
                    request=request,
                    response_text="{}",
                    response_sha256="wrong" if damage == "response" else fingerprint("{}"),
                    errors=[],
                    created_at=datetime.now(UTC),
                    duration_seconds=1,
                )
            )
            session.flush()
            vacancy_record = VacancyRepository(session).get(vacancy_id)
            snapshot = selection_snapshot(
                session,
                DirectionRepository(session).get_for_account(account_id, direction_id),
                vacancy_record,
            )
            assert snapshot is not None and snapshot.lines == source_lines(vacancy_record)
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
        if self.callback:
            self.callback()
        return json.dumps(
            {
                "fit": "direct",
                "profession": "applied_python",
                "role": "Python API",
                "reason": "Подтверждено проектом",
                "source_line_ids": [1],
                "profile_fact_ids": [payload["profile"]["facts"][0]["id"]],
                "gaps": [],
                "blocker": None,
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
        assert first.applied and first.status == "MATCH" and first.model_calls == 1
        second = processor.process(account_id, direction_id, vacancy_id)
        assert second.applied and second.model_calls == 0 and client.calls == 1
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
            result["fit"] = "possible"
            if uncertainty == "profession":
                result["profession"] = "unclear"
            else:
                result["gaps"] = ["Неясен требуемый уровень работы с API"]
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


@pytest.mark.parametrize(
    "category,reference",
    [
        ("screening_answer", "screening:1:2:question"),
        ("screening_answer", None),
        ("technology", "screening:1:2:legacy-question"),
    ],
)
def test_question_specific_answer_does_not_change_professional_selection(
    settings: Settings, category: str, reference: str | None
) -> None:
    account_id, direction_id, vacancy_id, resume_id, fact_id = seed(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            direction_record = DirectionRepository(session).get_for_account(
                account_id, direction_id
            )
            vacancy_record = VacancyRepository(session).get(vacancy_id)
            before = selection_snapshot(session, direction_record, vacancy_record)
            assert before is not None
            professional_fact = session.get(VerifiedFactModel, fact_id)
            assert professional_fact is not None
            answer = VerifiedFactModel(
                profile_id=professional_fact.profile_id,
                category=category,
                source_type="user",
                source_reference=reference,
                content="Знаком с назначением, но практически не работал",
                resume_id=resume_id,
                direction_id=direction_id,
                state=ConfirmationState.CONFIRMED,
                allow_in_forms=True,
            )
            session.add(answer)
            session.flush()
            after = selection_snapshot(session, direction_record, vacancy_record)
            assert after is not None
            assert [item.id for item in after.facts] == [fact_id]
            assert after.key == before.key
            assert after.request == before.request
            assert answer.state is ConfirmationState.CONFIRMED
            assert answer.allow_in_forms
            assert answer.content == "Знаком с назначением, но практически не работал"
    finally:
        database.close()


def test_worker_obeys_evaluation_switch_independently_of_search(
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
            options.evaluation_enabled = False
        assert not worker.run_once() and client.calls == 0
        with database.sessions.begin() as session:
            options = session.get(ApplicationSettingsModel, 1)
            assert options is not None
            options.evaluation_enabled = True
            state = session.get(SystemStateModel, 1)
            assert state is not None
            state.state = SystemState.CAPTCHA_REQUIRED
        assert not worker.run_once() and client.calls == 0
        with database.sessions.begin() as session:
            state = session.get(SystemStateModel, 1)
            assert state is not None
            state.state = SystemState.PAUSED
        assert worker.run_once()
        assert client.calls == 1
        assert not worker.run_once()
        with database.sessions() as session:
            from sqlalchemy import func, select

            from hugin.database.models import ApplicationModel

            assert session.scalar(select(func.count()).select_from(ApplicationModel)) == 0
    finally:
        worker.stop()
        database.close()


def test_application_process_prepares_queue_only_after_its_switch_is_enabled(
    settings: Settings,
) -> None:
    from sqlalchemy import func, select

    from hugin.database.models import ApplicationModel, SystemStateModel
    from hugin.domain.tasks import SystemState
    from hugin.workers.applications import ApplicationWorker

    account, direction, vacancy, _, _ = seed(settings)
    client = Client()
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    assert processor.process(account, direction, vacancy).applied
    worker = ApplicationWorker(settings, account_id=account)
    assert worker.prepare_queue() == 0
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            state = session.get(SystemStateModel, 1)
            assert state is not None
            state.state = SystemState.RUNNING
        assert worker.prepare_queue() == 1
        assert worker.prepare_queue() == 0
        with database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(ApplicationModel)) == 1
    finally:
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
                answer = json.loads(super().complete_json(system, user, schema))
                answer.update(profession="adjacent_it", fit="related")
                return json.dumps(answer)

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


@pytest.mark.parametrize("config", [{"enabled": "invalid"}])
def test_worker_skips_invalid_direction_without_model_calls(
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
            options.evaluation_enabled = True
        client = Client()
        worker = SemanticSelectionWorker(
            settings,
            account_id=account_id,
            processor=SemanticSelectionProcessor(settings, client_factory=lambda *_: client),
        )
        assert not worker.run_once() and client.calls == 0
    finally:
        database.close()


@pytest.mark.parametrize("config", [{}, {"semantic_selection": {"enabled": False}}])
def test_worker_evaluates_rules_only_once_per_vacancy_and_turn(
    settings: Settings, config: dict[str, object]
) -> None:
    from sqlalchemy import func, select

    from hugin.database.models import (
        ApplicationModel,
        ApplicationSettingsModel,
        CareerDirectionModel,
        DirectionVacancyModel,
    )
    from hugin.services.vacancy_analysis import RULES_VERSION
    from hugin.workers.semantic_selection import SemanticSelectionWorker

    account_id, direction_id, first_id, _, _ = seed(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            direction = session.get(CareerDirectionModel, direction_id)
            options = session.get(ApplicationSettingsModel, 1)
            assert direction is not None and options is not None
            direction.scoring_config = config
            options.evaluation_enabled = True
            options.search_enabled = False
            second = VacancyRepository(session).upsert(
                VacancyData(
                    "rules-only-second",
                    "Администратор баз данных",
                    "https://hh.ru/vacancy/rules-only-second",
                    description="Настраивать PostgreSQL, резервное копирование и права доступа.",
                    details_fetched_at=datetime.now(UTC),
                )
            )
            DirectionRepository(session).track_vacancy(direction_id, second.id)
            vacancy_ids = (first_id, second.id)
        client = Client()
        worker = SemanticSelectionWorker(
            settings,
            account_id=account_id,
            processor=SemanticSelectionProcessor(settings, client_factory=lambda *_: client),
        )
        for count in (1, 2):
            assert worker.run_once()
            with database.sessions() as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(DirectionVacancyModel)
                        .where(
                            DirectionVacancyModel.direction_id == direction_id,
                            DirectionVacancyModel.vacancy_id.in_(vacancy_ids),
                            DirectionVacancyModel.rules_version == RULES_VERSION,
                        )
                    )
                    == count
                )
        assert not worker.run_once()
        assert client.calls == 0
        with database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(ApplicationModel)) == 0
    finally:
        database.close()


def test_worker_respects_stop_after_selecting_rules_only_vacancy(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.orm import Session

    from hugin.database.models import ApplicationSettingsModel, CareerDirectionModel
    from hugin.workers.semantic_selection import SemanticSelectionWorker

    account_id, direction_id, vacancy_id, _, _ = seed(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            direction = session.get(CareerDirectionModel, direction_id)
            options = session.get(ApplicationSettingsModel, 1)
            assert direction is not None and options is not None
            direction.scoring_config = {}
            options.evaluation_enabled = True
        worker = SemanticSelectionWorker(settings, account_id=account_id)
        original_next = worker._next

        def stop_after_selection(session: Session) -> tuple[int, int] | None:
            selected = original_next(session)
            assert selected == (direction_id, vacancy_id)
            options = session.get(ApplicationSettingsModel, 1)
            assert options is not None
            options.evaluation_enabled = False
            return selected

        monkeypatch.setattr(worker, "_next", stop_after_selection)
        worker.run_once()
        with database.sessions() as session:
            tracked = DirectionRepository(session).get_tracked_vacancy(direction_id, vacancy_id)
            assert tracked.rules_version is None
    finally:
        database.close()


def test_worker_replaces_semantic_result_when_direction_switches_to_rules(
    settings: Settings,
) -> None:
    from hugin.database.models import ApplicationSettingsModel, CareerDirectionModel
    from hugin.services.vacancy_analysis import RULES_VERSION
    from hugin.workers.semantic_selection import SemanticSelectionWorker

    account_id, direction_id, vacancy_id, _, _ = seed(settings)
    client = Client()
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    assert processor.process(account_id, direction_id, vacancy_id).applied
    calls_before = client.calls
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            direction = session.get(CareerDirectionModel, direction_id)
            options = session.get(ApplicationSettingsModel, 1)
            assert direction is not None and options is not None
            direction.scoring_config = {}
            options.evaluation_enabled = True
            tracked = DirectionRepository(session).get_tracked_vacancy(direction_id, vacancy_id)
            assert tracked.rules_version == RULES_VERSION
            assert "semantic_selection" in tracked.rules_details
        worker = SemanticSelectionWorker(settings, account_id=account_id, processor=processor)
        assert worker.run_once()
        assert not worker.run_once()
        assert client.calls == calls_before
        with database.sessions() as session:
            tracked = DirectionRepository(session).get_tracked_vacancy(direction_id, vacancy_id)
            assert tracked.rules_version == RULES_VERSION
            assert "semantic_selection" not in tracked.rules_details
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
            options.evaluation_enabled = True

        class StoppingClient(Client):
            def complete_json(self, system: str, user: str, schema: dict[str, object]) -> str:
                response = super().complete_json(system, user, schema)
                worker.stop()
                return response

        client = StoppingClient()
        worker = SemanticSelectionWorker(
            settings,
            account_id=account_id,
            processor=SemanticSelectionProcessor(settings, client_factory=lambda *_: client),
        )
        assert worker.run_once()
        with database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(ApplicationModel)) == 0
        assert client.calls == 1
        assert not worker.run_once()
    finally:
        database.close()


def test_one_call_finishes_selection_and_repeat_reuses_saved_result(
    settings: Settings,
) -> None:
    from sqlalchemy import func, select

    from hugin.database.models import SemanticStageModel

    account_id, direction_id, vacancy_id, _, _ = seed(settings)
    client = Client()
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    first = processor.process(account_id, direction_id, vacancy_id, max_calls=1)
    assert first.status == "MATCH" and first.applied
    assert first.model_calls == 1
    with create_database(settings).sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(SemanticStageModel)
                .where(SemanticStageModel.stage == "selection")
            )
            == 1
        )
    second = processor.process(account_id, direction_id, vacancy_id, max_calls=1)
    assert second.applied and second.model_calls == 0
    assert client.calls == 1
    assert processor.process(account_id, direction_id, vacancy_id, max_calls=1).model_calls == 0


def test_evaluation_uses_oldest_unfinished_description_before_new_publication(
    settings: Settings,
) -> None:
    from datetime import timedelta

    from hugin.database.models import VacancyModel
    from hugin.services.background_processes import BackgroundProcessService
    from hugin.workers.semantic_selection import SemanticSelectionWorker

    account, direction, first_id, _, _ = seed(settings)
    now = datetime.now(UTC)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            first = session.get(VacancyModel, first_id)
            assert first is not None
            first.details_fetched_at = now - timedelta(minutes=10)
            first.published_at = now - timedelta(days=2)
            newer = VacancyRepository(session).upsert(
                VacancyData(
                    "newer-publication",
                    "Разработчик",
                    "https://hh.ru/vacancy/newer-publication",
                    description="Создавать API на Python",
                    details_fetched_at=now,
                    published_at=now,
                )
            )
            DirectionRepository(session).track_vacancy(direction, newer.id)
            BackgroundProcessService(session, account).set_enabled("evaluation", True)
        worker = SemanticSelectionWorker(settings, account_id=account)
        with database.sessions.begin() as session:
            assert worker._next(session) == (direction, first_id)
    finally:
        database.close()


def test_evaluation_resumes_saved_vacancy_after_restart_despite_new_arrival(
    settings: Settings,
) -> None:
    from datetime import timedelta

    from hugin.database.models import VacancyModel
    from hugin.services.background_processes import BackgroundProcessService
    from hugin.services.vacancy_analysis import RULES_VERSION
    from hugin.workers.semantic_selection import SemanticSelectionWorker

    account, direction, first_id, _, _ = seed(settings)
    client = Client(lambda: worker.stop())
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            BackgroundProcessService(session, account).set_enabled("evaluation", True)
        worker = SemanticSelectionWorker(
            settings, account_id=account, processor=processor, max_calls_per_turn=1
        )
        assert worker.run_once()
        assert client.calls == 1
        with database.sessions.begin() as session:
            first = session.get(VacancyModel, first_id)
            assert first is not None
            first.published_at = datetime.now(UTC) - timedelta(days=1)
            newcomer = VacancyRepository(session).upsert(
                VacancyData(
                    "arrival-during-evaluation",
                    "Разработчик",
                    "https://hh.ru/vacancy/arrival-during-evaluation",
                    description="Создавать API на Python",
                    details_fetched_at=datetime.now(UTC) - timedelta(minutes=30),
                    published_at=datetime.now(UTC),
                )
            )
            DirectionRepository(session).track_vacancy(direction, newcomer.id)
        resumed = SemanticSelectionWorker(
            settings, account_id=account, processor=processor, max_calls_per_turn=1
        )
        assert resumed.run_once()
        assert client.calls == 1
        with database.sessions() as session:
            tracked = DirectionRepository(session).get_tracked_vacancy(direction, first_id)
            assert tracked.rules_version == RULES_VERSION
            semantic = tracked.rules_details["semantic_selection"]
            assert isinstance(semantic, dict) and semantic["status"] == "ALLOW"
            assert (
                DirectionRepository(session)
                .get_tracked_vacancy(direction, newcomer.id)
                .rules_version
                is None
            )
    finally:
        database.close()


def test_evaluation_finishes_other_direction_before_leaving_saved_vacancy(
    settings: Settings,
) -> None:
    from datetime import timedelta

    from hugin.services.background_processes import BackgroundProcessService
    from hugin.services.vacancy_analysis import RULES_VERSION
    from hugin.workers.semantic_selection import SemanticSelectionWorker

    account, first_direction, vacancy_id, _, _ = seed(settings)
    client = Client()
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            BackgroundProcessService(session, account).set_enabled("evaluation", True)
        worker = SemanticSelectionWorker(
            settings, account_id=account, processor=processor, max_calls_per_turn=1
        )
        assert worker.run_once()
        with database.sessions.begin() as session:
            directions = DirectionRepository(session)
            second = directions.create(
                account,
                "Второе направление",
                scoring_config={"semantic_selection": {"enabled": True}},
            )
            resume = ResumeRepository(session).upsert(account, "other-resume", "Python API")
            directions.attach_resume(second.id, resume.id)
            profile = session.get(CandidateProfileModel, 1)
            assert profile is not None
            session.add(
                VerifiedFactModel(
                    profile_id=profile.id,
                    category="project",
                    source_type="user",
                    content="Создал API на Python для второго проекта",
                    resume_id=resume.id,
                    state=ConfirmationState.CONFIRMED,
                )
            )
            directions.track_vacancy(second.id, vacancy_id)
            newcomer = VacancyRepository(session).upsert(
                VacancyData(
                    "other-description",
                    "Разработчик",
                    "https://hh.ru/vacancy/other-description",
                    description="Создавать API на Python",
                    details_fetched_at=datetime.now(UTC) - timedelta(hours=1),
                    published_at=datetime.now(UTC),
                )
            )
            directions.track_vacancy(first_direction, newcomer.id)
        resumed = SemanticSelectionWorker(
            settings, account_id=account, processor=processor, max_calls_per_turn=1
        )
        assert resumed.run_once()
        assert client.calls == 2
        with database.sessions.begin() as session:
            second_result = DirectionRepository(session).get_tracked_vacancy(second.id, vacancy_id)
            assert second_result.rules_version == RULES_VERSION
            semantic = second_result.rules_details["semantic_selection"]
            assert isinstance(semantic, dict) and semantic["status"] == "ALLOW"
            assert resumed._next(session) == (first_direction, newcomer.id)
    finally:
        database.close()


@pytest.mark.parametrize("change", ["archived", "deleted", "disabled", "duplicate", "expired"])
def test_evaluation_skips_saved_vacancy_that_is_no_longer_eligible(
    settings: Settings, change: str
) -> None:
    from datetime import timedelta

    from hugin.database.models import CareerDirectionModel, VacancyModel
    from hugin.domain.vacancies import VacancyAvailability
    from hugin.services.background_processes import BackgroundProcessService
    from hugin.workers.semantic_selection import SemanticSelectionWorker

    account, first_direction, vacancy_id, _, _ = seed(settings)
    client = Client()
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            BackgroundProcessService(session, account).set_enabled("evaluation", True)
        worker = SemanticSelectionWorker(
            settings, account_id=account, processor=processor, max_calls_per_turn=1
        )
        assert worker.run_once()
        with database.sessions.begin() as session:
            directions = DirectionRepository(session)
            second = directions.create(account, "Другое активное направление")
            newcomer = VacancyRepository(session).upsert(
                VacancyData(
                    "eligible-after-cursor",
                    "Разработчик",
                    "https://hh.ru/vacancy/eligible-after-cursor",
                    description="Создавать API на Python",
                    details_fetched_at=datetime.now(UTC),
                )
            )
            directions.track_vacancy(second.id, newcomer.id)
            vacancy = session.get(VacancyModel, vacancy_id)
            assert vacancy is not None
            if change == "archived":
                vacancy.availability = VacancyAvailability.ARCHIVED
            elif change == "deleted":
                session.delete(vacancy)
            elif change == "disabled":
                direction = session.get(CareerDirectionModel, first_direction)
                assert direction is not None
                direction.is_active = False
            elif change == "duplicate":
                vacancy.duplicate_of_id = newcomer.id
            else:
                vacancy.published_at = datetime.now(UTC) - timedelta(days=31)
        resumed = SemanticSelectionWorker(settings, account_id=account, processor=processor)
        with database.sessions.begin() as session:
            assert resumed._next(session) == (second.id, newcomer.id)
        assert client.calls == 1
    finally:
        database.close()


def test_invalid_model_response_does_not_keep_cursor_ahead_of_other_vacancies(
    settings: Settings,
) -> None:
    from hugin.services.background_processes import BackgroundProcessService
    from hugin.workers.semantic_selection import SemanticSelectionWorker

    account, direction, vacancy_id, _, _ = seed(settings)

    class InvalidClient(Client):
        def complete_json(self, system: str, user: str, schema: dict[str, object]) -> str:
            self.calls += 1
            return "{}"

    client = InvalidClient()
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            BackgroundProcessService(session, account).set_enabled("evaluation", True)
        worker = SemanticSelectionWorker(
            settings, account_id=account, processor=processor, max_calls_per_turn=1
        )
        assert worker.run_once() and not worker.run_once()
        with database.sessions.begin() as session:
            tracked = DirectionRepository(session).get_tracked_vacancy(direction, vacancy_id)
            semantic = tracked.rules_details["semantic_selection"]
            assert isinstance(semantic, dict) and semantic["status"] == "REVIEW"
            newcomer = VacancyRepository(session).upsert(
                VacancyData(
                    "after-failed-evaluation",
                    "Разработчик",
                    "https://hh.ru/vacancy/after-failed-evaluation",
                    description="Создавать API на Python",
                    details_fetched_at=datetime.now(UTC),
                )
            )
            DirectionRepository(session).track_vacancy(direction, newcomer.id)
        with database.sessions.begin() as session:
            assert worker._next(session) == (direction, newcomer.id)
        assert client.calls == 1
    finally:
        database.close()


def test_disabling_evaluation_preserves_cursor_until_explicit_resume(settings: Settings) -> None:
    from hugin.database.models import BackgroundProcessRunModel
    from hugin.services.background_processes import BackgroundProcessService
    from hugin.workers.semantic_selection import SemanticSelectionWorker

    account, _, vacancy_id, _, _ = seed(settings)

    def disable_during_call() -> None:
        with database.sessions.begin() as session:
            BackgroundProcessService(session, account).set_enabled("evaluation", False)

    client = Client(disable_during_call)
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    worker = SemanticSelectionWorker(
        settings, account_id=account, processor=processor, max_calls_per_turn=1
    )
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            BackgroundProcessService(session, account).set_enabled("evaluation", True)
        assert worker.run_once()
        with database.sessions.begin() as session:
            BackgroundProcessService(session, account).set_enabled("evaluation", False)
        resumed = SemanticSelectionWorker(
            settings, account_id=account, processor=processor, max_calls_per_turn=1
        )
        assert not resumed.run_once()
        assert client.calls == 1
        with database.sessions.begin() as session:
            runtime = session.get(BackgroundProcessRunModel, (account, "evaluation"))
            assert runtime is not None and runtime.cursor_vacancy_id == vacancy_id
            BackgroundProcessService(session, account).set_enabled("evaluation", True)
        assert resumed.run_once()
        assert client.calls == 1
        assert not resumed.run_once()
    finally:
        database.close()
