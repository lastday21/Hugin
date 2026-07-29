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
    sent_today: ClassVar[int] = 0
    daily_limit: ClassVar[int] = 25
    recorded: ClassVar[list[tuple[ApplyJob, HhApplyResult, timedelta | None, datetime]]] = []
    recovered: ClassVar[int] = 0

    def __init__(self, _session: object) -> None:
        pass

    def recover_interrupted(self) -> int:
        type(self).recovered += 1
        return 0

    def policy(self, _timezone_name: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            daily_limit=self.daily_limit,
            delay_min_seconds=30,
            delay_max_seconds=60,
        )

    def applied_since(self, _account_id: int, _since: datetime) -> int:
        return self.sent_today

    def claim_next(self, **kwargs: object) -> object | None:
        assert kwargs == {"account_id": 1, "require_cover_letter": True}
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
) -> applications.ApplicationWorker:
    monkeypatch.setattr(applications, "create_database", lambda _settings: FakeDatabase())
    monkeypatch.setattr(
        applications,
        "ApplicationAutomationService",
        FakeApplicationService,
    )
    return applications.ApplicationWorker(
        Settings(environment="test", data_dir=tmp_path),
        letter_preparer=lambda _account_id: 0,
        job_handler=job_handler,
    )


def setup_function() -> None:
    FakeApplicationService.job = None
    FakeApplicationService.sent_today = 0
    FakeApplicationService.daily_limit = 25
    FakeApplicationService.recorded = []
    FakeApplicationService.recovered = 0


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


def test_worker_runs_authenticated_browser_job(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str]] = []

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
            expected_resume_title: str,
            cover_letter: str,
        ) -> HhApplyResult:
            calls.append((source_url, expected_resume_title, cover_letter))
            return HhApplyResult(HhApplyStatus.APPLIED, source_url)

    class FakeLoginService:
        def __init__(self, _store: object) -> None:
            pass

        def authenticate(self, _account_id: int, _browser: object) -> LoginResult:
            return LoginResult(LoginStatus.AUTHENTICATED)

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

    assert result.status is HhApplyStatus.APPLIED
    assert calls == [
        (
            "https://hh.ru/vacancy/101",
            "Python backend",
            "Здравствуйте!",
        )
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
            }
            return SimpleNamespace(generated=1, reused=0)

        monkeypatch.setattr(applications, "create_database", lambda _settings: LetterDatabase())
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
            lambda _settings, *, model, reasoning_effort: {("selected-model", "high"): object()}[
                (model, reasoning_effort)
            ],
        )

    monkeypatch.setattr(applications, "CoverLetterService", FakeLetterService)
    worker = applications.ApplicationWorker(
        Settings(environment="test"),
        letter_preparer=lambda _account_id: 0,
    )

    assert worker._prepare_letters(1) == 1
