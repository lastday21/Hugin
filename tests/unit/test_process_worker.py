import threading
from collections.abc import Callable

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.repositories.directions import AccountRepository
from hugin.services.background_processes import PROCESS_KEYS, BackgroundProcessService, ProcessKey
from hugin.workers.processes import BackgroundProcessWorker

pytestmark = pytest.mark.integration


def test_parallel_search_does_not_hold_evaluation_or_applications(settings: Settings) -> None:
    database = create_database(settings)
    with database.sessions.begin() as session:
        AccountRepository(session).create("Параллельные исполнители")
        service = BackgroundProcessService(session)
        service.stop_all()
        for key in ("search", "evaluation", "applications"):
            service.set_enabled(key, True)
    searching = threading.Event()
    release = threading.Event()
    evaluated = threading.Event()
    applied = threading.Event()

    def search(_token: int | None) -> bool:
        searching.set()
        return release.wait(10)

    def evaluation(_token: int | None) -> bool:
        if searching.wait(5):
            evaluated.set()
        return True

    def applications(_token: int | None) -> bool:
        if searching.wait(5):
            applied.set()
        return True

    worker = BackgroundProcessWorker(
        settings,
        steps={"search": search, "evaluation": evaluation, "applications": applications},
        parallel=True,
        poll_seconds=0.01,
    )
    other = BackgroundProcessWorker(settings, steps={"search": search})
    try:
        worker.start()
        assert searching.wait(5)
        assert evaluated.wait(5) and applied.wait(5)
        assert not release.is_set()
        assert not other.run_once()
        worker.stop(timeout_seconds=0)
        release.set()
        worker.stop(timeout_seconds=5)
        assert not worker.running
    finally:
        release.set()
        worker.stop(timeout_seconds=5)
        database.close()


def test_parallel_owner_loss_revokes_execution_before_another_worker_can_continue(
    settings: Settings,
) -> None:
    from sqlalchemy import text

    database = create_database(settings)
    with database.sessions.begin() as session:
        AccountRepository(session).create("Разрыв соединения владельца")
        service = BackgroundProcessService(session)
        service.stop_all()
        service.set_enabled("search", True)
    entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    sent: list[bool] = []

    def search(_token: int | None) -> bool:
        entered.set()
        if release.wait(10) and worker.has_ownership():
            sent.append(True)
        return True

    worker = BackgroundProcessWorker(
        settings,
        steps={"search": search},
        parallel=True,
        cancel=cancelled.set,
        poll_seconds=0.01,
    )
    try:
        worker.start()
        assert entered.wait(5) and worker.has_ownership()
        with database.engine.connect() as connection:
            pid = connection.scalar(
                text(
                    "SELECT l.pid FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                    "WHERE l.locktype='advisory' AND l.classid=684721 AND l.objid=1 "
                    "AND l.granted AND a.datname=current_database()"
                )
            )
            assert pid is not None
            assert connection.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        assert not worker.has_ownership()
        assert cancelled.is_set()
        release.set()
        worker.stop(timeout_seconds=5)
        assert not worker.running and not sent
    finally:
        release.set()
        worker.stop(timeout_seconds=5)
        database.close()


