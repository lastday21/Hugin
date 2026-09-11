# ruff: noqa: RUF001

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationSettingsModel,
    CareerDirectionModel,
    DirectionVacancyModel,
    SystemStateModel,
    VacancyModel,
)
from hugin.domain.directions import VacancyState
from hugin.domain.tasks import SystemState
from hugin.repositories.directions import AccountRepository
from hugin.repositories.tasks import SystemStateRepository
from hugin.services.autonomy import AutonomyPolicyService
from hugin.services.background_processes import PROCESS_KEYS, BackgroundProcessService
from hugin.services.vacancy_analysis import RULES_VERSION

pytestmark = pytest.mark.integration


@pytest.fixture
def service(settings: Settings) -> Iterator[BackgroundProcessService]:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тест", "process-controls")
            yield BackgroundProcessService(session, account.id)
    finally:
        database.close()


def test_independent_switches_and_combined_replies(service: BackgroundProcessService) -> None:
    service.stop_all()
    for key in PROCESS_KEYS:
        service.set_enabled(key, True)
        assert [item for item in PROCESS_KEYS if service.enabled(item)] == [key]
        service.set_enabled(key, False)
    service.set_enabled("replies", True)
    policy = AutonomyPolicyService(service._session).get()
    assert policy.auto_prepare_replies and policy.auto_send_approved_replies


def test_stop_all_revokes_lease_and_keeps_protection(service: BackgroundProcessService) -> None:
    repository = SystemStateRepository(service._session)
    repository.acquire_supervised_lease("test-token", ttl=timedelta(minutes=5))
    state = service._session.get(SystemStateModel, 1)
    assert state is not None
    state.state = SystemState.CAPTCHA_REQUIRED
    service.stop_all()
    assert state.state == SystemState.CAPTCHA_REQUIRED
    assert state.recovery_state == SystemState.PAUSED
    assert not repository.supervised_lease_active()
    assert not any(service.enabled(key) for key in PROCESS_KEYS)
    with pytest.raises(ValueError):
        service.set_enabled("applications", True)


def test_once_does_not_enable_recurring_sync(service: BackgroundProcessService) -> None:
    service.stop_all()
    service.request_check_now()
    assert not service.enabled("synchronization")
    assert service.claim_check_now()
    assert not service.claim_check_now()
    assert not service.enabled("synchronization")


def test_stop_revokes_claimed_once_and_pending_once(service: BackgroundProcessService) -> None:
    service.stop_all()
    service.request_check_now()
    service.stop_all()
    assert service.claim_check_now_token() is None
    service.request_check_now()
    token = service.claim_check_now_token()
    assert token is not None
    assert service.one_shot_allowed(token)
    service.stop_all()
    assert not service.one_shot_allowed(token)
    service.request_check_now()
    later = service.claim_check_now_token()
    assert later is not None and later != token
    assert service.one_shot_allowed(later)


def test_runtime_stopping_and_interruption(service: BackgroundProcessService) -> None:
    service.stop_all()
    service.set_enabled("evaluation", True)
    service.started("evaluation")
    service.set_enabled("evaluation", False)
    assert cast(dict[str, Any], service.snapshot())["processes"][1]["state"] == "stopping"
    service.finished("evaluation", worked=True)
    assert cast(dict[str, Any], service.snapshot())["processes"][1]["state"] == "disabled"
    service.set_enabled("evaluation", True)
    service.started("evaluation", now=datetime.now(UTC) - timedelta(hours=1))
    assert cast(dict[str, Any], service.snapshot())["processes"][1]["state"] == "interrupted"


def test_safe_new_defaults(service: BackgroundProcessService) -> None:
    settings = service._session.get(ApplicationSettingsModel, 1)
    assert settings is not None
    assert not settings.evaluation_enabled
    assert not settings.synchronization_enabled


def test_empty_account_has_no_work_and_api_mutations_are_not_found(settings: Settings) -> None:
    from fastapi.testclient import TestClient

    from hugin.api.app import create_app

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            service = BackgroundProcessService(session)
            assert not any(service.enabled(key) for key in PROCESS_KEYS)
            assert service.claim_check_now_token() is None
        app = create_app(settings)
        with TestClient(app) as client:
            assert client.get("/api/processes").status_code == 200
            headers = {"X-Hugin-Session": app.state.session_key}
            assert client.post("/api/processes/stop-all", headers=headers).status_code == 404
    finally:
        database.close()


