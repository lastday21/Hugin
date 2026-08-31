from __future__ import annotations

import threading
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest

import hugin.workers.applications as applications
from hugin.core.settings import Settings
from hugin.domain.hh import HhApplyResult, HhApplyStatus, HhScreeningSubmission
from hugin.domain.hh_sync import HhSyncBlockedError, HhSyncRetryableError
from hugin.services.application_automation import ApplyJob
from hugin.services.hh_login import LoginResult, LoginStatus
from hugin.services.screening_forms import StoredScreeningSubmission


class FakeSessions:
    def begin(self) -> object:
        return nullcontext(object())


class FakeDatabase:
    def __init__(self) -> None:
        self.sessions = FakeSessions()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeApplicationService:
    job: ClassVar[object | None] = None
    preflight_job: ClassVar[object | None] = None
    prepared_job: ClassVar[object | None] = None
    enabled: ClassVar[bool] = True
    sent_today: ClassVar[int] = 0
    daily_limit: ClassVar[int] = 25
    recorded: ClassVar[list[tuple[ApplyJob, HhApplyResult, timedelta | None, datetime]]] = []
    recovered: ClassVar[int] = 0
    expired_recovered: ClassVar[int] = 0
    expired_recovery_checks: ClassVar[list[datetime]] = []
    submission_checks: ClassVar[list[dict[str, object]]] = []
    released_preflights: ClassVar[list[tuple[ApplyJob, datetime]]] = []
    recorded_preflights: ClassVar[list[tuple[ApplyJob, HhApplyResult, datetime]]] = []
    automatic_form_ready: ClassVar[bool] = False
    day_starts: ClassVar[list[datetime]] = []
    deferred_letters: ClassVar[list[tuple[int, datetime]]] = []

    def __init__(self, _session: object) -> None:
        pass

    def recover_interrupted(self) -> int:
        type(self).recovered += 1
        return 0

    def recover_expired_supervised(self, now: datetime) -> int:
        type(self).expired_recovery_checks.append(now)
        return type(self).expired_recovered

    def policy(self, _timezone_name: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            timezone_name="UTC+05:00",
            daily_limit=self.daily_limit,
            delay_min_seconds=30,
            delay_max_seconds=60,
        )

    def applications_enabled(self) -> bool:
        return self.enabled

    def stretch_automation_enabled(self) -> bool:
        return True

    def applied_since(self, _account_id: int, since: datetime) -> int:
        type(self).day_starts.append(since)
        return self.sent_today

    def background_submission_is_allowed(
        self,
        task_id: int,
        **values: object,
    ) -> bool:
        type(self).submission_checks.append({"task_id": task_id, **values})
        return self.enabled

    def claim_next(self, **kwargs: object) -> object | None:
        assert kwargs == {
            "account_id": 1,
            "require_cover_letter": True,
            "require_cover_letter_quality": True,
            "include_stretch": True,
        }
        selected = type(self).job
        type(self).job = None
        return selected

    def claim_next_form_preflight(self, **kwargs: object) -> object | None:
        assert set(kwargs) == {
            "account_id",
            "include_stretch",
            "require_cover_letter_quality",
            "now",
        }
        assert kwargs["account_id"] == 1
        assert kwargs["include_stretch"] is True
        assert kwargs["require_cover_letter_quality"] is True
        assert isinstance(kwargs["now"], datetime)
        selected = type(self).preflight_job
        type(self).preflight_job = None
        return selected

    def release_form_preflight(self, job: ApplyJob, *, now: datetime) -> None:
        type(self).released_preflights.append((job, now))

    def claim_exact_prepared(self, **kwargs: object) -> object | None:
        assert set(kwargs) == {
            "account_id",
            "task_id",
            "include_stretch",
            "require_cover_letter_quality",
            "now",
        }
        assert kwargs["account_id"] == 1
        assert kwargs["include_stretch"] is True
        assert kwargs["require_cover_letter_quality"] is True
        selected = type(self).prepared_job
        type(self).prepared_job = None
        return selected

    def defer_letter_preparation(self, task_id: int, *, retry_at: datetime) -> bool:
        type(self).deferred_letters.append((task_id, retry_at))
        return True

    def record_form_preflight(
        self,
        job: ApplyJob,
        result: HhApplyResult,
        *,
        now: datetime,
    ) -> bool:
        type(self).recorded_preflights.append((job, result, now))
        return type(self).automatic_form_ready

    def record_result(
        self,
        job: ApplyJob,
        result: HhApplyResult,
        *,
        apply_delay: timedelta | None,
        now: datetime,
    ) -> None:
        self.recorded.append((job, result, apply_delay, now))