@pytest.mark.parametrize(
    "state", ["new", "queued", "delay", "review", "limit", "fresh_due", "retry_wait"]
)
def test_ready_applications_get_a_turn_before_search_adds_another_batch(
    settings: Settings, state: str
) -> None:
    from datetime import UTC, datetime, timedelta

    from hugin.database.models import AutomationJobModel, DirectionSearchQueryModel
    from hugin.domain.applications import ApplicationEventType, ApplicationState
    from hugin.domain.directions import VacancyState
    from hugin.domain.tasks import TaskState
    from hugin.domain.vacancies import VacancyData
    from hugin.repositories.applications import ApplicationRepository
    from hugin.repositories.automation import search_configuration_key
    from hugin.repositories.directions import DirectionRepository, ResumeRepository
    from hugin.repositories.tasks import QueueTaskRepository, SystemStateRepository
    from hugin.repositories.vacancies import VacancyRepository
    from hugin.services.application_automation import ApplicationAutomationService
    from hugin.services.automation import AutomationSchedulerService
    from hugin.services.vacancy_analysis import RULES_VERSION
    from tests.unit.test_automation_scheduler import seed_search_query

    account_id, query_id = seed_search_query(settings)
    database = create_database(settings)
    now = datetime.now(UTC)
    try:
        with database.sessions.begin() as session:
            service = BackgroundProcessService(session, account_id)
            for key in PROCESS_KEYS:
                service.set_enabled(key, True)
            AutomationSchedulerService(session).ensure_configured_jobs(account_id, now)
            query = session.get(DirectionSearchQueryModel, query_id)
            job = session.get(AutomationJobModel, f"search:{query_id}")
            assert query is not None and job is not None
            if state != "fresh_due":
                job.last_result = {
                    "fresh_search_at": now.isoformat(),
                    "fresh_search_configuration": search_configuration_key(query),
                }
            directions = DirectionRepository(session)
            resume = ResumeRepository(session).upsert(account_id, "ready-resume", "Python")
            directions.attach_resume(query.direction_id, resume.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "ready-before-depth",
                    "Python",
                    "https://hh.ru/vacancy/ready-before-depth",
                    published_at=now,
                    details_fetched_at=now,
                )
            )
            directions.track_vacancy(query.direction_id, vacancy.id)
            directions.apply_rules(
                query.direction_id,
                vacancy.id,
                state=VacancyState.ANALYZED if state == "new" else VacancyState.QUEUED,
                score=90,
                details={"category": "MATCH"},
                rules_version=RULES_VERSION,
            )
            if state != "new":
                applications = ApplicationRepository(session)
                application = applications.create_apply_intent(
                    account_id,
                    vacancy.id,
                    resume.id,
                    query.direction_id,
                )
                task = QueueTaskRepository(session).enqueue(
                    application.id,
                    90,
                    now + timedelta(minutes=15) if state == "retry_wait" else now,
                )
                if state == "review":
                    QueueTaskRepository(session).transition(task.id, TaskState.REVIEW_REQUIRED)
                elif state == "limit":
                    policy = ApplicationAutomationService(session).policy()
                    for _ in range(policy.daily_limit):
                        other = VacancyRepository(session).upsert(
                            VacancyData(str(_), "Python", f"https://hh.ru/vacancy/{_}")
                        )
                        sent = applications.create_apply_intent(account_id, other.id, resume.id)
                        applications.transition_state(sent.id, ApplicationState.APPLIED)
                        applications.append_event(
                            sent.id,
                            ApplicationEventType.APPLIED,
                            {"hh_status": "APPLIED", "external_confirmed": True},
                        )
                elif state == "delay":
                    SystemStateRepository(session).set_next_apply_at(now + timedelta(seconds=60))
        called: list[str] = []

        def step(key: ProcessKey) -> Callable[[int | None], bool]:
            def execute(_token: int | None) -> bool:
                called.append(key)
                return True

            return execute

        worker = BackgroundProcessWorker(
            settings,
            account_id=account_id,
            steps={key: step(key) for key in PROCESS_KEYS},
        )
        assert worker.run_once()
        assert called == (["applications"] if state in {"new", "queued", "delay"} else ["search"])
        if state in {"new", "queued", "delay"}:
            for _ in range(2):
                assert worker.run_once()
            assert called == ["applications", "synchronization", "replies"]
    finally:
        database.close()


