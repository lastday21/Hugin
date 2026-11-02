from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy.orm import Session

from hugin.api.app import create_app
from hugin.core.settings import Settings
from hugin.database.models import ApplicationSettingsModel
from tests.unit.test_form_reconciliation_jobs import _finished
from tests.unit.test_workspace_api import request, seed_workspace

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("background", [False, True])
def test_committed_writes_detach_older_reads(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, background: bool
) -> None:
    account, _, _ = seed_workspace(settings)
    app = create_app(settings)
    entered, release, release_work = Event(), Event(), Event()
    reads = app.state.concurrent_reads

    def current_limit() -> int:
        with app.state.database.sessions() as session:
            result: int = session.get(ApplicationSettingsModel, 1).hh_apply_daily_limit
            return result

    original = current_limit()

    def old_read() -> int:
        value = current_limit()
        entered.set()
        assert release.wait(5)
        return value

    def work(session: Session, _account: int) -> int:
        assert release_work.wait(5)
        policy = session.get(ApplicationSettingsModel, 1)
        assert policy is not None
        policy.hh_apply_daily_limit = original + 1
        return 0

    monkeypatch.setattr("hugin.api.routes.workspace._reconcile_form_drafts", work)
    key = request(app, "GET", "/api/session").json()["key"]
    headers = {"X-Hugin-Session": key}
    try:
        job = None
        if background:
            job = request(
                app,
                "POST",
                f"/api/forms/reconcile?account_id={account}&background=true",
                headers=headers,
            ).json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(reads.run, ("processes", account), old_read)
            try:
                assert entered.wait(2)
                if background:
                    assert job is not None
                    release_work.set()
                    assert _finished(app, account, job["job_id"])["state"] == "completed"
                else:
                    response = request(
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
                    assert response.status_code == 200
                fresh = pool.submit(reads.run, ("processes", account), current_limit)
                assert fresh.result(timeout=2) == original + 1
            finally:
                release.set()
            assert old.result(timeout=2) == original
    finally:
        release.set()
        release_work.set()
        app.state.form_reconciliation_jobs.close()
        app.state.database.close()
