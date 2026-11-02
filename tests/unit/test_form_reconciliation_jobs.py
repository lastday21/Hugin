from __future__ import annotations

import time
from threading import Event
from typing import Any

import pytest
from sqlalchemy.orm import Session

from hugin.api.app import create_app
from hugin.api.concurrent_reads import ConcurrentReads
from hugin.core.settings import Settings
from hugin.database.models import ApplicationSettingsModel
from tests.unit.test_workspace_api import request, seed_workspace

pytestmark = pytest.mark.integration


def _status(app: Any, account: int, job_id: str) -> dict[str, Any]:
    result: dict[str, Any] = request(
        app, "GET", f"/api/forms/reconcile/{job_id}?account_id={account}"
    ).json()
    return result


def _finished(app: Any, account: int, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = _status(app, account, job_id)
        if result["state"] in {"completed", "failed"}:
            return result
        time.sleep(0.01)
    raise AssertionError("The isolated reconciliation did not finish")


def test_background_form_check_returns_before_work_and_two_clients_share_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, _, _ = seed_workspace(settings)
    entered, release = Event(), Event()
    calls: list[int] = []
    invalidations: list[bool] = []
    monkeypatch.setattr(ConcurrentReads, "invalidate", lambda _self: invalidations.append(True))

    def work(_session: Session, account_id: int) -> int:
        calls.append(account_id)
        entered.set()
        assert release.wait(10), "The API response waited for the background work"
        return 3

    monkeypatch.setattr("hugin.api.routes.workspace._reconcile_form_drafts", work, raising=False)
    app = create_app(settings)
    try:
        key = request(app, "GET", "/api/session").json()["key"]
        path = f"/api/forms/reconcile?account_id={account}&background=true"
        first = request(app, "POST", path, headers={"X-Hugin-Session": key}).json()
        assert isinstance(first, dict) and "job_id" in first
        assert entered.wait(1)
        assert first["state"] in {"pending", "running"}
        second = request(app, "POST", path, headers={"X-Hugin-Session": key}).json()
        assert second["job_id"] == first["job_id"]
        assert calls == [account]
        assert _status(app, account, first["job_id"])["state"] == "running"
        assert (
            request(
                app, "GET", f"/api/forms/reconcile/{first['job_id']}?account_id={account + 1}"
            ).status_code
            == 404
        )
        release.set()
        result = _finished(app, account, first["job_id"])
        assert result["state"] == "completed" and result["reconciled"] == 3
        assert result["error"] is None
        assert invalidations == []
    finally:
        release.set()
        jobs = getattr(app.state, "form_reconciliation_jobs", None)
        if jobs is not None:
            jobs.close()
        app.state.database.close()


def test_background_form_check_reports_failure_rolls_back_and_can_retry(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, _, _ = seed_workspace(settings)
    attempts = 0

    def work(session: Session, _account_id: int) -> int:
        nonlocal attempts
        attempts += 1
        policy = session.get(ApplicationSettingsModel, 1)
        assert policy is not None
        policy.hh_apply_daily_limit += 1
        session.flush()
        if attempts == 1:
            raise RuntimeError("isolated failure")
        return 1

    monkeypatch.setattr("hugin.api.routes.workspace._reconcile_form_drafts", work, raising=False)
    app = create_app(settings)
    try:
        with app.state.database.sessions() as session:
            original = session.get(ApplicationSettingsModel, 1).hh_apply_daily_limit
        key = request(app, "GET", "/api/session").json()["key"]
        path = f"/api/forms/reconcile?account_id={account}&background=true"
        first = request(app, "POST", path, headers={"X-Hugin-Session": key}).json()
        assert isinstance(first, dict) and "job_id" in first
        failed = _finished(app, account, first["job_id"])
        assert failed["state"] == "failed" and failed["error"]
        with app.state.database.sessions() as session:
            assert session.get(ApplicationSettingsModel, 1).hh_apply_daily_limit == original
        second = request(app, "POST", path, headers={"X-Hugin-Session": key}).json()
        assert second["job_id"] != first["job_id"]
        completed = _finished(app, account, second["job_id"])
        assert completed["state"] == "completed" and completed["reconciled"] == 1
        with app.state.database.sessions() as session:
            assert session.get(ApplicationSettingsModel, 1).hh_apply_daily_limit == original + 1
    finally:
        jobs = getattr(app.state, "form_reconciliation_jobs", None)
        if jobs is not None:
            jobs.close()
        app.state.database.close()
