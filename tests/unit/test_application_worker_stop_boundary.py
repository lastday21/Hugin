from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import hugin.workers.applications as applications
from hugin.domain.hh import HhApplyResult, HhApplyStatus
from hugin.services.application_automation import ApplyJob
from hugin.services.hh_login import LoginResult, LoginStatus
from tests.unit.test_application_worker import (
    FakeApplicationService,
    fake_job,
    prepare_worker,
)
from tests.unit.test_application_worker import (
    setup_function as reset_fake_service,
)


@pytest.fixture(autouse=True)
def clean_service() -> None:
    reset_fake_service()


@pytest.mark.parametrize("stop_stage", ["preflight", "letter"])
def test_stop_during_preparation_keeps_the_checked_task_without_sending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stop_stage: str
) -> None:
    job = fake_job()
    FakeApplicationService.preflight_job = job
    FakeApplicationService.prepared_job = job
    prepared: list[int] = []
    sent: list[int] = []

    def preflight(selected: ApplyJob) -> HhApplyResult:
        if stop_stage == "preflight":
            worker.stop()
        return HhApplyResult(HhApplyStatus.MANUAL_REVIEW_REQUIRED, selected.vacancy.source_url)

    def prepare_letter(selected: ApplyJob) -> int:
        prepared.append(selected.task.id)
        if stop_stage == "letter":
            worker.stop()
        return 1

    def apply(selected: ApplyJob) -> HhApplyResult:
        sent.append(selected.task.id)
        return HhApplyResult(HhApplyStatus.APPLIED, selected.vacancy.source_url)

    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        form_preflight_handler=preflight,
        letter_preparer=prepare_letter,
        job_handler=apply,
    )
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert worker.run_once(now)
    assert FakeApplicationService.released_preflights == [(job, now)]
    assert prepared == ([job.task.id] if stop_stage == "letter" else [])
    assert FakeApplicationService.prepared_job is job
    assert sent == []
    assert FakeApplicationService.recorded == []


def test_stop_revokes_the_submit_guard_even_when_global_queue_remains_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worker = prepare_worker(monkeypatch, tmp_path)
    job = fake_job()
    values = cast(SimpleNamespace, job)
    values.cover_letter_id = 15
    values.cover_letter_sha256 = "a" * 64
    assert worker._background_submission_is_allowed(job)
    worker.stop()
    assert FakeApplicationService.enabled
    assert not worker._background_submission_is_allowed(job)


def test_stopped_worker_does_not_claim_a_new_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job = fake_job()
    FakeApplicationService.job = job
    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        job_handler=lambda selected: HhApplyResult(
            HhApplyStatus.APPLIED, selected.vacancy.source_url
        ),
    )
    worker.stop()
    assert not worker.run_once()
    assert FakeApplicationService.job is job
    assert FakeApplicationService.recorded == []


def test_stop_after_claim_records_known_absence_of_submission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job = fake_job()
    sent: list[int] = []

    def apply(selected: ApplyJob) -> HhApplyResult:
        sent.append(selected.task.id)
        return HhApplyResult(HhApplyStatus.APPLIED, selected.vacancy.source_url)

    worker = prepare_worker(monkeypatch, tmp_path, job_handler=apply)

    def claim(_now: datetime) -> tuple[ApplyJob | None, bool]:
        worker.stop()
        return job, False

    monkeypatch.setattr(worker, "_claim", claim)
    assert worker.run_once()
    assert sent == []
    assert len(FakeApplicationService.recorded) == 1
    recorded_job, result, delay, _recorded_at = FakeApplicationService.recorded[0]
    assert recorded_job is job
    assert result.status is HhApplyStatus.RETRYABLE_ERROR
    assert delay is None


def test_stop_while_reading_permission_cannot_return_a_stale_allowance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worker = prepare_worker(monkeypatch, tmp_path)
    job = fake_job()
    values = cast(SimpleNamespace, job)
    values.cover_letter_id = 15
    values.cover_letter_sha256 = "a" * 64

    def allowed(_service: object, _task_id: int, **_values: object) -> bool:
        worker.stop()
        return True

    monkeypatch.setattr(FakeApplicationService, "background_submission_is_allowed", allowed)
    assert not worker._background_submission_is_allowed(job)


@pytest.mark.parametrize("preflight", [False, True])
def test_stop_during_login_does_not_open_or_submit_the_application_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, preflight: bool
) -> None:
    class Browser:
        def apply_to_vacancy(self, *_args: object, **_kwargs: object) -> HhApplyResult:
            pytest.fail("После остановки форма не должна открываться")

    class Login:
        def __init__(self, _store: object) -> None:
            pass

        def authenticate(self, _account_id: int, _browser: object) -> LoginResult:
            worker.stop()
            return LoginResult(LoginStatus.AUTHENTICATED)

    worker = prepare_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(worker, "_get_browser", Browser)
    monkeypatch.setattr(worker, "_auto_screening_submission", lambda _application_id: None)
    monkeypatch.setattr(applications, "HhLoginService", Login)
    result = worker._run_form_preflight(fake_job()) if preflight else worker._run_job(fake_job())
    assert result.status is HhApplyStatus.RETRYABLE_ERROR


@pytest.mark.parametrize("status", [HhApplyStatus.APPLIED, HhApplyStatus.UNKNOWN_RESULT])
def test_stop_after_external_action_preserves_its_actual_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: HhApplyStatus
) -> None:
    job = fake_job()
    FakeApplicationService.job = job
    calls: list[int] = []

    def apply(selected: ApplyJob) -> HhApplyResult:
        calls.append(selected.task.id)
        worker.stop()
        return HhApplyResult(status, selected.vacancy.source_url)

    worker = prepare_worker(monkeypatch, tmp_path, job_handler=apply)
    assert worker.run_once()
    assert not worker.run_once()
    assert calls == [job.task.id]
    assert len(FakeApplicationService.recorded) == 1
    assert FakeApplicationService.recorded[0][1].status is status


def test_other_thread_can_stop_a_slow_preflight_without_starting_submission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entered = threading.Event()
    release = threading.Event()
    job = fake_job()
    FakeApplicationService.preflight_job = job
    FakeApplicationService.prepared_job = job
    monkeypatch.setattr(applications, "upgrade_database", lambda _settings: None)

    def preflight(selected: ApplyJob) -> HhApplyResult:
        entered.set()
        assert release.wait(5)
        return HhApplyResult(HhApplyStatus.MANUAL_REVIEW_REQUIRED, selected.vacancy.source_url)

    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        form_preflight_handler=preflight,
        letter_preparer=lambda _job: pytest.fail("Подготовка после остановки запрещена"),
        job_handler=lambda _job: pytest.fail("Отправка после остановки запрещена"),
    )
    try:
        worker.start()
        assert entered.wait(5)
        thread = worker._thread
        assert thread is not None
        worker.stop(timeout_seconds=0.001)
        assert worker.running
        release.set()
        thread.join(5)
        assert not worker.running
        assert len(FakeApplicationService.released_preflights) == 1
        assert FakeApplicationService.prepared_job is job
        assert FakeApplicationService.recorded == []
    finally:
        release.set()
        worker.stop()