@pytest.mark.parametrize(
    "search_state", ["due", "retry_wait", "fresh", "details", "evaluation", "details_paused"]
)
def test_fresh_search_runs_before_models_without_delaying_replies(
    settings: Settings, search_state: str
) -> None:
    from datetime import UTC, datetime, timedelta

    from hugin.database.models import (
        AutomationJobModel,
        DirectionSearchQueryModel,
        DirectionVacancyModel,
        VacancyModel,
    )
    from hugin.domain.automation import AutomationJobState
    from hugin.domain.vacancies import VacancyData
    from hugin.repositories.automation import search_configuration_key
    from hugin.repositories.directions import DirectionRepository
    from hugin.repositories.vacancies import VacancyRepository
    from hugin.services.automation import AutomationSchedulerService
    from hugin.services.vacancy_analysis import RULES_VERSION
    from tests.unit.test_automation_scheduler import seed_search_query

    account_id, query_id = seed_search_query(settings)
    database = create_database(settings)
    now = datetime.now(UTC)
    vacancy_id: int | None = None
    try:
        with database.sessions.begin() as session:
            service = BackgroundProcessService(session, account_id)
            for key in PROCESS_KEYS:
                service.set_enabled(key, True)
            AutomationSchedulerService(session).ensure_configured_jobs(account_id, now)
            job = session.get(AutomationJobModel, f"search:{query_id}")
            query = session.get(DirectionSearchQueryModel, query_id)
            assert job is not None and query is not None
            job.next_run_at = now - timedelta(seconds=1)
            if search_state == "retry_wait":
                job.state = AutomationJobState.FAILED
                job.next_run_at = now + timedelta(minutes=1)
            elif search_state in {"fresh", "details", "evaluation", "details_paused"}:
                job.last_result = {
                    "fresh_search_at": now.isoformat(),
                    "fresh_search_configuration": search_configuration_key(query),
                }
                if search_state != "fresh":
                    vacancy = VacancyRepository(session).upsert(
                        VacancyData(
                            "fresh-pending",
                            "Python разработчик",
                            "https://hh.ru/vacancy/fresh-pending",
                            published_at=now,
                            details_fetched_at=now if search_state == "evaluation" else None,
                        )
                    )
                    vacancy_id = vacancy.id
                    DirectionRepository(session).track_vacancy(query.direction_id, vacancy.id)
                if search_state == "details_paused":
                    service.set_enabled("evaluation", False)
        called: list[str] = []

        def step(key: ProcessKey) -> Callable[[int | None], bool]:
            def execute(_token: int | None) -> bool:
                called.append(key)
                return True

            return execute

        worker = BackgroundProcessWorker(
            settings, account_id=account_id, steps={key: step(key) for key in PROCESS_KEYS}
        )
        for _ in range(5):
            assert worker.run_once()
        expected = list(PROCESS_KEYS)
        if search_state == "due":
            expected = ["search", "synchronization", "replies", "search", "synchronization"]
        elif search_state == "details":
            expected = ["search", "evaluation", "synchronization", "replies", "search"]
        elif search_state == "evaluation":
            expected = ["evaluation", "synchronization", "replies", "evaluation", "synchronization"]
        elif search_state == "details_paused":
            expected = ["search", "applications", "synchronization", "replies", "search"]
        assert called == expected
        if search_state == "due":
            with database.sessions.begin() as session:
                job = session.get(AutomationJobModel, f"search:{query_id}")
                query = session.get(DirectionSearchQueryModel, query_id)
                assert job is not None and query is not None
                job.last_result = {
                    "fresh_search_at": datetime.now(UTC).isoformat(),
                    "fresh_search_configuration": search_configuration_key(query),
                }
            for _ in range(4):
                assert worker.run_once()
            assert called[-4:] == ["replies", "search", "evaluation", "applications"]
        elif search_state in {"details", "evaluation"}:
            with database.sessions.begin() as session:
                assert vacancy_id is not None
                vacancy_row = session.get(VacancyModel, vacancy_id)
                query = session.get(DirectionSearchQueryModel, query_id)
                assert vacancy_row is not None and query is not None
                tracked = session.get(DirectionVacancyModel, (query.direction_id, vacancy_id))
                assert tracked is not None
                vacancy_row.details_fetched_at = datetime.now(UTC)
                tracked.rules_version = RULES_VERSION
            for _ in range(4):
                assert worker.run_once()
            expected_resumed = (
                ["replies", "search", "evaluation", "applications"]
                if search_state == "evaluation"
                else ["evaluation", "applications", "synchronization", "replies"]
            )
            assert called[-4:] == expected_resumed
    finally:
        database.close()