def prepare_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    browser_lock: threading.Lock | None = None,
    job_handler: applications.ApplicationJobHandler | None = None,
    form_preflight_handler: applications.FormPreflightHandler | None = None,
    letter_preparer: applications.LetterQueuePreparer | None = None,
) -> applications.ApplicationWorker:
    monkeypatch.setattr(applications, "create_database", lambda _settings: FakeDatabase())
    monkeypatch.setattr(
        applications,
        "ApplicationAutomationService",
        FakeApplicationService,
    )
    return applications.ApplicationWorker(
        Settings(environment="test", data_dir=tmp_path),
        browser_lock=browser_lock,
        letter_preparer=letter_preparer or (lambda _job: 0),
        job_handler=job_handler,
        form_preflight_handler=form_preflight_handler,
    )


def setup_function() -> None:
    FakeApplicationService.job = None
    FakeApplicationService.preflight_job = None
    FakeApplicationService.prepared_job = None
    FakeApplicationService.enabled = True
    FakeApplicationService.sent_today = 0
    FakeApplicationService.daily_limit = 25
    FakeApplicationService.recorded = []
    FakeApplicationService.recovered = 0
    FakeApplicationService.expired_recovered = 0
    FakeApplicationService.expired_recovery_checks = []
    FakeApplicationService.submission_checks = []
    FakeApplicationService.released_preflights = []
    FakeApplicationService.recorded_preflights = []
    FakeApplicationService.automatic_form_ready = False
    FakeApplicationService.day_starts = []
    FakeApplicationService.deferred_letters = []


def fake_job(vacancy_id: str = "101") -> ApplyJob:
    return cast(
        ApplyJob,
        SimpleNamespace(
            task=SimpleNamespace(id=10),
            application=SimpleNamespace(id=11, account_id=1, direction_id=12),
            vacancy=SimpleNamespace(
                id=13,
                hh_id=vacancy_id,
                source_url=f"https://hh.ru/vacancy/{vacancy_id}",
            ),
            resume=SimpleNamespace(id=14, hh_id="resume-1", title="Python backend"),
            direction_vacancy=SimpleNamespace(direction_id=12),
            cover_letter=None,
            cover_letter_id=None,
            cover_letter_sha256=None,
        ),
    )


def test_worker_processes_ready_application_and_records_delay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job = cast(
        ApplyJob,
        SimpleNamespace(
            vacancy=SimpleNamespace(source_url="https://hh.ru/vacancy/101"),
        ),
    )
    FakeApplicationService.job = job
    handled: list[object] = []

    def handle(selected: ApplyJob) -> HhApplyResult:
        handled.append(selected)
        return HhApplyResult(HhApplyStatus.APPLIED, "https://hh.ru/vacancy/101")

    worker = prepare_worker(monkeypatch, tmp_path, job_handler=handle)
    now = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

    assert worker.run_once(now)
    assert handled == [job]
    assert FakeApplicationService.day_starts == [datetime(2026, 7, 26, 19, 0, tzinfo=UTC)]
    assert len(FakeApplicationService.recorded) == 1
    _, result, delay, recorded_at = FakeApplicationService.recorded[0]
    assert result.status is HhApplyStatus.APPLIED
    assert delay is not None
    assert 30 <= delay.total_seconds() <= 60
    assert recorded_at == now


