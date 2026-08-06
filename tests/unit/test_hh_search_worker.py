from __future__ import annotations

import threading
from datetime import UTC, datetime
from types import TracebackType

import pytest

import hugin.workers.hh_search as search_worker_module
from hugin.core.settings import Settings
from hugin.domain import (
    AutomationJobKind,
    AutomationJobRecord,
    AutomationJobResult,
    AutomationJobState,
)
from hugin.services.hh_login import LoginResult, LoginStatus
from hugin.workers.automation import AutomationJobBlocked
from hugin.workers.hh_search import HhSearchJobHandler


def make_job(
    *,
    kind: AutomationJobKind = AutomationJobKind.SEARCH,
    account_id: int = 1,
    search_query_id: int | None = 7,
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
        last_result={},
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
        self.calls: list[tuple[int, int, object]] = []

    def run(
        self,
        *,
        account_id: int,
        search_query_id: int,
        browser: object,
    ) -> AutomationJobResult:
        self.calls.append((account_id, search_query_id, browser))
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
        assert detail_limit == 5
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
    assert cycle.calls == [(1, 7, browsers[0])]
    settings = Settings(environment="test")
    assert browsers[0].arguments[0] == settings.browser_profile_dir(1)
    assert browsers[0].arguments[-1] == settings.hh_browser_timeout_ms
    assert browsers[0].keyword_arguments == {
        "start_minimized": True,
        "browser_source_ip": (
            str(settings.hh_browser_source_ip)
            if settings.hh_browser_source_ip is not None
            else None
        ),
    }
