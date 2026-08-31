from __future__ import annotations

import random
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from hugin.adapters.codex_cli import configured_codex_cli_client
from hugin.adapters.credentials import WindowsCredentialStore
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import CareerDirectionModel
from hugin.diagnostics import OperationJournal, error_details
from hugin.domain.hh import HhApplyResult, HhApplyStatus
from hugin.domain.hh_sync import HhSyncBlockedError, HhSyncRetryableError
from hugin.domain.time import as_utc, day_start_utc
from hugin.services.application_automation import (
    ApplicationAutomationService,
    ApplyJob,
)
from hugin.services.cover_letter import CoverLetterService
from hugin.services.hh_login import HhLoginService, LoginStatus
from hugin.services.screening_forms import ScreeningDraftService, StoredScreeningSubmission

type ApplicationJobHandler = Callable[[ApplyJob], HhApplyResult]
type FormPreflightHandler = Callable[[ApplyJob], HhApplyResult]
type LetterQueuePreparer = Callable[[ApplyJob], int]


class ApplicationWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        account_id: int = 1,
        browser_lock: threading.Lock | None = None,
        poll_seconds: float = 5.0,
        job_handler: ApplicationJobHandler | None = None,
        form_preflight_handler: FormPreflightHandler | None = None,
        letter_preparer: LetterQueuePreparer | None = None,
        journal: OperationJournal | None = None,
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
        self._form_preflight_handler = form_preflight_handler or self._run_form_preflight
        self._letter_preparer = letter_preparer or self._prepare_letter
        self._journal = journal or OperationJournal(settings.data_dir)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._browser: VisibleHhBrowser | None = None
        self._browser_owner: int | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        starting = self._journal.start(
            "applications",
            "worker.lifecycle",
            action="start",
            account_id=self._account_id,
        )
        try:
            upgrade_database(self._settings)
            database = create_database(self._settings)
        except Exception as error:
            starting.fail(error)
            raise
        try:
            with database.sessions.begin() as session:
                recovered = ApplicationAutomationService(session).recover_interrupted()
        finally:
            database.close()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="hugin-application-queue",
            daemon=True,
        )
        self._thread.start()
        starting.succeed(recovered_tasks=recovered)

    def stop(self, timeout_seconds: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout_seconds)
        if thread is None or not thread.is_alive():
            self._close_browser()
        self._thread = None
        self._journal.record(
            "applications",
            "worker.lifecycle",
            status="completed",
            action="stop",
            account_id=self._account_id,
        )

    def run_once(self, now: datetime | None = None) -> bool:
        selected_at = as_utc(now or datetime.now(UTC))
        if not self._browser_lock.acquire(blocking=False):
            return False
        try:
            job, may_prepare_letters = self._claim(selected_at)
            if job is None:
                if not may_prepare_letters:
                    self._close_browser()
                    return False
                preparation_job = self._claim_form_preflight(selected_at)
                if preparation_job is None:
                    if not self.has_pending_work(selected_at):
                        self._close_browser()
                    return False
                self._process_form_preflight(
                    preparation_job,
                    selected_at,
                    now_is_fixed=now is not None,
                )
                return True

            self._process_application(job, selected_at, now_is_fixed=now is not None)
            return True
        finally:
            self._browser_lock.release()

    def _process_application(
        self,
        job: ApplyJob,
        selected_at: datetime,
        *,
        now_is_fixed: bool,
    ) -> None:
        task = getattr(job, "task", None)
        application = getattr(job, "application", None)
        vacancy = getattr(job, "vacancy", None)
        resume = getattr(job, "resume", None)
        run = self._journal.start(
            "applications",
            "apply",
            account_id=self._account_id,
            task_id=getattr(task, "id", None),
            application_id=getattr(application, "id", None),
            vacancy_id=getattr(vacancy, "hh_id", None),
            resume_id=getattr(resume, "id", None),
        )
        handler_error: Exception | None = None
        try:
            result = self._job_handler(job)
        except HhSyncRetryableError as error:
            self._close_browser()
            handler_error = error
            run.fail(error, result_status=HhApplyStatus.RETRYABLE_ERROR.value)
            result = HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                job.vacancy.source_url,
                str(error),
                retry_after_seconds=error.retry_after_seconds,
                retry_blocks_queue=True,
            )
        except HhSyncBlockedError as error:
            self._close_browser()
            handler_error = error
            status = self._apply_status_for_blocked_code(error.code)
            run.fail(error, result_status=status.value, error_code=error.code)
            result = HhApplyResult(
                status,
                job.vacancy.source_url,
                str(error),
            )
        except Exception as error:
            self._close_browser()
            handler_error = error
            run.fail(error, result_status=HhApplyStatus.UNKNOWN_RESULT.value)
            result = HhApplyResult(
                HhApplyStatus.UNKNOWN_RESULT,
                job.vacancy.source_url,
                f"Ошибка выполнения после начала отклика: {type(error).__name__}",
            )

        finished_at = selected_at if now_is_fixed else datetime.now(UTC)
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
        except Exception as error:
            if handler_error is None:
                run.fail(error, result_status=result.status.value, stage="record_result")
            raise
        finally:
            database.close()
        if handler_error is None:
            run.succeed(
                result_status=result.status.value,
                questions=len(result.questions),
                warnings=len(result.warnings),
                retry_after_seconds=result.retry_after_seconds,
                confirmation=result.confirmation[:1000],
                final_url=result.final_url[:1000],
            )

    def _process_form_preflight(
        self,
        job: ApplyJob,
        selected_at: datetime,
        *,
        now_is_fixed: bool,
    ) -> None:
        run = self._journal.start(
            "applications",
            "form_preflight",
            account_id=self._account_id,
            task_id=job.task.id,
            application_id=job.application.id,
            vacancy_id=job.vacancy.hh_id,
            resume_id=job.resume.id,
        )
        handler_error: Exception | None = None
        try:
            result = self._form_preflight_handler(job)
        except HhSyncRetryableError as error:
            handler_error = error
            run.fail(error, result_status=HhApplyStatus.RETRYABLE_ERROR.value)
            result = HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                job.vacancy.source_url,
                str(error),
                retry_after_seconds=error.retry_after_seconds,
                retry_blocks_queue=True,
            )
        except HhSyncBlockedError as error:
            handler_error = error
            status = self._apply_status_for_blocked_code(error.code)
            run.fail(error, result_status=status.value, error_code=error.code)
            result = HhApplyResult(
                status,
                job.vacancy.source_url,
                str(error),
            )
        except Exception as error:
            handler_error = error
            run.fail(error, result_status=HhApplyStatus.RETRYABLE_ERROR.value)
            result = HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                job.vacancy.source_url,
                f"Ошибка предварительной проверки формы: {type(error).__name__}",
            )
        if result.status is HhApplyStatus.UNKNOWN_RESULT:
            result = HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                result.final_url,
                "Предварительная проверка не нажимала кнопку отправки; проверка будет повторена",
                warnings=result.warnings,
                retry_after_seconds=result.retry_after_seconds,
                retry_blocks_queue=result.retry_blocks_queue,
            )

        finished_at = selected_at if now_is_fixed else datetime.now(UTC)
        automatic_form_ready = False
        plain_form_ready = (
            result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED and not result.questions
        )
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                service = ApplicationAutomationService(session)
                if plain_form_ready:
                    service.release_form_preflight(job, now=finished_at)
                elif result.status is HhApplyStatus.QUESTIONS_REQUIRED:
                    automatic_form_ready = service.record_form_preflight(
                        job,
                        result,
                        now=finished_at,
                    )
                else:
                    service.record_result(
                        job,
                        result,
                        apply_delay=None,
                        now=finished_at,
                    )
        except Exception as error:
            if handler_error is None:
                run.fail(error, result_status=result.status.value, stage="record_result")
            raise
        finally:
            database.close()

        if not plain_form_ready and not automatic_form_ready:
            if handler_error is None:
                run.succeed(
                    result_status=result.status.value,
                    questions=len(result.questions),
                    warnings=len(result.warnings),
                    retry_after_seconds=result.retry_after_seconds,
                )
            return

        run.succeed(
            result_status="FORM_PREFLIGHT_PASSED",
            questions=len(result.questions),
            warnings=len(result.warnings),
        )
        prepared_at = selected_at if now_is_fixed else datetime.now(UTC)
        self._prepare_after_form_preflight(
            job,
            prepared_at,
            now_is_fixed=now_is_fixed,
        )

    def _prepare_after_form_preflight(
        self,
        job: ApplyJob,
        selected_at: datetime,
        *,
        now_is_fixed: bool,
    ) -> None:
        run = self._journal.start(
            "applications",
            "cover_letters.prepare_candidate",
            account_id=self._account_id,
            task_id=job.task.id,
            application_id=job.application.id,
            vacancy_id=job.vacancy.hh_id,
            resume_id=job.resume.id,
        )
        if not self._prepare_exact_letter(job, selected_at):
            run.succeed(prepared=False)
            return
        claim_at = selected_at if now_is_fixed else datetime.now(UTC)
        prepared_job = self._claim_exact_prepared(job.task.id, claim_at)
        run.succeed(prepared=True, claimed=prepared_job is not None)
        if prepared_job is not None:
            self._process_application(
                prepared_job,
                claim_at,
                now_is_fixed=now_is_fixed,
            )

    def _claim(self, now: datetime) -> tuple[ApplyJob | None, bool]:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                service = ApplicationAutomationService(session)
                service.recover_expired_supervised(now)
                if not service.applications_enabled():
                    return None, False
                policy = service.policy()
                sent_today = service.applied_since(
                    self._account_id,
                    day_start_utc(policy.timezone_name, now),
                )
                if sent_today >= policy.daily_limit:
                    return None, False
                include_stretch = service.stretch_automation_enabled()
                return (
                    service.claim_next(
                        account_id=self._account_id,
                        require_cover_letter=True,
                        require_cover_letter_quality=True,
                        include_stretch=include_stretch,
                    ),
                    True,
                )
        finally:
            database.close()

    def _claim_form_preflight(self, now: datetime) -> ApplyJob | None:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                service = ApplicationAutomationService(session)
                if not service.applications_enabled():
                    return None
                include_stretch = service.stretch_automation_enabled()
                return service.claim_next_form_preflight(
                    account_id=self._account_id,
                    include_stretch=include_stretch,
                    require_cover_letter_quality=True,
                    now=now,
                )
        finally:
            database.close()

    def _claim_exact_prepared(self, task_id: int, now: datetime) -> ApplyJob | None:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                service = ApplicationAutomationService(session)
                if not service.applications_enabled():
                    return None
                return service.claim_exact_prepared(
                    account_id=self._account_id,
                    task_id=task_id,
                    include_stretch=service.stretch_automation_enabled(),
                    require_cover_letter_quality=True,
                    now=now,
                )
        finally:
            database.close()

    def _prepare_exact_letter(self, job: ApplyJob, selected_at: datetime) -> bool:
        try:
            prepared = self._letter_preparer(job)
        except (LookupError, RuntimeError, ValueError) as error:
            self._journal.record(
                "applications",
                "cover_letters.prepare",
                status="failed",
                level="ERROR",
                account_id=self._account_id,
                vacancy_id=job.vacancy.hh_id,
                retry_in_minutes=15,
                **error_details(error),
            )
            database = create_database(self._settings)
            try:
                with database.sessions.begin() as session:
                    ApplicationAutomationService(session).defer_letter_preparation(
                        job.task.id,
                        retry_at=selected_at + timedelta(minutes=15),
                    )
            finally:
                database.close()
            return False
        if prepared:
            self._journal.record(
                "applications",
                "cover_letters.prepare",
                status="completed",
                account_id=self._account_id,
                vacancy_id=job.vacancy.hh_id,
                prepared=prepared,
            )
            return True
        return False

    def _prepare_letter(self, job: ApplyJob) -> int:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                automation = ApplicationAutomationService(session)
                if not automation.applications_enabled():
                    return 0
                if job.application.direction_id is None:
                    raise RuntimeError("Направление отклика отсутствует")
                direction_name = session.scalar(
                    select(CareerDirectionModel.name).where(
                        CareerDirectionModel.id == job.application.direction_id,
                        CareerDirectionModel.account_id == job.application.account_id,
                        CareerDirectionModel.is_active.is_(True),
                    )
                )
                if direction_name is None:
                    raise LookupError("Активное направление отклика не найдено")
                include_stretch = automation.stretch_automation_enabled()
                client = configured_codex_cli_client(
                    self._settings,
                    operation="cover_letter",
                )
                quality_model = configured_codex_cli_client(
                    self._settings,
                    operation="cover_letter_quality_check",
                )
                if not automation.applications_enabled():
                    return 0
                result = CoverLetterService(
                    session,
                    client,
                    quality_model=quality_model,
                ).prepare(
                    account_id=job.application.account_id,
                    direction_name=direction_name,
                    vacancy_hh_id=job.vacancy.hh_id,
                    application_id=job.application.id,
                    limit=1,
                    include_stretch=include_stretch,
                )
                return result.generated + result.reused + result.already_ready
        finally:
            database.close()

    def _run_form_preflight(self, job: ApplyJob) -> HhApplyResult:
        browser = self._get_browser()
        login = HhLoginService(WindowsCredentialStore()).authenticate(
            self._account_id,
            browser,
        )
        if not login.authenticated:
            self._close_browser()
            return HhApplyResult(
                self._apply_status_for_login(login.status),
                job.vacancy.source_url,
                "Перед проверкой формы требуется завершить вход в hh.ru",
            )
        return browser.apply_to_vacancy(
            job.vacancy.source_url,
            expected_resume_hh_id=job.resume.hh_id,
            expected_resume_title=job.resume.title,
            cover_letter="",
            submit=False,
            submit_guard=None,
        )

    def _run_job(self, job: ApplyJob) -> HhApplyResult:
        application = getattr(job, "application", None)
        application_id = getattr(application, "id", None)
        submission = (
            self._auto_screening_submission(application_id)
            if isinstance(application_id, int)
            else None
        )
        browser = self._get_browser()
        login = HhLoginService(WindowsCredentialStore()).authenticate(
            self._account_id,
            browser,
        )
        if not login.authenticated:
            self._close_browser()
            return HhApplyResult(
                self._apply_status_for_login(login.status),
                job.vacancy.source_url,
                "Перед отправкой требуется завершить вход в hh.ru",
            )
        if not job.cover_letter:
            raise RuntimeError("Готовое сопроводительное письмо отсутствует")
        return browser.apply_to_vacancy(
            job.vacancy.source_url,
            expected_resume_hh_id=job.resume.hh_id,
            expected_resume_title=job.resume.title,
            cover_letter=job.cover_letter,
            submit=True,
            submit_guard=lambda: self._background_submission_is_allowed(
                job,
                submission,
            ),
            screening_submission=(submission.payload if submission is not None else None),
        )

    def _get_browser(self) -> VisibleHhBrowser:
        owner = threading.get_ident()
        if self._browser is not None:
            if self._browser_owner != owner:
                raise RuntimeError("Рабочая сессия браузера принадлежит другому потоку")
            if self._browser.is_open():
                return self._browser
            self._close_browser()

        browser = VisibleHhBrowser(
            self._settings.browser_profile_dir(self._account_id),
            self._settings.hh_login_url,
            self._settings.hh_resumes_url,
            self._settings.hh_search_url,
            self._settings.hh_browser_timeout_ms,
            start_minimized=True,
            browser_source_ip=(
                str(self._settings.hh_browser_source_ip)
                if self._settings.hh_browser_source_ip is not None
                else None
            ),
        )
        entered = browser.__enter__()
        self._browser = entered
        self._browser_owner = owner
        self._journal.record(
            "applications",
            "browser.session",
            status="completed",
            action="open",
            account_id=self._account_id,
        )
        return entered

    def _close_browser(self) -> None:
        browser = self._browser
        if browser is None:
            return
        owner = threading.get_ident()
        if self._browser_owner != owner:
            return
        self._browser = None
        self._browser_owner = None
        try:
            browser.__exit__(None, None, None)
        finally:
            self._journal.record(
                "applications",
                "browser.session",
                status="completed",
                action="close",
                account_id=self._account_id,
            )

    @staticmethod
    def _apply_status_for_login(status: LoginStatus) -> HhApplyStatus:
        statuses = {
            LoginStatus.CREDENTIALS_REQUIRED: HhApplyStatus.AUTH_REQUIRED,
            LoginStatus.CONFIRMATION_REQUIRED: HhApplyStatus.AUTH_REQUIRED,
            LoginStatus.CAPTCHA_REQUIRED: HhApplyStatus.CAPTCHA_REQUIRED,
            LoginStatus.ACCOUNT_WARNING: HhApplyStatus.ACCOUNT_WARNING,
            LoginStatus.INVALID_CREDENTIALS: HhApplyStatus.INVALID_CREDENTIALS,
            LoginStatus.MANUAL_ACTION_REQUIRED: HhApplyStatus.AUTH_REQUIRED,
        }
        return statuses[status]

    @staticmethod
    def _apply_status_for_blocked_code(code: str) -> HhApplyStatus:
        statuses = {
            "ACCOUNT_WARNING": HhApplyStatus.ACCOUNT_WARNING,
            "CAPTCHA_REQUIRED": HhApplyStatus.CAPTCHA_REQUIRED,
            "INVALID_CREDENTIALS": HhApplyStatus.INVALID_CREDENTIALS,
        }
        return statuses.get(code.strip().upper(), HhApplyStatus.AUTH_REQUIRED)

    def _background_submission_is_allowed(
        self,
        job: ApplyJob,
        submission: StoredScreeningSubmission | None = None,
    ) -> bool:
        if job.cover_letter_id is None or job.cover_letter_sha256 is None:
            return False
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                allowed = ApplicationAutomationService(session).background_submission_is_allowed(
                    job.task.id,
                    letter_id=job.cover_letter_id,
                    letter_sha256=job.cover_letter_sha256,
                    resume_hh_id=job.resume.hh_id,
                    resume_title=job.resume.title,
                )
                if not allowed or submission is None:
                    return allowed
                return ScreeningDraftService(session).auto_submission_allowed(submission)
        finally:
            database.close()

    def _auto_screening_submission(
        self,
        application_id: int,
    ) -> StoredScreeningSubmission | None:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                return ScreeningDraftService(session).get_auto_submission(application_id)
        finally:
            database.close()

    def _applications_enabled(self) -> bool:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                return ApplicationAutomationService(session).applications_enabled()
        finally:
            database.close()

    def has_pending_work(self, now: datetime | None = None) -> bool:
        selected_at = as_utc(now or datetime.now(UTC))
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                service = ApplicationAutomationService(session)
                if not service.applications_enabled():
                    return False
                policy = service.policy()
                if (
                    service.applied_since(
                        self._account_id,
                        day_start_utc(policy.timezone_name, selected_at),
                    )
                    >= policy.daily_limit
                ):
                    return False
                return service.has_pending_application_work(
                    account_id=self._account_id,
                    include_stretch=service.stretch_automation_enabled(),
                    include_scheduled=True,
                    now=selected_at,
                )
        finally:
            database.close()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception as error:
                    self._journal.record(
                        "applications",
                        "worker.loop",
                        status="failed",
                        level="ERROR",
                        account_id=self._account_id,
                        **error_details(error),
                    )
                self._stop.wait(self._poll_seconds)
        finally:
            self._close_browser()