def test_worker_does_not_claim_application_while_browser_is_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    browser_lock = threading.Lock()
    browser_lock.acquire()
    job = cast(
        ApplyJob,
        SimpleNamespace(
            vacancy=SimpleNamespace(source_url="https://hh.ru/vacancy/101"),
        ),
    )
    FakeApplicationService.job = job
    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        browser_lock=browser_lock,
    )

    try:
        assert not worker.run_once(datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    finally:
        browser_lock.release()

    assert FakeApplicationService.job is job
    assert FakeApplicationService.recorded == []


def test_worker_does_not_claim_after_daily_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    FakeApplicationService.sent_today = 25
    FakeApplicationService.job = cast(
        ApplyJob,
        SimpleNamespace(
            vacancy=SimpleNamespace(source_url="https://hh.ru/vacancy/101"),
        ),
    )
    worker = prepare_worker(monkeypatch, tmp_path)

    assert not worker.run_once(datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    assert FakeApplicationService.recorded == []


def test_worker_does_not_prepare_letters_when_applications_are_paused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    FakeApplicationService.enabled = False
    prepared_for: list[int] = []

    def prepare_letter(job: ApplyJob) -> int:
        prepared_for.append(job.application.account_id)
        return 1

    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        letter_preparer=prepare_letter,
    )

    assert not worker.run_once(datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    assert prepared_for == []


def test_worker_checks_expired_supervised_task_while_queue_is_paused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    FakeApplicationService.enabled = False
    FakeApplicationService.expired_recovered = 1
    worker = prepare_worker(monkeypatch, tmp_path)
    now = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

    assert not worker.run_once(now)
    assert FakeApplicationService.expired_recovery_checks == [now]


def test_worker_does_not_prepare_letters_after_daily_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    FakeApplicationService.sent_today = FakeApplicationService.daily_limit
    prepared_for: list[int] = []

    def prepare_letter(job: ApplyJob) -> int:
        prepared_for.append(job.application.account_id)
        return 1

    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        letter_preparer=prepare_letter,
    )

    assert not worker.run_once(datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    assert prepared_for == []


def test_worker_marks_exception_after_claim_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job = cast(
        ApplyJob,
        SimpleNamespace(
            vacancy=SimpleNamespace(source_url="https://hh.ru/vacancy/101"),
        ),
    )
    FakeApplicationService.job = job

    def fail(_job: ApplyJob) -> HhApplyResult:
        raise RuntimeError("page closed")

    worker = prepare_worker(monkeypatch, tmp_path, job_handler=fail)

    assert worker.run_once(datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    _, result, delay, _ = FakeApplicationService.recorded[0]
    assert result.status is HhApplyStatus.UNKNOWN_RESULT
    assert delay is None


def test_worker_retries_login_timeout_without_marking_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job = fake_job()
    FakeApplicationService.job = job

    def fail_before_vacancy(_job: ApplyJob) -> HhApplyResult:
        raise HhSyncRetryableError(
            "HH_NETWORK_TIMEOUT",
            "Страница входа hh.ru временно недоступна",
            retry_after_seconds=60,
        )

    worker = prepare_worker(monkeypatch, tmp_path, job_handler=fail_before_vacancy)

    assert worker.run_once(datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    _, result, delay, _ = FakeApplicationService.recorded[0]
    assert result.status is HhApplyStatus.RETRYABLE_ERROR
    assert result.retry_after_seconds == 60
    assert result.retry_blocks_queue
    assert delay is None


def test_worker_promotes_account_warning_before_application(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job = fake_job()
    FakeApplicationService.job = job

    def fail_before_vacancy(_job: ApplyJob) -> HhApplyResult:
        raise HhSyncBlockedError(
            "ACCOUNT_WARNING",
            "hh.ru показал предупреждение безопасности",
        )

    worker = prepare_worker(monkeypatch, tmp_path, job_handler=fail_before_vacancy)

    assert worker.run_once(datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    assert FakeApplicationService.recorded[0][1].status is HhApplyStatus.ACCOUNT_WARNING


def test_clean_form_is_checked_then_letter_is_prepared_and_sent_in_one_cycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    preflight_job = fake_job()
    send_values = vars(cast(SimpleNamespace, preflight_job)).copy()
    send_values.update(
        cover_letter="Здравствуйте!",
        cover_letter_id=20,
        cover_letter_sha256="letter-sha",
    )
    send_job = cast(ApplyJob, SimpleNamespace(**send_values))
    FakeApplicationService.preflight_job = preflight_job
    prepared: list[ApplyJob] = []
    sent: list[ApplyJob] = []

    def prepare_letter(job: ApplyJob) -> int:
        prepared.append(job)
        FakeApplicationService.prepared_job = send_job
        return 1

    def send(job: ApplyJob) -> HhApplyResult:
        sent.append(job)
        return HhApplyResult(HhApplyStatus.APPLIED, job.vacancy.source_url)

    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        job_handler=send,
        form_preflight_handler=lambda job: HhApplyResult(
            HhApplyStatus.MANUAL_REVIEW_REQUIRED,
            job.vacancy.source_url,
        ),
        letter_preparer=prepare_letter,
    )
    now = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

    assert worker.run_once(now)
    assert FakeApplicationService.released_preflights == [(preflight_job, now)]
    assert prepared == [preflight_job]
    assert sent == [send_job]
    assert FakeApplicationService.recorded[0][1].status is HhApplyStatus.APPLIED


def test_automatic_queue_checks_form_before_preparing_letter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    preparation_job = fake_job()
    send_values = vars(cast(SimpleNamespace, preparation_job)).copy()
    send_values.update(
        cover_letter="Здравствуйте!",
        cover_letter_id=20,
        cover_letter_sha256="letter-sha",
    )
    send_job = cast(ApplyJob, SimpleNamespace(**send_values))
    FakeApplicationService.preflight_job = preparation_job
    events: list[str] = []
    prepared: list[ApplyJob] = []
    sent: list[ApplyJob] = []

    def prepare_letter(job: ApplyJob) -> int:
        events.append("letter")
        prepared.append(job)
        FakeApplicationService.prepared_job = send_job
        return 1

    def check_form(job: ApplyJob) -> HhApplyResult:
        events.append("form")
        return HhApplyResult(HhApplyStatus.MANUAL_REVIEW_REQUIRED, job.vacancy.source_url)

    def send(job: ApplyJob) -> HhApplyResult:
        events.append("send")
        sent.append(job)
        return HhApplyResult(HhApplyStatus.APPLIED, job.vacancy.source_url)

    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        job_handler=send,
        form_preflight_handler=check_form,
        letter_preparer=prepare_letter,
    )
    now = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

    assert worker.run_once(now)
    assert events == ["form", "letter", "send"]
    assert FakeApplicationService.released_preflights == [(preparation_job, now)]
    assert prepared == [preparation_job]
    assert sent == [send_job]


def test_form_with_questions_is_recorded_without_model_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job = fake_job()
    FakeApplicationService.preflight_job = job
    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        form_preflight_handler=lambda selected: HhApplyResult(
            HhApplyStatus.QUESTIONS_REQUIRED,
            selected.vacancy.source_url,
            questions=("Расскажите о проекте",),  # noqa: RUF001
        ),
        letter_preparer=lambda _job: pytest.fail("модель не должна вызываться"),
    )

    assert worker.run_once(datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    assert FakeApplicationService.released_preflights == []
    assert (
        FakeApplicationService.recorded_preflights[0][1].status is HhApplyStatus.QUESTIONS_REQUIRED
    )
    assert FakeApplicationService.recorded == []


def test_dangerous_form_is_not_treated_as_plain_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job = fake_job()
    FakeApplicationService.preflight_job = job
    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        form_preflight_handler=lambda selected: HhApplyResult(
            HhApplyStatus.MANUAL_REVIEW_REQUIRED,
            selected.vacancy.source_url,
            questions=("Укажите паспортные данные",),
        ),
        letter_preparer=lambda _job: pytest.fail("модель не должна вызываться"),
    )

    assert worker.run_once(datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    assert FakeApplicationService.released_preflights == []
    assert FakeApplicationService.recorded[0][1].status is HhApplyStatus.MANUAL_REVIEW_REQUIRED


def test_safe_form_preflight_prepares_letter_for_automatic_submission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job = fake_job()
    FakeApplicationService.preflight_job = job
    FakeApplicationService.automatic_form_ready = True
    prepared: list[ApplyJob] = []

    def prepare_letter(selected: ApplyJob) -> int:
        prepared.append(selected)
        return 1

    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        form_preflight_handler=lambda selected: HhApplyResult(
            HhApplyStatus.QUESTIONS_REQUIRED,
            selected.vacancy.source_url,
            questions=("Укажите Telegram",),
        ),
        letter_preparer=prepare_letter,
    )

    assert worker.run_once(datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    assert prepared == [job]
    assert FakeApplicationService.recorded_preflights[0][0] is job


def test_rejected_letter_does_not_delay_next_vacancy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        letter_preparer=lambda _job: 0,
    )
    now = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

    assert not worker._prepare_exact_letter(fake_job(), now)

    next_job = fake_job("102")
    FakeApplicationService.preflight_job = next_job
    worker._form_preflight_handler = lambda selected: HhApplyResult(
        HhApplyStatus.MANUAL_REVIEW_REQUIRED,
        selected.vacancy.source_url,
    )

    assert worker.run_once(now)


def test_letter_provider_failure_defers_only_current_vacancy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job = fake_job()
    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        letter_preparer=lambda _job: (_ for _ in ()).throw(RuntimeError("нет связи")),
    )
    now = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

    assert not worker._prepare_exact_letter(job, now)
    assert FakeApplicationService.deferred_letters == [(job.task.id, now + timedelta(minutes=15))]


def test_form_preflight_exception_is_retryable_not_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job = fake_job()
    FakeApplicationService.preflight_job = job

    def fail(_job: ApplyJob) -> HhApplyResult:
        raise RuntimeError("page closed")

    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        form_preflight_handler=fail,
        letter_preparer=lambda _job: pytest.fail("модель не должна вызываться"),
    )

    assert worker.run_once(datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    assert FakeApplicationService.recorded[0][1].status is HhApplyStatus.RETRYABLE_ERROR


def test_form_preflight_retryable_error_blocks_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job = fake_job()
    FakeApplicationService.preflight_job = job

    def fail(_job: ApplyJob) -> HhApplyResult:
        raise HhSyncRetryableError(
            "HH_NETWORK_TIMEOUT",
            "Страница входа hh.ru временно недоступна",
            retry_after_seconds=60,
        )

    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        form_preflight_handler=fail,
        letter_preparer=lambda _job: pytest.fail("модель не должна вызываться"),
    )

    assert worker.run_once(datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    result = FakeApplicationService.recorded[0][1]
    assert result.status is HhApplyStatus.RETRYABLE_ERROR
    assert result.retry_after_seconds == 60
    assert result.retry_blocks_queue


def test_form_preflight_unknown_result_is_downgraded_to_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job = fake_job()
    FakeApplicationService.preflight_job = job
    worker = prepare_worker(
        monkeypatch,
        tmp_path,
        form_preflight_handler=lambda selected: HhApplyResult(
            HhApplyStatus.UNKNOWN_RESULT,
            selected.vacancy.source_url,
        ),
        letter_preparer=lambda _job: pytest.fail("модель не должна вызываться"),
    )

    assert worker.run_once(datetime(2026, 7, 27, 10, 0, tzinfo=UTC))
    assert FakeApplicationService.recorded[0][1].status is HhApplyStatus.RETRYABLE_ERROR


def test_worker_recovers_interrupted_jobs_before_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = prepare_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(applications, "upgrade_database", lambda _settings: None)
    monkeypatch.setattr(worker, "_run", lambda: None)

    worker.start()
    worker.stop()

    assert FakeApplicationService.recovered == 1


def test_worker_runs_authenticated_browser_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, str, str]] = []

    class FakeBrowser:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeBrowser:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def apply_to_vacancy(
            self,
            source_url: str,
            *,
            expected_resume_hh_id: str,
            expected_resume_title: str,
            cover_letter: str,
            submit: bool,
            submit_guard: object,
            screening_submission: object,
        ) -> HhApplyResult:
            assert submit
            assert callable(submit_guard)
            assert screening_submission is None
            assert submit_guard()
            calls.append((source_url, expected_resume_hh_id, expected_resume_title, cover_letter))
            return HhApplyResult(HhApplyStatus.APPLIED, source_url)

    class FakeLoginService:
        def __init__(self, _store: object) -> None:
            pass

        def authenticate(self, _account_id: int, _browser: object) -> LoginResult:
            return LoginResult(LoginStatus.AUTHENTICATED)

    monkeypatch.setattr(applications, "VisibleHhBrowser", FakeBrowser)
    monkeypatch.setattr(applications, "HhLoginService", FakeLoginService)
    worker = prepare_worker(monkeypatch, tmp_path)
    job = cast(
        ApplyJob,
        SimpleNamespace(
            task=SimpleNamespace(id=10),
            vacancy=SimpleNamespace(source_url="https://hh.ru/vacancy/101"),
            resume=SimpleNamespace(hh_id="resume-1", title="Python backend"),
            cover_letter="Здравствуйте!",
            cover_letter_id=20,
            cover_letter_sha256="letter-sha",
        ),
    )

    result = worker._run_job(job)

    assert result.status is HhApplyStatus.APPLIED
    assert calls == [
        (
            "https://hh.ru/vacancy/101",
            "resume-1",
            "Python backend",
            "Здравствуйте!",
        )
    ]
    assert FakeApplicationService.submission_checks == [
        {
            "task_id": 10,
            "letter_id": 20,
            "letter_sha256": "letter-sha",
            "resume_hh_id": "resume-1",
            "resume_title": "Python backend",
        }
    ]


