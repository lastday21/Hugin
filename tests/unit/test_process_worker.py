import threading
from collections.abc import Callable

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.repositories.directions import AccountRepository
from hugin.services.background_processes import PROCESS_KEYS, BackgroundProcessService, ProcessKey
from hugin.workers.processes import BackgroundProcessWorker

pytestmark = pytest.mark.integration


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
