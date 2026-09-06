from __future__ import annotations

import threading
from datetime import datetime

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.domain import (
    ApplicationEventType,
    ApplicationState,
    HhApplyResult,
    HhApplyStatus,
    SystemState,
    TaskState,
    VacancyData,
    VacancyState,
)
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    QueueTaskRepository,
    ResumeRepository,
    SystemStateRepository,
    VacancyRepository,
)
from hugin.services.application_automation import ApplicationAutomationService, ApplyJob
from hugin.services.vacancy_analysis import RULES_VERSION
from hugin.workers.applications import ApplicationWorker

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("failure", [None, "handler", "storage"])
def test_stop_and_restart_preserve_result_without_repeating_external_handler(
    settings: Settings, failure: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []
    worker: ApplicationWorker | None = None
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тест", "worker-recovery")
            resume = ResumeRepository(session).upsert(account.id, "worker-resume", "Python")
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData("worker-recovery", "Python developer", "https://hh.ru/vacancy/test")
            )
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.ANALYZED,
                score=80,
                details={"accepted": True, "category": "MATCH", "fit_tier": 1},
                rules_version=RULES_VERSION,
            )
            prepared = ApplicationAutomationService(session).prepare_for_account_id(
                account_id=account.id, direction_name=direction.name, include_stretch=False
            )
            assert prepared.created == 1
            SystemStateRepository(session).transition(SystemState.RUNNING)

        def claim(_now: datetime) -> tuple[ApplyJob | None, bool]:
            # Здесь проверяется восстановление записи; письмо и сайт заменены обработчиком.
            with database.sessions.begin() as session:
                return ApplicationAutomationService(session).claim_next(direction.id), False

        def handle(job: ApplyJob) -> HhApplyResult:
            calls.append(job.task.id)
            entered.set()
            assert release.wait(5)
            if failure == "handler":
                raise RuntimeError("Соединение прервано после начала действия")
            return HhApplyResult(HhApplyStatus.APPLIED, job.vacancy.source_url, "Подтверждено")

        worker = ApplicationWorker(settings, account_id=account.id, job_handler=handle)
        monkeypatch.setattr(worker, "_claim", claim)
        with monkeypatch.context() as recording:
            if failure == "storage":

                def fail_record(*args: object, **kwargs: object) -> None:
                    raise RuntimeError("Сбой сохранения полученного подтверждения")

                recording.setattr(ApplicationAutomationService, "record_result", fail_record)
            worker.start()
            assert entered.wait(5)
            original = worker._thread
            assert original is not None
            worker.stop(timeout_seconds=0.001)
            assert worker.running
            worker.start()
            assert worker._thread is original
            assert len(calls) == 1
            with database.sessions.begin() as session:
                assert QueueTaskRepository(session).get(calls[0]).state is TaskState.RUNNING
            release.set()
            original.join(5)
            assert not original.is_alive()
            worker.stop()

        with database.sessions.begin() as session:
            task = QueueTaskRepository(session).get(calls[0])
            expected = {
                None: TaskState.COMPLETED,
                "handler": TaskState.UNKNOWN_RESULT,
                "storage": TaskState.RUNNING,
            }[failure]
            assert task.state is expected
            assert task.attempts == 1

        worker.start()
        worker.stop()
        assert not worker.run_once()
        assert calls == [task.id]
        with database.sessions.begin() as session:
            tasks = QueueTaskRepository(session)
            restored = tasks.get(task.id)
            expected = TaskState.COMPLETED if failure is None else TaskState.UNKNOWN_RESULT
            assert restored.state is expected
            assert restored.attempts == 1
            application = ApplicationRepository(session).get(restored.application_id)
            assert application.state is (
                ApplicationState.APPLIED if failure is None else ApplicationState.APPLYING
            )
            events = ApplicationRepository(session).list_events(application.id)
            assert sum(event.event_type is ApplicationEventType.APPLIED for event in events) == (
                1 if failure is None else 0
            )
            if failure == "storage":
                assert restored.last_error_code == "INTERRUPTED_DURING_APPLY"
            assert ApplicationAutomationService(session).recover_interrupted() == 0
    finally:
        release.set()
        if worker is not None:
            worker.stop()
        database.close()