def test_worker_submits_safe_screening_form_after_final_recheck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = HhScreeningSubmission(
        "a" * 64,
        (("name:telegram", "@timur"),),
    )
    submission = StoredScreeningSubmission(
        form_id=7,
        application_id=11,
        payload=payload,
    )
    screening_checks: list[StoredScreeningSubmission] = []

    class FakeScreeningDraftService:
        def __init__(self, _session: object) -> None:
            pass

        def auto_submission_allowed(
            self,
            checked_submission: StoredScreeningSubmission,
        ) -> bool:
            screening_checks.append(checked_submission)
            return True

    class FakeBrowser:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeBrowser:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def apply_to_vacancy(
            self,
            source_url: str,
            *,
            expected_resume_hh_id: str,
            expected_resume_title: str,
            cover_letter: str,
            submit: bool,
            submit_guard: object,
            screening_submission: object,
        ) -> HhApplyResult:
            assert source_url == "https://hh.ru/vacancy/101"
            assert expected_resume_hh_id == "resume-1"
            assert expected_resume_title == "Python backend"
            assert cover_letter == "Здравствуйте!"
            assert submit
            assert screening_submission == payload
            assert callable(submit_guard)
            assert submit_guard()
            return HhApplyResult(HhApplyStatus.APPLIED, source_url)

    class FakeLoginService:
        def __init__(self, _store: object) -> None:
            pass

        def authenticate(self, _account_id: int, _browser: object) -> LoginResult:
            return LoginResult(LoginStatus.AUTHENTICATED)

    monkeypatch.setattr(applications, "VisibleHhBrowser", FakeBrowser)
    monkeypatch.setattr(applications, "HhLoginService", FakeLoginService)
    monkeypatch.setattr(applications, "ScreeningDraftService", FakeScreeningDraftService)
    worker = prepare_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(
        worker,
        "_auto_screening_submission",
        lambda application_id: submission if application_id == 11 else None,
    )
    job = cast(
        ApplyJob,
        SimpleNamespace(
            task=SimpleNamespace(id=10),
            application=SimpleNamespace(id=11),
            vacancy=SimpleNamespace(source_url="https://hh.ru/vacancy/101"),
            resume=SimpleNamespace(hh_id="resume-1", title="Python backend"),
            cover_letter="Здравствуйте!",
            cover_letter_id=20,
            cover_letter_sha256="letter-sha",
        ),
    )

    result = worker._run_job(job)

    assert result.status is HhApplyStatus.APPLIED
    assert screening_checks == [submission]
    assert FakeApplicationService.submission_checks == [
        {
            "task_id": 10,
            "letter_id": 20,
            "letter_sha256": "letter-sha",
            "resume_hh_id": "resume-1",
            "resume_title": "Python backend",
        }
    ]


