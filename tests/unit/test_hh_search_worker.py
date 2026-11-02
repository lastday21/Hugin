from __future__ import annotations

import threading
from datetime import UTC, datetime
from types import TracebackType

import pytest

import hugin.workers.hh_search as search_worker_module
from hugin.core.settings import Settings
from hugin.diagnostics import OperationJournal
from hugin.domain import (
    AutomationJobKind,
    AutomationJobRecord,
    AutomationJobResult,
    AutomationJobState,
)
from hugin.domain.hh_sync import HhSyncBlockedError, HhSyncRetryableError
from hugin.services.hh_login import LoginResult, LoginStatus
from hugin.services.period_search import PeriodSearchCycle
from hugin.workers.automation import AutomationJobBlocked, AutomationJobDeferred, AutomationJobRetry
from hugin.workers.hh_search import HhSearchJobHandler


def make_job(
    *,
    kind: AutomationJobKind = AutomationJobKind.SEARCH,
    account_id: int = 1,
    search_query_id: int | None = 7,
    last_result: AutomationJobResult | None = None,
) -> AutomationJobRecord:
    now = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    return AutomationJobRecord(
        key="search:7",
        kind=kind,
        state=AutomationJobState.RUNNING,
        account_id=account_id,
        search_query_id=search_query_id,
        interval_seconds=7200,
        next_run_at=now,
        last_started_at=now,
        last_finished_at=None,
        last_success_at=None,
        heartbeat_at=now,
        consecutive_failures=0,
        last_error_code=None,
        last_error_message=None,
        last_result=dict(last_result or {}),
        created_at=now,
        updated_at=now,
    )


class FakeBrowser:
    def __init__(
        self,
        arguments: tuple[object, ...],
        keyword_arguments: dict[str, object],
    ) -> None:
        self.arguments = arguments
        self.keyword_arguments = keyword_arguments
        self.entered = False
        self.exited = False

    def __enter__(self) -> FakeBrowser:
        self.entered = True
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self.exited = True


class FakeCycle:
    def __init__(self, result: AutomationJobResult) -> None:
        self.result = result
        self.calls: list[tuple[int, int, object, bool]] = []

    def run(
        self,
        *,
        account_id: int,
        search_query_id: int,
        browser: object,
        prefer_fresh_search: bool = False,
    ) -> AutomationJobResult:
        self.calls.append((account_id, search_query_id, browser, prefer_fresh_search))
        return self.result


class FakeLoginService:
    def __init__(
        self,
        status: LoginStatus,
        credential_store: object,
        calls: list[tuple[int, object]],
    ) -> None:
        self.status = status
        self.credential_store = credential_store
        self.calls = calls

    def authenticate(self, account_id: int, browser: object) -> LoginResult:
        self.calls.append((account_id, browser))
        return LoginResult(self.status)


def prepare_handler(
    monkeypatch: pytest.MonkeyPatch,
    status: LoginStatus,
    *,
    browser_lock: threading.Lock | None = None,
) -> tuple[HhSearchJobHandler, FakeCycle, list[FakeBrowser], list[tuple[int, object]]]:
    browsers: list[FakeBrowser] = []
    login_calls: list[tuple[int, object]] = []
    credential_store = object()
    cycle = FakeCycle({"found": 4, "queued": 2})

    def create_browser(*arguments: object, **keyword_arguments: object) -> FakeBrowser:
        browser = FakeBrowser(arguments, keyword_arguments)
        browsers.append(browser)
        return browser

    def create_credential_store() -> object:
        return credential_store

    def create_login_service(store: object) -> FakeLoginService:
        assert store is credential_store
        return FakeLoginService(status, store, login_calls)

    def create_cycle(
        _settings: Settings,
        *,
        page_limit: int,
        detail_limit: int,
    ) -> FakeCycle:
        assert page_limit == 3
        assert detail_limit == 20
        return cycle

    monkeypatch.setattr(search_worker_module, "VisibleHhBrowser", create_browser)
    monkeypatch.setattr(
        search_worker_module,
        "WindowsCredentialStore",
        create_credential_store,
    )
    monkeypatch.setattr(search_worker_module, "HhLoginService", create_login_service)
    monkeypatch.setattr(search_worker_module, "BackgroundSearchCycle", create_cycle)
    handler = HhSearchJobHandler(
        Settings(environment="test"),
        browser_lock=browser_lock,
    )
    return handler, cycle, browsers, login_calls