def test_application_queue_advances_past_three_skipped_candidates(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from hugin.domain.directions import VacancyState
    from hugin.domain.vacancies import VacancyData
    from hugin.repositories.directions import DirectionRepository
    from hugin.repositories.vacancies import VacancyRepository
    from hugin.services.application_automation import ApplicationAutomationService
    from hugin.services.vacancy_analysis import RULES_VERSION
    from hugin.workers.applications import ApplicationWorker

    database = create_database(settings)
    ids: list[int] = []
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Queue", "queue-account")
            direction = DirectionRepository(session).create(account.id, "Python")
            for number in range(4):
                vacancy = VacancyRepository(session).upsert(
                    VacancyData(
                        str(number),
                        "Python",
                        f"https://hh.ru/vacancy/{number}",
                        details_fetched_at=datetime.now(UTC),
                    )
                )
                DirectionRepository(session).track_vacancy(direction.id, vacancy.id)
                from hugin.database.models import DirectionVacancyModel

                tracked = session.get(DirectionVacancyModel, (direction.id, vacancy.id))
                assert tracked is not None
                tracked.state = VacancyState.ANALYZED
                tracked.rules_version = RULES_VERSION
                tracked.rules_details = {"category": "MATCH"}
                ids.append(vacancy.id)
            BackgroundProcessService(session, account.id).set_enabled("applications", True)
        visited: list[tuple[int, ...]] = []

        def prepare(_self: object, **values: object) -> int:
            from typing import cast

            visited.append(cast(tuple[int, ...], values["vacancy_ids"]))
            return 0

        monkeypatch.setattr(ApplicationAutomationService, "prepare_vacancies", prepare)
        worker = ApplicationWorker(settings, account_id=account.id)
        assert worker.prepare_queue() == 0
        assert worker.prepare_queue() == 0
        assert visited == [tuple(reversed(ids[1:])), (ids[0],)]
        assert worker.prepare_queue() == 0
        assert visited[-1] == tuple(reversed(ids[1:]))
    finally:
        database.close()


def test_round_robin_continues_after_error_and_skips_disabled(settings: Settings) -> None:
    database = create_database(settings)
    with database.sessions.begin() as session:
        AccountRepository(session).create("Проверка очереди")
        service = BackgroundProcessService(session)
        service.stop_all()
        for key in ("search", "evaluation", "synchronization"):
            service.set_enabled(key, True)
    called: list[str] = []

    def step(key: ProcessKey) -> Callable[[int | None], bool]:
        def run(force: int | None) -> bool:
            called.append(key)
            if key == "evaluation":
                raise RuntimeError("Проверочная ошибка")
            return True

        return run

    worker = BackgroundProcessWorker(settings, steps={key: step(key) for key in PROCESS_KEYS})
    for _ in range(4):
        assert worker.run_once()
    assert called == ["search", "evaluation", "synchronization", "search"]
    with database.sessions.begin() as session:
        BackgroundProcessService(session).stop_all()
    assert not worker.run_once()
    assert len(called) == 4
    database.close()


def test_one_shot_does_not_enable_recurring_sync(settings: Settings) -> None:
    database = create_database(settings)
    with database.sessions.begin() as session:
        AccountRepository(session).create("Проверка очереди")
        service = BackgroundProcessService(session)
        service.stop_all()
        service.request_check_now()
    called: list[int | None] = []

    def synchronize(token: int | None) -> bool:
        called.append(token)
        return True

    worker = BackgroundProcessWorker(settings, steps={"synchronization": synchronize})
    assert worker.run_once()
    assert not worker.run_once()
    assert len(called) == 1 and called[0] is not None
    with database.sessions() as session:
        assert not BackgroundProcessService(session).enabled("synchronization")
    database.close()


def test_second_executor_cannot_overlap_and_lifecycle_stops(settings: Settings) -> None:
    database = create_database(settings)
    with database.sessions.begin() as session:
        AccountRepository(session).create("Проверка единственного исполнителя")
        service = BackgroundProcessService(session)
        service.stop_all()
        service.set_enabled("search", True)
    entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    calls: list[int] = []

    def search(_token: int | None) -> bool:
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return True

    worker = BackgroundProcessWorker(
        settings,
        steps={"search": search},
        poll_seconds=0.01,
        heartbeat_seconds=0.01,
        cancel=cancelled.set,
    )
    other = BackgroundProcessWorker(settings, steps={"search": search})
    try:
        worker.start()
        assert entered.wait(5)
        thread = worker._thread
        worker.start()
        assert worker._thread is thread and worker.running
        assert not other.run_once()
        with database.sessions() as session:
            from hugin.database.models import BackgroundProcessRunModel

            runtime = session.get(BackgroundProcessRunModel, (1, "search"))
            assert runtime is not None and runtime.state == "running" and runtime.runs == 1
        worker.stop(timeout_seconds=0)
        assert cancelled.is_set()
        release.set()
        worker.stop(timeout_seconds=5)
        assert not worker.running and not worker.run_once()
        assert calls == [1]
    finally:
        release.set()
        worker.stop(timeout_seconds=5)
        database.close()


def test_missing_account_does_not_start_any_process(settings: Settings) -> None:
    worker = BackgroundProcessWorker(settings, steps={"search": lambda _: True})
    assert not worker.run_once()


def test_restart_recovers_interrupted_processes_and_continues_saved_order(
    settings: Settings,
) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from hugin.database.models import BackgroundProcessRunModel

    database = create_database(settings)
    now = datetime.now(UTC)
    with database.sessions.begin() as session:
        account = AccountRepository(session).create("Восстановление")
        other = AccountRepository(session).create("Другой аккаунт")
        service = BackgroundProcessService(session, account.id)
        for key in PROCESS_KEYS:
            service.set_enabled(key, True)
        service.started("search", now=now - timedelta(minutes=1))
        service.started("evaluation", now=now)
        BackgroundProcessService(session, other.id).started("replies", now=now)
    observed: list[dict[str, str]] = []
    called: list[str] = []

    def step(key: ProcessKey) -> Callable[[int | None], bool]:
        def execute(_token: int | None) -> bool:
            called.append(key)
            with database.sessions() as session:
                observed.append(
                    {
                        row.key: row.state
                        for row in session.scalars(
                            select(BackgroundProcessRunModel).where(
                                BackgroundProcessRunModel.account_id == account.id
                            )
                        )
                    }
                )
            return True

        return execute

    try:
        worker = BackgroundProcessWorker(
            settings, account_id=account.id, steps={key: step(key) for key in PROCESS_KEYS}
        )
        assert worker.run_once()
        restarted = BackgroundProcessWorker(
            settings, account_id=account.id, steps={key: step(key) for key in PROCESS_KEYS}
        )
        assert restarted.run_once()
        assert called == ["applications", "synchronization"]
        assert observed[0]["search"] == observed[0]["evaluation"] == "interrupted"
        assert [key for key, state in observed[0].items() if state == "running"] == ["applications"]
        with database.sessions() as session:
            interrupted = session.get(BackgroundProcessRunModel, (account.id, "evaluation"))
            assert interrupted is not None
            assert interrupted.last_finished_at is None and interrupted.completed == 0
            assert interrupted.runs == 1 and interrupted.heartbeat_at == now
            other_runtime = session.get(BackgroundProcessRunModel, (other.id, "replies"))
            assert other_runtime is not None and other_runtime.state == "running"
    finally:
        database.close()


def test_recovery_preserves_disabled_processes_and_does_not_start_work(settings: Settings) -> None:
    from hugin.database.models import BackgroundProcessRunModel

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Остановленное восстановление")
            service = BackgroundProcessService(session, account.id)
            service.stop_all()
            service.started("evaluation")
        calls: list[str] = []

        def evaluate(_token: int | None) -> bool:
            calls.append("evaluation")
            return True

        worker = BackgroundProcessWorker(
            settings, account_id=account.id, steps={"evaluation": evaluate}
        )
        assert not worker.run_once()
        assert not calls
        with database.sessions() as session:
            assert all(not BackgroundProcessService(session).enabled(key) for key in PROCESS_KEYS)
            runtime = session.get(BackgroundProcessRunModel, (account.id, "evaluation"))
            assert runtime is not None and runtime.state == "interrupted"
            assert runtime.completed == 0 and runtime.last_finished_at is None
    finally:
        database.close()