def test_worker_checks_form_without_letter_or_submission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, str, str, bool, object]] = []

    class FakeBrowser:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeBrowser:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def apply_to_vacancy(
            self,
            source_url: str,
            *,
            expected_resume_hh_id: str,
            expected_resume_title: str,
            cover_letter: str,
            submit: bool,
            submit_guard: object,
        ) -> HhApplyResult:
            calls.append(
                (
                    source_url,
                    expected_resume_hh_id,
                    expected_resume_title,
                    cover_letter,
                    submit,
                    submit_guard,
                )
            )
            return HhApplyResult(HhApplyStatus.MANUAL_REVIEW_REQUIRED, source_url)

    class FakeLoginService:
        def __init__(self, _store: object) -> None:
            pass

        def authenticate(self, _account_id: int, _browser: object) -> LoginResult:
            return LoginResult(LoginStatus.AUTHENTICATED)

    monkeypatch.setattr(applications, "VisibleHhBrowser", FakeBrowser)
    monkeypatch.setattr(applications, "HhLoginService", FakeLoginService)
    worker = prepare_worker(monkeypatch, tmp_path)
    job = fake_job()

    result = worker._run_form_preflight(job)

    assert result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
    assert calls == [
        (
            "https://hh.ru/vacancy/101",
            "resume-1",
            "Python backend",
            "",
            False,
            None,
        )
    ]