def test_rechecks_protection_changed_by_another_session(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as setup:
            account = AccountRepository(setup).create("Guard", "guard-test")
        with database.sessions() as session:
            original = session.get(SystemStateModel, 1)
            assert original is not None and original.state == SystemState.PAUSED
            service = BackgroundProcessService(session, account.id)
            with database.sessions.begin() as external:
                state = external.get(SystemStateModel, 1)
                assert state is not None
                state.state = SystemState.ACCOUNT_WARNING
            with pytest.raises(ValueError):
                service.set_enabled("search", True)
    finally:
        database.close()


def test_protection_shows_pending_resume_permission_until_explicit_stop(
    service: BackgroundProcessService,
) -> None:
    state = service._session.get(SystemStateModel, 1)
    assert state is not None
    state.state = SystemState.AUTH_REQUIRED
    state.recovery_state = SystemState.RUNNING
    service._session.flush()
    item = cast(dict[str, Any], service.snapshot())["processes"][2]
    assert item["enabled"] and item["state"] == "blocked"
    assert not service.enabled("applications")
    service.set_enabled("applications", False)
    assert state.state == SystemState.AUTH_REQUIRED
    assert state.recovery_state == SystemState.PAUSED


def test_funnel_unique_overlap_stale_unknown_and_ready(service: BackgroundProcessService) -> None:
    session = service._session
    directions = [
        CareerDirectionModel(account_id=service._account_id, name=name) for name in ("Python", "ИТ")
    ]
    session.add_all(directions)
    session.flush()
    for index, (details, rules, category) in enumerate(
        (
            (False, None, None),
            (True, None, None),
            (True, RULES_VERSION, "MATCH"),
            (True, RULES_VERSION, "REVIEW"),
            (True, RULES_VERSION, "REJECTED"),
            (True, RULES_VERSION, None),
        )
    ):
        vacancy = VacancyModel(
            hh_id=str(index),
            title="Python",
            source_url="https://hh.ru",
            details_fetched_at=datetime.now(UTC) if details else None,
        )
        session.add(vacancy)
        session.flush()
        session.add(
            DirectionVacancyModel(
                direction_id=directions[0].id,
                vacancy_id=vacancy.id,
                rules_version=rules,
                rules_details={"category": category},
                state=VacancyState.ANALYZED if details else VacancyState.DISCOVERED,
            )
        )
        if index == 2:
            session.add(DirectionVacancyModel(direction_id=directions[1].id, vacancy_id=vacancy.id))
    session.flush()
    funnel = cast(dict[str, Any], service.snapshot())["funnel"]
    assert funnel["total"] == 6
    counts = {item["key"]: item["count"] for item in funnel["stages"]}
    assert counts == {
        "sent": 0,
        "unavailable": 0,
        "awaiting_details": 1,
        "awaiting_evaluation": 1,
        "review": 2,
        "ready": 1,
        "rejected": 1,
    }
    assert sum(counts.values()) == funnel["total"]


@pytest.mark.parametrize("exclusion", ["old", "duplicate"])
@pytest.mark.parametrize("has_details", [False, True])
def test_funnel_existing_exclusions_never_wait_but_keep_uncertain_and_sent_precedence(
    service: BackgroundProcessService,
    exclusion: str,
    has_details: bool,
) -> None:
    from hugin.database.models import ApplicationModel, ApplicationTaskModel
    from hugin.domain.applications import ApplicationState
    from hugin.domain.tasks import TaskState
    from hugin.domain.vacancies import VacancyAvailability
    from hugin.repositories.directions import ResumeRepository

    session = service._session
    direction = CareerDirectionModel(account_id=service._account_id, name="Excluded")
    original = VacancyModel(hh_id="original", title="Original", source_url="https://hh.ru/1")
    session.add_all((direction, original))
    session.flush()
    vacancy = VacancyModel(
        hh_id="excluded",
        title="Excluded",
        source_url="https://hh.ru/2",
        published_at=datetime.now(UTC) - timedelta(days=31) if exclusion == "old" else None,
        duplicate_of_id=original.id if exclusion == "duplicate" else None,
        details_fetched_at=datetime.now(UTC) if has_details else None,
    )
    session.add(vacancy)
    session.flush()
    session.add(
        DirectionVacancyModel(
            direction_id=direction.id,
            vacancy_id=vacancy.id,
            rules_version=RULES_VERSION,
            rules_details={"category": "MATCH"},
            state=VacancyState.QUEUED,
        )
    )
    session.flush()

    def stage() -> str:
        funnel = cast(dict[str, Any], service.snapshot())["funnel"]
        assert funnel["total"] == 1
        return next(str(item["key"]) for item in funnel["stages"] if item["count"] == 1)

    assert stage() == "rejected"
    resume = ResumeRepository(session).upsert(service._account_id, "exclusion-resume", "Python")
    application = ApplicationModel(
        account_id=service._account_id,
        vacancy_id=vacancy.id,
        resume_id=resume.id,
        state=ApplicationState.APPLYING,
    )
    session.add(application)
    session.flush()
    task = ApplicationTaskModel(
        application_id=application.id,
        state=TaskState.UNKNOWN_RESULT,
        priority_score=70,
        scheduled_at=datetime.now(UTC),
    )
    session.add(task)
    session.flush()
    assert stage() == "review"
    application.state = ApplicationState.APPLIED
    session.flush()
    assert stage() == "sent"
    vacancy.availability = VacancyAvailability.CLOSED
    session.flush()
    assert stage() == "sent"
    application.state = ApplicationState.APPLYING
    session.flush()
    assert stage() == "unavailable"


def test_process_api_guard_and_validation(settings: Settings) -> None:
    from fastapi.testclient import TestClient

    from hugin.api.app import create_app

    database = create_database(settings)
    with database.sessions.begin() as session:
        account = AccountRepository(session).create("Тест", "api-process-controls")
    database.close()
    app = create_app(settings)
    with TestClient(app) as client:
        url = f"/api/processes?account_id={account.id}"
        assert client.get(url).status_code == 200
        assert client.post(f"/api/processes/stop-all?account_id={account.id}").status_code == 403
        headers = {"X-Hugin-Session": app.state.session_key}
        assert (
            client.post(
                f"/api/processes/stop-all?account_id={account.id}", headers=headers
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/api/processes/evaluation?account_id={account.id}",
                json={"enabled": True},
                headers=headers,
            ).status_code
            == 200
        )
        assert (
            client.put(
                "/api/processes/no-such-process", json={"enabled": True}, headers=headers
            ).status_code
            == 422
        )


def test_failure_and_heartbeat_are_visible(service: BackgroundProcessService) -> None:
    service.set_enabled("evaluation", True)
    service.started("evaluation", now=datetime.now(UTC) - timedelta(hours=1))
    service.heartbeat("evaluation")
    assert cast(dict[str, Any], service.snapshot())["processes"][1]["state"] == "running"
    service.failed("evaluation", "Не удалось получить ответ модели")
    item = cast(dict[str, Any], service.snapshot())["processes"][1]
    assert item["state"] == "error"
    assert item["reason"] == "Не удалось получить ответ модели"


def test_waiting_distinguishes_first_turn_and_no_saved_evaluation(
    service: BackgroundProcessService,
) -> None:
    service.set_enabled("evaluation", True)
    initial = cast(dict[str, Any], service.snapshot())["processes"][1]
    assert initial["reason"] == "Ожидает первого хода исполнителя"
    service.started("evaluation")
    service.finished("evaluation", worked=False)
    empty = cast(dict[str, Any], service.snapshot())["processes"][1]
    assert empty["reason"] == "Нет сохранённых вакансий для оценки"


def test_waiting_search_reports_no_queries_then_actual_schedule_and_deferral(
    service: BackgroundProcessService,
) -> None:
    from hugin.database.models import AutomationJobModel, DirectionSearchQueryModel
    from hugin.domain.automation import AutomationJobKind, AutomationJobState

    service.set_enabled("search", True)
    service.started("search")
    service.finished("search", worked=False)
    assert (
        cast(dict[str, Any], service.snapshot())["processes"][0]["reason"]
        == "Нет активных поисковых запросов"
    )
    session = service._session
    direction = CareerDirectionModel(account_id=service._account_id, name="Waiting")
    session.add(direction)
    session.flush()
    query = DirectionSearchQueryModel(direction_id=direction.id, query="Python")
    session.add(query)
    session.flush()
    next_run = datetime.now(UTC) + timedelta(hours=2)
    job = AutomationJobModel(
        key="waiting-search",
        kind=AutomationJobKind.SEARCH,
        account_id=service._account_id,
        search_query_id=query.id,
        interval_seconds=600,
        next_run_at=next_run,
        last_finished_at=datetime.now(UTC),
        last_result={"deferred": True, "reason": "BROWSER_PROFILE_BUSY"},
    )
    session.add(job)
    session.flush()
    reason = cast(dict[str, Any], service.snapshot())["processes"][0]["reason"]
    assert "Последний ход отложен" in reason and "профиль hh.ru занят" in reason
    assert "Следующий поиск:" in reason
    job.state = AutomationJobState.FAILED
    job.last_error_message = "Не удалось открыть страницу"
    session.flush()
    assert (
        "Ошибка последнего хода: Не удалось открыть страницу"
        in cast(dict[str, Any], service.snapshot())["processes"][0]["reason"]
    )


def test_waiting_sync_uses_earliest_job_and_replies_keep_retry_time(
    service: BackgroundProcessService,
) -> None:
    from hugin.database.models import AutomationJobModel
    from hugin.domain.automation import AutomationJobKind

    service.set_enabled("synchronization", True)
    service.started("synchronization")
    service.finished("synchronization", worked=False)
    for kind, delay in ((AutomationJobKind.MESSAGES, 30), (AutomationJobKind.STATUSES, 10)):
        service._session.add(
            AutomationJobModel(
                key=kind.value,
                kind=kind,
                account_id=service._account_id,
                interval_seconds=300,
                next_run_at=datetime.now(UTC) + timedelta(minutes=delay),
            )
        )
    service._session.flush()
    reason = cast(dict[str, Any], service.snapshot())["processes"][3]["reason"]
    assert "Следующая проверка статусов:" in reason
    service.set_enabled("replies", True)
    service.started("replies")
    service.finished("replies", worked=False)
    service._runtime("replies").retry_after_at = datetime.now(UTC) + timedelta(minutes=3)
    service._session.flush()
    assert (
        "Повтор ответа не раньше"
        in cast(dict[str, Any], service.snapshot())["processes"][4]["reason"]
    )


def test_waiting_applications_explains_delay_and_daily_limit(
    service: BackgroundProcessService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hugin.repositories.applications import ApplicationRepository

    service.set_enabled("applications", True)
    service.started("applications")
    service.finished("applications", worked=False)
    state = service._session.get(SystemStateModel, 1)
    assert state is not None
    state.next_apply_at = datetime.now(UTC) + timedelta(minutes=7)
    service._session.flush()
    assert (
        "Следующая отправка не раньше"
        in cast(dict[str, Any], service.snapshot())["processes"][2]["reason"]
    )
    monkeypatch.setattr(ApplicationRepository, "count_applied_since", lambda *args: 25)
    assert (
        "Достигнут дневной предел откликов (25)"
        in cast(dict[str, Any], service.snapshot())["processes"][2]["reason"]
    )


def test_semantic_funnel_requires_current_profile_and_intact_stored_result(
    settings: Settings,
) -> None:
    from sqlalchemy import select

    from hugin.database.models import SemanticStageModel
    from hugin.services.semantic_processing import SemanticSelectionProcessor
    from tests.unit.test_semantic_processing import Client, edit_fact, seed

    account, direction, vacancy, _, fact = seed(settings)
    database = create_database(settings)

    def counts() -> dict[str, int]:
        with database.sessions() as session:
            value = cast(dict[str, Any], BackgroundProcessService(session, account).snapshot())
            return {item["key"]: item["count"] for item in value["funnel"]["stages"]}

    try:
        assert counts()["awaiting_evaluation"] == 1
        client = Client()
        processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
        assert processor.process(account, direction, vacancy).applied
        assert counts()["ready"] == 1
        with database.sessions.begin() as session:
            stage = session.scalar(
                select(SemanticStageModel).where(SemanticStageModel.stage == "selection")
            )
            assert stage is not None
            original = stage.response_sha256
            stage.response_sha256 = "broken"
        assert counts()["awaiting_evaluation"] == 1
        with database.sessions.begin() as session:
            stage = session.scalar(
                select(SemanticStageModel).where(SemanticStageModel.stage == "selection")
            )
            assert stage is not None
            stage.response_sha256 = original
        assert counts()["ready"] == 1
        edit_fact(settings, fact)
        assert counts()["awaiting_evaluation"] == 1
    finally:
        database.close()


def test_search_observation_keeps_time_and_ignores_unobserved_deferred_counts(
    service: BackgroundProcessService,
) -> None:
    from hugin.database.models import AutomationJobModel, DirectionSearchQueryModel
    from hugin.domain.automation import AutomationJobKind

    session = service._session
    direction = CareerDirectionModel(account_id=service._account_id, name="Search")
    session.add(direction)
    session.flush()
    queries = [
        DirectionSearchQueryModel(direction_id=direction.id, query=str(index)) for index in range(2)
    ]
    session.add_all(queries)
    session.flush()
    for index, query in enumerate(queries):
        session.add(
            AutomationJobModel(
                key=f"search-{index}",
                kind=AutomationJobKind.SEARCH,
                account_id=service._account_id,
                search_query_id=query.id,
                interval_seconds=300,
                last_finished_at=datetime.now(UTC),
                last_result=(
                    {
                        "observed_at": "2026-09-09T05:00:00+00:00",
                        "observed_query": "Python",
                        "observed_region": "113",
                        "observed_page": 2,
                        "observed_found": 125,
                        "pages_loaded": 0,
                        "details_loaded": 3,
                    }
                    if index == 0
                    else {"found": 999, "deferred": "APPLICATIONS_PENDING"}
                ),
            )
        )
    session.flush()
    result = cast(dict[str, Any], service.snapshot())["last_search"]
    assert result["observed_at"] == "2026-09-09T05:00:00+00:00"
    assert result["found"] == 125 and result["page"] == 2
    assert result["coverage_exhausted"] is None


def test_sync_schedule_once_and_protection_api(settings: Settings) -> None:
    from fastapi.testclient import TestClient

    from hugin.api.app import create_app

    database = create_database(settings)
    with database.sessions.begin() as session:
        account = AccountRepository(session).create("Test", "sync-api")
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"X-Hugin-Session": app.state.session_key}
        suffix = f"?account_id={account.id}"
        assert (
            client.put(
                "/api/processes/synchronization/schedule" + suffix,
                headers=headers,
                json={"message_interval_minutes": 0, "status_interval_minutes": 10},
            ).status_code
            == 422
        )
        result = client.put(
            "/api/processes/synchronization/schedule" + suffix,
            headers=headers,
            json={"message_interval_minutes": 11, "status_interval_minutes": 22},
        )
        assert result.status_code == 200
        assert result.json()["synchronization"]["message_interval_minutes"] == 11
        assert client.post(
            "/api/processes/synchronization/check-now" + suffix, headers=headers
        ).json()["synchronization"]["check_now_pending"]
        with database.sessions.begin() as session:
            state = session.get(SystemStateModel, 1)
            assert state is not None
            state.state = SystemState.AUTH_REQUIRED
        assert (
            client.post(
                "/api/processes/synchronization/check-now" + suffix, headers=headers
            ).status_code
            == 409
        )
        assert (
            client.put(
                "/api/processes/search" + suffix, headers=headers, json={"enabled": True}
            ).status_code
            == 409
        )
        assert (
            client.put(
                "/api/processes/search" + suffix, headers=headers, json={"enabled": False}
            ).status_code
            == 200
        )
    database.close()


def test_unknown_application_never_shows_ready_and_sent_beats_unavailable(
    settings: Settings,
) -> None:
    from hugin.database.models import ApplicationModel, ApplicationTaskModel
    from hugin.domain.applications import ApplicationState
    from hugin.domain.tasks import TaskState
    from hugin.domain.vacancies import VacancyAvailability
    from tests.unit.test_communications import create_application

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            _, application_id = create_application(
                session, account_label="Test", vacancy_hh_id="unknown"
            )
            application = session.get(ApplicationModel, application_id)
            assert application is not None
            direction = CareerDirectionModel(account_id=application.account_id, name="Visible")
            session.add(direction)
            session.flush()
            vacancy = session.get(VacancyModel, application.vacancy_id)
            assert vacancy is not None
            vacancy.details_fetched_at = datetime.now(UTC)
            session.add(
                DirectionVacancyModel(
                    direction_id=direction.id,
                    vacancy_id=vacancy.id,
                    state=VacancyState.ANALYZED,
                    rules_version=RULES_VERSION,
                    rules_details={"category": "MATCH"},
                )
            )
            application.state = ApplicationState.APPLYING
            session.add(
                ApplicationTaskModel(
                    application_id=application.id,
                    state=TaskState.UNKNOWN_RESULT,
                    priority_score=70,
                    scheduled_at=datetime.now(UTC),
                )
            )
            session.flush()
            service = BackgroundProcessService(session, application.account_id)

            def count(key: str) -> int:
                value = cast(dict[str, Any], service.snapshot())
                return next(
                    int(item["count"]) for item in value["funnel"]["stages"] if item["key"] == key
                )

            assert count("review") == 1
            application.state = ApplicationState.APPLIED
            vacancy.availability = VacancyAvailability.CLOSED
            session.flush()
            assert count("sent") == 1
            application.state = ApplicationState.APPLYING
            session.flush()
            assert count("unavailable") == 1
    finally:
        database.close()
