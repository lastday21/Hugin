from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest

from hugin.core.settings import Settings
from hugin.domain.hh import HhApplyResult, HhApplyStatus
from hugin.services.application_automation import ApplyJob
from hugin.services.hh_login import LoginResult, LoginStatus
from hugin.workers import applications


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
    enabled: ClassVar[bool] = True
    sent_today: ClassVar[int] = 0
    daily_limit: ClassVar[int] = 25
    recorded: ClassVar[list[tuple[ApplyJob, HhApplyResult, timedelta | None, datetime]]] = []
    recovered: ClassVar[int] = 0
    expired_recovered: ClassVar[int] = 0
    expired_recovery_checks: ClassVar[list[datetime]] = []
    submission_checks: ClassVar[list[dict[str, object]]] = []

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
            daily_limit=self.daily_limit,
            delay_min_seconds=30,
            delay_max_seconds=60,
        )

    def applications_enabled(self) -> bool:
        return self.enabled

    def applied_since(self, _account_id: int, _since: datetime) -> int:
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
            "include_stretch": False,
        }
        selected = type(self).job
        type(self).job = None
        return selected

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
    job_handler: applications.ApplicationJobHandler | None = None,
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
        letter_preparer=letter_preparer or (lambda _account_id: 0),
        job_handler=job_handler,
    )


def setup_function() -> None:
    FakeApplicationService.job = None
    FakeApplicationService.enabled = True
    FakeApplicationService.sent_today = 0
    FakeApplicationService.daily_limit = 25
    FakeApplicationService.recorded = []
    FakeApplicationService.recovered = 0
    FakeApplicationService.expired_recovered = 0
    FakeApplicationService.expired_recovery_checks = []
    FakeApplicationService.submission_checks = []


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
    assert len(FakeApplicationService.recorded) == 1
    _, result, delay, recorded_at = FakeApplicationService.recorded[0]
    assert result.status is HhApplyStatus.APPLIED
    assert delay is not None
    assert 30 <= delay.total_seconds() <= 60
    assert recorded_at == now


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

    def prepare_letter(account_id: int) -> int:
        prepared_for.append(account_id)
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

    def prepare_letter(account_id: int) -> int:
        prepared_for.append(account_id)
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
        ) -> HhApplyResult:
            assert submit
            assert callable(submit_guard)
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


def test_worker_maps_incomplete_login_without_applying(
    monkeypatch: pytest.MonkeyPatch,
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
            return LoginResult(LoginStatus.CAPTCHA_REQUIRED)

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

    assert result.status is HhApplyStatus.CAPTCHA_REQUIRED


def test_worker_prepares_one_letter_for_active_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LetterSession:
        def scalars(self, _statement: object) -> tuple[str, ...]:
            return ("Python backend", "Другое ИТ")

        def expire_all(self) -> None:
            pass

    class LetterSessions:
        def begin(self) -> object:
            return nullcontext(LetterSession())

    class LetterDatabase:
        def __init__(self) -> None:
            self.sessions = LetterSessions()

        def close(self) -> None:
            pass

    class FakeLetterService:
        def __init__(self, _session: object, _client: object) -> None:
            pass

        def prepare(self, **values: object) -> SimpleNamespace:
            assert values == {
                "account_id": 1,
                "direction_name": "Python backend",
                "limit": 1,
                "include_stretch": False,
            }
            return SimpleNamespace(generated=1, reused=0)

    class FakeAutomation:
        def __init__(self, _session: object) -> None:
            pass

        def applications_enabled(self) -> bool:
            return True

    monkeypatch.setattr(applications, "create_database", lambda _settings: LetterDatabase())
    monkeypatch.setattr(applications, "ApplicationAutomationService", FakeAutomation)
    monkeypatch.setattr(
        applications,
        "AiPromptSettingsService",
        lambda _session: SimpleNamespace(
            get_model=lambda: "selected-model",
            get_reasoning_effort=lambda: "high",
        ),
    )
    monkeypatch.setattr(
        applications,
        "configured_yandex_ai_client",
        lambda _settings, *, model, reasoning_effort, operation: {
            ("selected-model", "high", "cover_letter"): object()
        }[(model, reasoning_effort, operation)],
    )

    monkeypatch.setattr(applications, "CoverLetterService", FakeLetterService)
    worker = applications.ApplicationWorker(
        Settings(environment="test"),
        letter_preparer=lambda _account_id: 0,
    )

    assert worker._prepare_letters(1) == 1


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
        "configured_yandex_ai_client",
        lambda *_args, **_kwargs: pytest.fail("модель не должна вызываться"),
    )

    assert worker._prepare_letters(1) == 0