@pytest.mark.parametrize(
    ("login_status", "apply_status"),
    (
        (LoginStatus.CAPTCHA_REQUIRED, HhApplyStatus.CAPTCHA_REQUIRED),
        (LoginStatus.INVALID_CREDENTIALS, HhApplyStatus.INVALID_CREDENTIALS),
    ),
)
def test_worker_maps_incomplete_login_without_applying(
    monkeypatch: pytest.MonkeyPatch,
    login_status: LoginStatus,
    apply_status: HhApplyStatus,
) -> None:
    class FakeBrowser:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeBrowser:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    class FakeLoginService:
        def __init__(self, _store: object) -> None:
            pass

        def authenticate(self, _account_id: int, _browser: object) -> LoginResult:
            return LoginResult(login_status)

    monkeypatch.setattr(applications, "VisibleHhBrowser", FakeBrowser)
    monkeypatch.setattr(applications, "HhLoginService", FakeLoginService)
    worker = applications.ApplicationWorker(
        Settings(environment="test"),
        letter_preparer=lambda _account_id: 0,
    )
    job = cast(
        ApplyJob,
        SimpleNamespace(
            vacancy=SimpleNamespace(source_url="https://hh.ru/vacancy/101"),
            resume=SimpleNamespace(title="Python backend"),
            cover_letter="Здравствуйте!",
        ),
    )

    result = worker._run_job(job)

    assert result.status is apply_status


