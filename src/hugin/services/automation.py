from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationSettingsModel,
    CareerDirectionModel,
    DirectionSearchQueryModel,
)
from hugin.domain.automation import (
    AutomationJobKind,
    AutomationJobRecord,
    AutomationJobResult,
    AutomationJobState,
)
from hugin.domain.time import as_utc
from hugin.repositories.automation import AutomationJobRepository

STALE_RUNNING_AFTER = timedelta(minutes=5)
FAILURE_RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
)


class AutomationSchedulerService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._jobs = AutomationJobRepository(session)

    def ensure_account_jobs(
        self,
        account_id: int,
        now: datetime | None = None,
        *,
        message_interval_minutes: int = 5,
        status_interval_minutes: int = 30,
    ) -> tuple[AutomationJobRecord, AutomationJobRecord]:
        if message_interval_minutes < 1 or status_interval_minutes < 1:
            raise ValueError("Интервалы фоновых проверок должны быть положительными")
        selected_at = self._now(now)
        messages = self._jobs.ensure(
            kind=AutomationJobKind.MESSAGES,
            account_id=account_id,
            interval_seconds=message_interval_minutes * 60,
            next_run_at=selected_at,
        )
        statuses = self._jobs.ensure(
            kind=AutomationJobKind.STATUSES,
            account_id=account_id,
            interval_seconds=status_interval_minutes * 60,
            next_run_at=selected_at,
        )
        return messages, statuses

    def ensure_configured_jobs(
        self,
        account_id: int,
        now: datetime | None = None,
    ) -> tuple[AutomationJobRecord, ...]:
        selected_at = self._now(now)
        settings = self._session.get(ApplicationSettingsModel, 1)
        if settings is None:
            raise LookupError("Настройки фоновых проверок не найдены")
        ensured: list[AutomationJobRecord] = list(
            self.ensure_account_jobs(
                account_id,
                selected_at,
                message_interval_minutes=settings.message_interval_minutes,
                status_interval_minutes=settings.status_interval_minutes,
            )
        )
        queries = tuple(
            self._session.scalars(
                select(DirectionSearchQueryModel)
                .join(
                    CareerDirectionModel,
                    CareerDirectionModel.id == DirectionSearchQueryModel.direction_id,
                )
                .where(
                    CareerDirectionModel.account_id == account_id,
                    CareerDirectionModel.is_active.is_(True),
                    DirectionSearchQueryModel.is_active.is_(True),
                )
                .order_by(DirectionSearchQueryModel.id)
            )
        )
        active_keys: set[str] = set()
        for query in queries:
            job = self.ensure_search_job(
                account_id=account_id,
                search_query_id=query.id,
                interval_minutes=query.schedule_minutes,
                now=selected_at,
            )
            if job.state is AutomationJobState.DISABLED:
                job = self.enable(job.key, selected_at)
            ensured.append(job)
            active_keys.add(job.key)

        for job in self.list_for_account(account_id):
            if (
                job.kind is AutomationJobKind.SEARCH
                and job.key not in active_keys
                and job.state is not AutomationJobState.DISABLED
            ):
                ensured.append(self.disable(job.key, selected_at))
        return tuple(ensured)

    def ensure_search_job(
        self,
        *,
        account_id: int,
        search_query_id: int,
        interval_minutes: int,
        now: datetime | None = None,
    ) -> AutomationJobRecord:
        if interval_minutes < 5:
            raise ValueError("Интервал поиска должен быть не меньше 5 минут")
        return self._jobs.ensure(
            kind=AutomationJobKind.SEARCH,
            account_id=account_id,
            search_query_id=search_query_id,
            interval_seconds=interval_minutes * 60,
            next_run_at=self._now(now),
        )

    def list_for_account(self, account_id: int) -> tuple[AutomationJobRecord, ...]:
        return self._jobs.list_for_account(account_id)

    def claim_due(self, now: datetime | None = None) -> AutomationJobRecord | None:
        return self._jobs.claim_due(self._now(now))

    def heartbeat(
        self,
        job_key: str,
        now: datetime | None = None,
    ) -> AutomationJobRecord:
        return self._jobs.heartbeat(job_key, self._now(now))

    def complete(
        self,
        job_key: str,
        result: AutomationJobResult | None = None,
        now: datetime | None = None,
    ) -> AutomationJobRecord:
        return self._jobs.complete(job_key, result, self._now(now))

    def fail(
        self,
        job_key: str,
        *,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
    ) -> AutomationJobRecord:
        failed_at = self._now(now)
        current = self._jobs.get(job_key)
        delay = self._retry_delay(current.consecutive_failures + 1)
        return self._jobs.fail(
            job_key,
            retry_at=failed_at + delay,
            error_code=error_code,
            error_message=error_message,
            now=failed_at,
        )

    def recover_stale(
        self,
        now: datetime | None = None,
        stale_after: timedelta = STALE_RUNNING_AFTER,
    ) -> tuple[AutomationJobRecord, ...]:
        if stale_after <= timedelta(0):
            raise ValueError("Порог зависшего задания должен быть положительным")
        recovered_at = self._now(now)
        stale = self._jobs.stale_running(recovered_at - stale_after)
        recovered: list[AutomationJobRecord] = []
        for job in stale:
            delay = self._retry_delay(job.consecutive_failures + 1)
            recovered.append(
                self._jobs.fail(
                    job.key,
                    retry_at=recovered_at + delay,
                    error_code="AUTOMATION_INTERRUPTED",
                    error_message="Предыдущий запуск фонового задания был прерван",
                    now=recovered_at,
                )
            )
        return tuple(recovered)

    def block(
        self,
        job_key: str,
        *,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
    ) -> AutomationJobRecord:
        return self._jobs.block(
            job_key,
            error_code=error_code,
            error_message=error_message,
            now=self._now(now),
        )

    def unblock(
        self,
        job_key: str,
        now: datetime | None = None,
    ) -> AutomationJobRecord:
        return self._jobs.unblock(job_key, self._now(now))

    def disable(
        self,
        job_key: str,
        now: datetime | None = None,
    ) -> AutomationJobRecord:
        return self._jobs.disable(job_key, self._now(now))

    def enable(
        self,
        job_key: str,
        now: datetime | None = None,
    ) -> AutomationJobRecord:
        return self._jobs.enable(job_key, self._now(now))

    @staticmethod
    def _retry_delay(failure_number: int) -> timedelta:
        index = min(max(failure_number, 1), len(FAILURE_RETRY_DELAYS)) - 1
        return FAILURE_RETRY_DELAYS[index]

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        return as_utc(value or datetime.now(UTC))
