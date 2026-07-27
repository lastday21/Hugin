from __future__ import annotations

import random
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from hugin.adapters.credentials import WindowsCredentialStore
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import CareerDirectionModel
from hugin.domain.hh import HhApplyResult, HhApplyStatus
from hugin.domain.time import as_utc, local_day_start_utc, local_timezone_name
from hugin.services.application_automation import (
    ApplicationAutomationService,
    ApplyJob,
)
from hugin.services.cover_letter import CoverLetterService
from hugin.services.hh_login import HhLoginService, LoginStatus
from hugin.services.yandex_client import configured_yandex_ai_client

type ApplicationJobHandler = Callable[[ApplyJob], HhApplyResult]
type LetterQueuePreparer = Callable[[int], int]


class ApplicationWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        account_id: int = 1,
        browser_lock: threading.Lock | None = None,
        poll_seconds: float = 5.0,
        job_handler: ApplicationJobHandler | None = None,
        letter_preparer: LetterQueuePreparer | None = None,
    ) -> None:
        if account_id < 1:
            raise ValueError("Идентификатор аккаунта должен быть положительным")
        if poll_seconds <= 0:
            raise ValueError("Интервал проверки очереди должен быть положительным")
        self._settings = settings
        self._account_id = account_id
        self._browser_lock = browser_lock or threading.Lock()
        self._poll_seconds = poll_seconds
        self._job_handler = job_handler or self._run_job
        self._letter_preparer = letter_preparer or self._prepare_letters
        self._next_letter_attempt_at: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        upgrade_database(self._settings)
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                ApplicationAutomationService(session).recover_interrupted()
        finally:
            database.close()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="hugin-application-queue",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout_seconds)
        self._thread = None

    def run_once(self, now: datetime | None = None) -> bool:
        selected_at = as_utc(now or datetime.now(UTC))
        job = self._claim(selected_at)
        if job is None and self._may_prepare_letters(selected_at):
            try:
                prepared = self._letter_preparer(self._account_id)
            except (LookupError, RuntimeError, ValueError):
                self._next_letter_attempt_at = selected_at + timedelta(minutes=15)
            else:
                self._next_letter_attempt_at = (
                    selected_at + timedelta(minutes=5) if prepared == 0 else selected_at
                )
                if prepared:
                    job = self._claim(selected_at)
        if job is None:
            return False

        try:
            result = self._job_handler(job)
        except Exception as error:
            result = HhApplyResult(
                HhApplyStatus.UNKNOWN_RESULT,
                job.vacancy.source_url,
                f"Ошибка выполнения после начала отклика: {type(error).__name__}",
            )

        finished_at = selected_at if now is not None else datetime.now(UTC)
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                service = ApplicationAutomationService(session)
                policy = service.policy()
                apply_delay = (
                    timedelta(
                        seconds=random.uniform(
                            policy.delay_min_seconds,
                            policy.delay_max_seconds,
                        )
                    )
                    if result.status is HhApplyStatus.APPLIED
                    else None
                )
                service.record_result(
                    job,
                    result,
                    apply_delay=apply_delay,
                    now=finished_at,
                )
        finally:
            database.close()
        return True

    def _claim(self, now: datetime) -> ApplyJob | None:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                service = ApplicationAutomationService(session)
                local_now = now.astimezone()
                policy = service.policy(local_timezone_name(local_now))
                sent_today = service.applied_since(
                    self._account_id,
                    local_day_start_utc(local_now),
                )
                if sent_today >= policy.daily_limit:
                    return None
                return service.claim_next(
                    account_id=self._account_id,
                    require_cover_letter=True,
                )
        finally:
            database.close()

    def _prepare_letters(self, account_id: int) -> int:
        client = configured_yandex_ai_client(self._settings)
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                direction_names = tuple(
                    session.scalars(
                        select(CareerDirectionModel.name)
                        .where(
                            CareerDirectionModel.account_id == account_id,
                            CareerDirectionModel.is_active.is_(True),
                        )
                        .order_by(CareerDirectionModel.id)
                    )
                )
                prepared = 0
                for direction_name in direction_names:
                    result = CoverLetterService(session, client).prepare(
                        account_id=account_id,
                        direction_name=direction_name,
                        limit=1,
                    )
                    prepared += result.generated + result.reused
                    if prepared:
                        break
                return prepared
        finally:
            database.close()

    def _run_job(self, job: ApplyJob) -> HhApplyResult:
        with (
            self._browser_lock,
            VisibleHhBrowser(
                self._settings.browser_profile_dir(self._account_id),
                self._settings.hh_login_url,
                self._settings.hh_resumes_url,
                self._settings.hh_search_url,
                self._settings.hh_browser_timeout_ms,
            ) as browser,
        ):
            login = HhLoginService(WindowsCredentialStore()).authenticate(
                self._account_id,
                browser,
            )
            if not login.authenticated:
                statuses = {
                    LoginStatus.CREDENTIALS_REQUIRED: HhApplyStatus.AUTH_REQUIRED,
                    LoginStatus.CONFIRMATION_REQUIRED: HhApplyStatus.AUTH_REQUIRED,
                    LoginStatus.CAPTCHA_REQUIRED: HhApplyStatus.CAPTCHA_REQUIRED,
                    LoginStatus.INVALID_CREDENTIALS: HhApplyStatus.AUTH_REQUIRED,
                    LoginStatus.MANUAL_ACTION_REQUIRED: HhApplyStatus.AUTH_REQUIRED,
                }
                return HhApplyResult(
                    statuses[login.status],
                    job.vacancy.source_url,
                    "Перед отправкой требуется завершить вход в hh.ru",
                )
            if not job.cover_letter:
                raise RuntimeError("Готовое сопроводительное письмо отсутствует")
            return browser.apply_to_vacancy(
                job.vacancy.source_url,
                expected_resume_title=job.resume.title,
                cover_letter=job.cover_letter,
            )

    def _may_prepare_letters(self, now: datetime) -> bool:
        return self._next_letter_attempt_at is None or self._next_letter_attempt_at <= now

    def _run(self) -> None:
        while not self._stop.is_set():
            worked = self.run_once()
            if not worked:
                self._stop.wait(self._poll_seconds)
