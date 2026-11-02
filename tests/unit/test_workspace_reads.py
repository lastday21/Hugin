from __future__ import annotations

import json
import time
from threading import Event
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from httpx import Response
from sqlalchemy.orm import Session

from hugin.api.app import create_app
from hugin.core.settings import Settings
from hugin.database.models import ApplicationSettingsModel
from hugin.services.background_processes import BackgroundProcessService
from hugin.services.search_outcomes import SearchOutcomeService
from tests.unit.test_workspace_api import request, seed_workspace

pytestmark = pytest.mark.integration


def _finished(app: FastAPI, account: int, response: Response) -> Response:
    deadline = time.monotonic() + 5
    while response.status_code == 202 and time.monotonic() < deadline:
        job_id = response.json()["job_id"]
        response = request(app, "GET", f"/api/workspace-reads/{job_id}?account_id={account}")
        if response.status_code == 202:
            time.sleep(0.01)
    assert response.status_code != 202, "The isolated workspace read did not finish"
    return response


@pytest.mark.parametrize("kind", ["processes", "outcomes"])
def test_background_workspace_read_returns_early_shares_work_and_preserves_full_result(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    account, _, _ = seed_workspace(settings)
    app = create_app(settings)
    entered, release = Event(), Event()
    calls = 0
    with app.state.database.sessions() as session:
        expected = (
            BackgroundProcessService(session, account).snapshot()
            if kind == "processes"
            else SearchOutcomeService(session).snapshot(account)
        )

    def work(*_args: Any) -> object:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5), "The HTTP response waited for the background read"
        return expected

    service = BackgroundProcessService if kind == "processes" else SearchOutcomeService
    monkeypatch.setattr(service, "snapshot", work)
    path = f"/api/{kind}?account_id={account}&background=true"
    try:
        first = request(app, "GET", path)
        assert first.status_code == 202
        assert entered.wait(1)
        assert first.json()["kind"] == "workspace-read"
        assert first.json()["state"] == "running"
        second = request(app, "GET", path)
        assert second.status_code == 202 and second.json() == first.json()
        assert calls == 1
        job_id = first.json()["job_id"]
        assert (
            request(
                app, "GET", f"/api/workspace-reads/{job_id}?account_id={account + 1}"
            ).status_code
            == 404
        )
        assert request(app, "GET", "/api/workspace-reads/missing").status_code == 404
        assert request(app, "GET", f"/api/workspace-reads/{job_id}?account_id=0").status_code == 422
        release.set()
        result = _finished(app, account, first)
        assert result.status_code == 200 and result.json() == jsonable_encoder(expected)
        # Completion is available to old pollers, but a fresh start cannot reuse it.
        release.clear()
        newer = request(app, "GET", path)
        assert newer.status_code == 202 and newer.json()["job_id"] != job_id
        release.set()
        assert _finished(app, account, newer).json() == jsonable_encoder(expected)
        assert calls == 2
    finally:
        release.set()
        jobs = getattr(app.state, "workspace_reads", None)
        if jobs is not None:
            jobs.close()
        app.state.form_reconciliation_jobs.close()
        app.state.database.close()


def test_failed_background_workspace_read_returns_generic_error_and_can_retry(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, _, _ = seed_workspace(settings)
    app = create_app(settings)
    attempts = 0

    def work(*_args: Any) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("private database failure")
        return {"complete": True, "nested": [1, {"value": "full result"}]}

    monkeypatch.setattr(BackgroundProcessService, "snapshot", work)
    path = f"/api/processes?account_id={account}&background=true"
    try:
        failed = _finished(app, account, request(app, "GET", path))
        assert failed.status_code == 500
        assert "private database failure" not in failed.text
        failures = [
            entry
            for entry in app.state.journal.entries()
            if entry["event"] == "workspace.read" and entry["status"] == "failed"
        ]
        assert len(failures) == 1
        details = failures[0]["details"]
        assert details["kind"] == "processes" and details["account_id"] == account
        assert details["error_type"] == "RuntimeError"
        assert details["failure_evidence_saved"]
        evidence = json.loads(
            (settings.data_dir / f"evidence/models/{details['job_id']}-failure.json").read_text(
                "utf8"
            )
        )["payload"]
        assert evidence["error_message"] == "private database failure"
        assert "RuntimeError: private database failure" in evidence["traceback"]
        assert (
            request(
                app, "GET", f"/api/workspace-reads/{details['job_id']}?account_id={account}"
            ).status_code
            == 500
        )
        retried = _finished(app, account, request(app, "GET", path))
        assert retried.status_code == 200
        assert retried.json() == {"complete": True, "nested": [1, {"value": "full result"}]}
        assert attempts == 2
        assert (
            len(
                [
                    entry
                    for entry in app.state.journal.entries()
                    if entry["event"] == "workspace.read"
                ]
            )
            == 1
        )
    finally:
        jobs = getattr(app.state, "workspace_reads", None)
        if jobs is not None:
            jobs.close()
        app.state.form_reconciliation_jobs.close()
        app.state.database.close()


@pytest.mark.parametrize("form_reconciliation", [False, True])
def test_committed_write_starts_new_workspace_job_without_cancelling_old_pollers(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, form_reconciliation: bool
) -> None:
    account, _, _ = seed_workspace(settings)
    app = create_app(settings)
    entered, release = Event(), Event()
    calls = 0

    def snapshot(service: BackgroundProcessService) -> dict[str, object]:
        nonlocal calls
        calls += 1
        policy = service._session.get(ApplicationSettingsModel, 1)
        assert policy is not None
        value = policy.hh_apply_daily_limit
        if calls == 1:
            entered.set()
            assert release.wait(5)
        return {"limit": value}

    def reconcile(session: Session, _account: int) -> int:
        policy = session.get(ApplicationSettingsModel, 1)
        assert policy is not None
        policy.hh_apply_daily_limit += 1
        return 0

    monkeypatch.setattr(BackgroundProcessService, "snapshot", snapshot)
    monkeypatch.setattr("hugin.api.routes.workspace._reconcile_form_drafts", reconcile)
    path = f"/api/processes?account_id={account}&background=true"
    try:
        with app.state.database.sessions() as session:
            original = session.get(ApplicationSettingsModel, 1).hh_apply_daily_limit
        old = request(app, "GET", path)
        assert old.status_code == 202 and entered.wait(1)
        key = request(app, "GET", "/api/session").json()["key"]
        headers = {"X-Hugin-Session": key}
        if form_reconciliation:
            from tests.unit.test_form_reconciliation_jobs import _finished as finished_form

            job = request(
                app,
                "POST",
                f"/api/forms/reconcile?account_id={account}&background=true",
                headers=headers,
            ).json()
            assert finished_form(app, account, job["job_id"])["state"] == "completed"
        else:
            changed = request(
                app,
                "PUT",
                "/api/queue/settings",
                headers=headers,
                json={
                    "daily_limit": original + 1,
                    "delay_min_seconds": 30,
                    "delay_max_seconds": 70,
                },
            )
            assert changed.status_code == 200
        fresh = _finished(app, account, request(app, "GET", path))
        assert fresh.status_code == 200 and fresh.json() == {"limit": original + 1}
        assert calls == 2
        release.set()
        assert _finished(app, account, old).json() == {"limit": original}
    finally:
        release.set()
        app.state.form_reconciliation_jobs.close()
        app.state.workspace_reads.close()
        app.state.database.close()