@pytest.mark.parametrize(
    "boundary",
    ["budget", "stopped", "fresh", "completed", "failure", "details_first", "other_direction"],
)
def test_period_depth_reuses_browser_within_budget_and_respects_boundaries(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    handler, _, browsers, login_calls = prepare_handler(monkeypatch, LoginStatus.AUTHENTICATED)
    calls: list[AutomationJobResult] = []

    class DepthCycle(PeriodSearchCycle):
        def run(self, **kwargs: object) -> AutomationJobResult:
            previous = kwargs["progress"]
            assert isinstance(previous, dict)
            calls.append(dict(previous))
            if boundary in {"details_first", "other_direction"} and len(calls) == 1:
                return {
                    **previous,
                    "pages_loaded": 0,
                    "details_loaded": 3 if boundary == "details_first" else 0,
                    "depth_waited_for_details": True,
                    "observed_search_kind": "fresh",
                    "continuation": True,
                }
            if boundary == "failure" and len(calls) == 2:
                raise HhSyncRetryableError("HH_NETWORK_TIMEOUT", "Сеть", retry_after_seconds=30)
            if boundary == "stopped" and len(calls) == 2:
                return {**previous, "pages_loaded": 0, "reason": "Поиск остановлен"}
            return {
                "cursor_signature": "same-period",
                "checkpoint": len(calls) - (boundary == "details_first"),
                "pages_loaded": 1,
                "new_vacancies": 2,
                "observed_search_kind": "fresh" if boundary == "fresh" else "depth",
                "round_complete": boundary == "completed",
                "continuation": boundary != "completed",
            }

    handler._cycle = DepthCycle(handler._settings)
    original: AutomationJobResult = {"cursor_signature": "same-period", "checkpoint": 0}
    job = make_job(last_result=original)
    if boundary == "failure":
        with pytest.raises(AutomationJobRetry) as error:
            handler(job)
        assert error.value.code == "HH_NETWORK_TIMEOUT"
        assert len(calls) == 2
    else:
        result = handler(job)
        expected_pages = {"budget": 3, "details_first": 2, "other_direction": 0}.get(boundary, 1)
        assert result["pages_loaded"] == expected_pages
        assert result["checkpoint"] == expected_pages
        expected_calls = {"stopped": 2, "details_first": 3, "other_direction": 1}.get(
            boundary, expected_pages
        )
        assert len(calls) == expected_calls
        if boundary == "budget":
            assert result["new_vacancies"] == 6
        if boundary == "details_first":
            assert result["details_loaded"] == 3
            assert result["new_vacancies"] == 4
    assert job.last_result == original
    assert len(browsers) == len(login_calls) == 1
    assert browsers[0].exited


@pytest.mark.parametrize(
    "job",
    [
        make_job(kind=AutomationJobKind.MESSAGES, search_query_id=None),
        make_job(search_query_id=None),
        make_job(account_id=2),
    ],
)
def test_search_handler_rejects_wrong_job_before_opening_browser(
    monkeypatch: pytest.MonkeyPatch,
    job: AutomationJobRecord,
) -> None:
    handler, cycle, browsers, login_calls = prepare_handler(
        monkeypatch,
        LoginStatus.AUTHENTICATED,
    )

    with pytest.raises(ValueError):
        handler(job)

    assert not browsers
    assert not login_calls
    assert not cycle.calls


@pytest.mark.parametrize(
    "status",
    [
        LoginStatus.CREDENTIALS_REQUIRED,
        LoginStatus.CONFIRMATION_REQUIRED,
        LoginStatus.CAPTCHA_REQUIRED,
        LoginStatus.INVALID_CREDENTIALS,
        LoginStatus.MANUAL_ACTION_REQUIRED,
    ],
)
def test_search_handler_blocks_when_login_needs_user_action(
    monkeypatch: pytest.MonkeyPatch,
    status: LoginStatus,
) -> None:
    browser_lock = threading.Lock()
    handler, cycle, browsers, login_calls = prepare_handler(
        monkeypatch,
        status,
        browser_lock=browser_lock,
    )

    with pytest.raises(AutomationJobBlocked) as raised:
        handler(make_job())

    assert raised.value.code == status.value.upper()
    assert str(raised.value) == handler._login_message(status)
    assert len(browsers) == 1
    assert browsers[0].entered
    assert browsers[0].exited
    assert login_calls == [(1, browsers[0])]
    assert not cycle.calls
    assert browser_lock.acquire(blocking=False)
    browser_lock.release()


def test_search_handler_runs_cycle_after_successful_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, cycle, browsers, login_calls = prepare_handler(
        monkeypatch,
        LoginStatus.AUTHENTICATED,
    )

    result = handler(make_job())

    assert result == {"found": 4, "queued": 2}
    assert len(browsers) == 1
    assert browsers[0].entered
    assert browsers[0].exited
    assert login_calls == [(1, browsers[0])]
    assert cycle.calls == [(1, 7, browsers[0], False)]
    settings = Settings(environment="test")
    assert browsers[0].arguments[0] == settings.browser_profile_dir(1)
    assert browsers[0].arguments[-1] == settings.hh_browser_timeout_ms
    journal = browsers[0].keyword_arguments.pop("journal")
    assert isinstance(journal, OperationJournal)
    assert journal.log_dir == settings.data_dir / "logs"
    assert browsers[0].keyword_arguments == {
        "start_minimized": True,
        "browser_channel": settings.hh_browser_channel,
        "browser_source_ip": (
            str(settings.hh_browser_source_ip)
            if settings.hh_browser_source_ip is not None
            else None
        ),
        "profile_lock_timeout_seconds": 2.0,
        "shared_endpoint": None,
    }


def test_search_preserves_captcha_source_from_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, cycle, browsers, _ = prepare_handler(monkeypatch, LoginStatus.AUTHENTICATED)
    url = "https://hh.ru/search/vacancy?text=Python&page=3"

    def blocked(**_kwargs: object) -> AutomationJobResult:
        raise HhSyncBlockedError("CAPTCHA_REQUIRED", "Проверка", verification_url=url)

    monkeypatch.setattr(cycle, "run", blocked)
    with pytest.raises(AutomationJobBlocked) as error:
        handler(make_job())
    assert error.value.code == "CAPTCHA_REQUIRED"
    assert error.value.verification_url == url
    assert browsers[0].exited


def test_search_preserves_retry_reason_and_delay_after_login_browser_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, cycle, browsers, _ = prepare_handler(monkeypatch, LoginStatus.AUTHENTICATED)

    def authenticate(_self: FakeLoginService, _account: int, _browser: object) -> LoginResult:
        raise HhSyncRetryableError("HH_BROWSER_CLOSED", "Браузер закрыт", retry_after_seconds=60)

    monkeypatch.setattr(FakeLoginService, "authenticate", authenticate)
    with pytest.raises(AutomationJobRetry) as raised:
        handler(make_job(last_result={"page_index": 4, "next_step": "details"}))

    assert raised.value.code == "HH_BROWSER_CLOSED"
    assert raised.value.retry_after_seconds == 60
    assert browsers[0].exited
    assert not cycle.calls


def test_search_forces_fresh_results_after_backlog_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, cycle, browsers, login_calls = prepare_handler(
        monkeypatch,
        LoginStatus.AUTHENTICATED,
    )

    result = handler(make_job(last_result={"backlog_processed": True, "queued": 1}))

    assert result == {"found": 4, "queued": 2}
    assert login_calls == [(1, browsers[0])]
    assert cycle.calls == [(1, 7, browsers[0], True)]


def test_search_waits_while_found_vacancies_are_being_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, cycle, browsers, login_calls = prepare_handler(
        monkeypatch,
        LoginStatus.AUTHENTICATED,
    )
    handler._application_work_pending = lambda: True

    with pytest.raises(AutomationJobDeferred) as raised:
        handler(make_job())

    assert raised.value.code == "APPLICATIONS_PENDING"
    assert raised.value.retry_after_seconds == 60
    assert not browsers
    assert not login_calls
    assert not cycle.calls


def test_search_quickly_defers_when_browser_profile_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_lock = threading.Lock()
    handler, cycle, browsers, login_calls = prepare_handler(
        monkeypatch,
        LoginStatus.AUTHENTICATED,
        browser_lock=browser_lock,
    )

    def fail_enter(_browser: FakeBrowser) -> FakeBrowser:
        raise RuntimeError("Профиль hh.ru занят другой задачей дольше допустимого времени")

    monkeypatch.setattr(FakeBrowser, "__enter__", fail_enter)

    with pytest.raises(AutomationJobDeferred) as raised:
        handler(make_job())

    assert raised.value.code == "BROWSER_PROFILE_BUSY"
    assert raised.value.retry_after_seconds == 15
    assert len(browsers) == 1
    assert not login_calls
    assert not cycle.calls
    assert browser_lock.acquire(blocking=False)
    browser_lock.release()


def test_search_does_not_wait_for_shared_browser_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_lock = threading.Lock()
    browser_lock.acquire()
    monkeypatch.setattr(
        search_worker_module,
        "_BACKGROUND_PROFILE_LOCK_TIMEOUT_SECONDS",
        0.01,
    )
    handler, cycle, browsers, login_calls = prepare_handler(
        monkeypatch,
        LoginStatus.AUTHENTICATED,
        browser_lock=browser_lock,
    )

    try:
        with pytest.raises(AutomationJobDeferred) as raised:
            handler(make_job())
    finally:
        browser_lock.release()

    assert raised.value.code == "BROWSER_PROFILE_BUSY"
    assert raised.value.retry_after_seconds == 15
    assert not browsers
    assert not login_calls
    assert not cycle.calls