def test_worker_prepares_one_letter_for_active_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LetterSession:
        def scalar(self, _statement: object) -> str:
            return "Python backend"

    class LetterSessions:
        def begin(self) -> object:
            return nullcontext(LetterSession())

    class LetterDatabase:
        def __init__(self) -> None:
            self.sessions = LetterSessions()

        def close(self) -> None:
            pass

    class FakeLetterService:
        def __init__(
            self,
            _session: object,
            _client: object,
            *,
            quality_model: object,
        ) -> None:
            assert quality_model is judge

        def prepare(self, **values: object) -> SimpleNamespace:
            assert values == {
                "account_id": 1,
                "direction_name": "Python backend",
                "vacancy_hh_id": "101",
                "application_id": 11,
                "limit": 1,
                "include_stretch": True,
            }
            return SimpleNamespace(generated=1, reused=0, already_ready=0)

    class FakeAutomation:
        def __init__(self, _session: object) -> None:
            pass

        def applications_enabled(self) -> bool:
            return True

        def stretch_automation_enabled(self) -> bool:
            return True

    monkeypatch.setattr(applications, "create_database", lambda _settings: LetterDatabase())
    monkeypatch.setattr(applications, "ApplicationAutomationService", FakeAutomation)
    client = object()
    judge = object()
    monkeypatch.setattr(
        applications,
        "configured_codex_cli_client",
        lambda _settings, *, operation: client if operation == "cover_letter" else judge,
    )

    monkeypatch.setattr(applications, "CoverLetterService", FakeLetterService)
    worker = applications.ApplicationWorker(
        Settings(environment="test"),
        letter_preparer=lambda _job: 0,
    )

    assert worker._prepare_letter(fake_job()) == 1


def test_worker_checks_current_application_state_before_submit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = prepare_worker(monkeypatch, tmp_path)

    assert worker._applications_enabled()
    FakeApplicationService.enabled = False
    assert not worker._applications_enabled()


def test_worker_does_not_create_model_client_while_paused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    FakeApplicationService.enabled = False
    worker = prepare_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(
        applications,
        "configured_codex_cli_client",
        lambda *_args, **_kwargs: pytest.fail("модель не должна вызываться"),
    )

    assert worker._prepare_letter(fake_job()) == 0
