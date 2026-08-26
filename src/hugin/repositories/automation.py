from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from hugin.database.models import (
    AutomationJobModel,
    CareerDirectionModel,
    DirectionSearchQueryModel,
)
from hugin.domain.automation import (
    AutomationJobKind,
    AutomationJobNotFoundError,
    AutomationJobRecord,
    AutomationJobResult,
    AutomationJobState,
    AutomationJobStateError,
    automation_job_key,
)
from hugin.domain.directions import DirectionScope
from hugin.domain.time import as_utc

CLAIMABLE_STATES = (AutomationJobState.WAITING, AutomationJobState.FAILED)


def _optional_utc(value: datetime | None) -> datetime | None:
    return as_utc(value) if value is not None else None


def _job_record(model: AutomationJobModel) -> AutomationJobRecord:
    return AutomationJobRecord(
        key=model.key,
        kind=model.kind,
        state=model.state,
        account_id=model.account_id,
        search_query_id=model.search_query_id,
        interval_seconds=model.interval_seconds,
        next_run_at=_optional_utc(model.next_run_at),
        last_started_at=_optional_utc(model.last_started_at),
        last_finished_at=_optional_utc(model.last_finished_at),
        last_success_at=_optional_utc(model.last_success_at),
        heartbeat_at=_optional_utc(model.heartbeat_at),
        consecutive_failures=model.consecutive_failures,
        last_error_code=model.last_error_code,
        last_error_message=model.last_error_message,
        last_result=cast(AutomationJobResult, dict(model.last_result)),
        created_at=as_utc(model.created_at),
        updated_at=as_utc(model.updated_at),
    )


class AutomationJobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def ensure(
        self,
        *,
        kind: AutomationJobKind,
        account_id: int,
        interval_seconds: int,
        next_run_at: datetime,
        search_query_id: int | None = None,
    ) -> AutomationJobRecord:
        if interval_seconds < 1:
            raise ValueError("Интервал фонового задания должен быть положительным")
        job_key = automation_job_key(kind, account_id, search_query_id)
        scheduled_at = as_utc(next_run_at)
        model = self._session.get(AutomationJobModel, job_key)
        if model is None:
            model = AutomationJobModel(
                key=job_key,
                kind=kind,
                state=AutomationJobState.WAITING,
                account_id=account_id,
                search_query_id=search_query_id,
                interval_seconds=interval_seconds,
                next_run_at=scheduled_at,
            )
            self._session.add(model)
        else:
            if (
                model.kind is not kind
                or model.account_id != account_id
                or model.search_query_id != search_query_id
            ):
                raise ValueError(f"Ключ фонового задания «{job_key}» уже занят")
            model.interval_seconds = interval_seconds
            if model.next_run_at is None and model.state in CLAIMABLE_STATES:
                model.next_run_at = scheduled_at
        self._session.flush()
        return _job_record(model)

    def get(self, job_key: str) -> AutomationJobRecord:
        return _job_record(self._model(job_key))

    def list_for_account(self, account_id: int) -> tuple[AutomationJobRecord, ...]:
        models = self._session.scalars(
            select(AutomationJobModel)
            .where(AutomationJobModel.account_id == account_id)
            .order_by(AutomationJobModel.kind, AutomationJobModel.key)
        )
        return tuple(_job_record(model) for model in models)

    def list_by_kind(
        self,
        kind: AutomationJobKind,
    ) -> tuple[AutomationJobRecord, ...]:
        models = self._session.scalars(
            select(AutomationJobModel)
            .where(AutomationJobModel.kind == kind)
            .order_by(AutomationJobModel.account_id, AutomationJobModel.key)
        )
        return tuple(_job_record(model) for model in models)

    def schedule_soon(
        self,
        *,
        kind: AutomationJobKind,
        account_id: int,
        run_at: datetime,
    ) -> AutomationJobRecord | None:
        scheduled_at = as_utc(run_at)
        job_key = automation_job_key(kind, account_id)
        model = self._session.get(AutomationJobModel, job_key)
        if model is None or model.state not in CLAIMABLE_STATES:
            return None
        if model.next_run_at is None or as_utc(model.next_run_at) > scheduled_at:
            model.next_run_at = scheduled_at
            model.updated_at = datetime.now(UTC)
            self._session.flush()
        return _job_record(model)

    def claim_due(
        self,
        now: datetime | None = None,
        *,
        search_enabled: bool = True,
    ) -> AutomationJobRecord | None:
        selected_at = as_utc(now or datetime.now(UTC))
        priority = case(
            (AutomationJobModel.kind == AutomationJobKind.MESSAGES, 0),
            (AutomationJobModel.kind == AutomationJobKind.STATUSES, 1),
            else_=2,
        )
        search_direction_priority = case(
            (
                CareerDirectionModel.scoring_config["role_scope"].as_string()
                == DirectionScope.PYTHON_BACKEND.value,
                0,
            ),
            else_=1,
        )
        statement = (
            select(AutomationJobModel)
            .outerjoin(
                DirectionSearchQueryModel,
                DirectionSearchQueryModel.id == AutomationJobModel.search_query_id,
            )
            .outerjoin(
                CareerDirectionModel,
                CareerDirectionModel.id == DirectionSearchQueryModel.direction_id,
            )
            .where(
                AutomationJobModel.state.in_(CLAIMABLE_STATES),
                AutomationJobModel.next_run_at.is_not(None),
                AutomationJobModel.next_run_at <= selected_at,
            )
            .order_by(
                priority,
                search_direction_priority,
                AutomationJobModel.next_run_at,
                AutomationJobModel.key,
            )
            .with_for_update(of=AutomationJobModel, skip_locked=True)
            .limit(1)
        )
        if not search_enabled:
            statement = statement.where(AutomationJobModel.kind != AutomationJobKind.SEARCH)
        model = self._session.scalar(statement)
        if model is None:
            return None
        model.state = AutomationJobState.RUNNING
        model.last_started_at = selected_at
        model.heartbeat_at = selected_at
        model.updated_at = selected_at
        self._session.flush()
        return _job_record(model)

    def heartbeat(self, job_key: str, now: datetime | None = None) -> AutomationJobRecord:
        selected_at = as_utc(now or datetime.now(UTC))
        model = self._running_model(job_key)
        model.heartbeat_at = selected_at
        model.updated_at = selected_at
        self._session.flush()
        return _job_record(model)

    def complete(
        self,
        job_key: str,
        result: AutomationJobResult | None = None,
        now: datetime | None = None,
    ) -> AutomationJobRecord:
        finished_at = as_utc(now or datetime.now(UTC))
        model = self._running_model(job_key)
        model.state = AutomationJobState.WAITING
        model.next_run_at = finished_at + timedelta(seconds=model.interval_seconds)
        model.last_finished_at = finished_at
        model.last_success_at = finished_at
        model.heartbeat_at = finished_at
        model.consecutive_failures = 0
        model.last_error_code = None
        model.last_error_message = None
        model.last_result = dict(result or {})
        model.updated_at = finished_at
        self._session.flush()
        return _job_record(model)

    def defer(
        self,
        job_key: str,
        *,
        run_at: datetime,
        result: AutomationJobResult | None = None,
        now: datetime | None = None,
    ) -> AutomationJobRecord:
        finished_at = as_utc(now or datetime.now(UTC))
        model = self._running_model(job_key)
        model.state = AutomationJobState.WAITING
        model.next_run_at = as_utc(run_at)
        model.last_finished_at = finished_at
        model.heartbeat_at = finished_at
        model.last_error_code = None
        model.last_error_message = None
        model.last_result = dict(result or {})
        model.updated_at = finished_at
        self._session.flush()
        return _job_record(model)

    def fail(
        self,
        job_key: str,
        *,
        retry_at: datetime,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
    ) -> AutomationJobRecord:
        finished_at = as_utc(now or datetime.now(UTC))
        model = self._running_model(job_key)
        model.state = AutomationJobState.FAILED
        model.next_run_at = as_utc(retry_at)
        model.last_finished_at = finished_at
        model.heartbeat_at = finished_at
        model.consecutive_failures += 1
        model.last_error_code = self._error_code(error_code)
        model.last_error_message = self._error_message(error_message)
        model.updated_at = finished_at
        self._session.flush()
        return _job_record(model)

    def block(
        self,
        job_key: str,
        *,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
    ) -> AutomationJobRecord:
        blocked_at = as_utc(now or datetime.now(UTC))
        model = self._model(job_key, for_update=True)
        if model.state is AutomationJobState.DISABLED:
            raise AutomationJobStateError(
                job_key,
                model.state,
                AutomationJobState.WAITING,
            )
        if model.state is AutomationJobState.RUNNING:
            model.last_finished_at = blocked_at
        model.state = AutomationJobState.BLOCKED
        model.next_run_at = None
        model.heartbeat_at = blocked_at
        model.last_error_code = self._error_code(error_code)
        model.last_error_message = self._error_message(error_message)
        model.updated_at = blocked_at
        self._session.flush()
        return _job_record(model)

    def unblock(self, job_key: str, now: datetime | None = None) -> AutomationJobRecord:
        unblocked_at = as_utc(now or datetime.now(UTC))
        model = self._model(job_key, for_update=True)
        if model.state is not AutomationJobState.BLOCKED:
            raise AutomationJobStateError(
                job_key,
                model.state,
                AutomationJobState.BLOCKED,
            )
        model.state = AutomationJobState.WAITING
        model.next_run_at = unblocked_at
        model.consecutive_failures = 0
        model.last_error_code = None
        model.last_error_message = None
        model.updated_at = unblocked_at
        self._session.flush()
        return _job_record(model)

    def disable(self, job_key: str, now: datetime | None = None) -> AutomationJobRecord:
        disabled_at = as_utc(now or datetime.now(UTC))
        model = self._model(job_key, for_update=True)
        if model.state is AutomationJobState.RUNNING:
            raise AutomationJobStateError(
                job_key,
                model.state,
                AutomationJobState.WAITING,
            )
        model.state = AutomationJobState.DISABLED
        model.next_run_at = None
        model.updated_at = disabled_at
        self._session.flush()
        return _job_record(model)

    def enable(self, job_key: str, now: datetime | None = None) -> AutomationJobRecord:
        enabled_at = as_utc(now or datetime.now(UTC))
        model = self._model(job_key, for_update=True)
        if model.state is not AutomationJobState.DISABLED:
            return _job_record(model)
        model.state = AutomationJobState.WAITING
        model.next_run_at = enabled_at
        model.consecutive_failures = 0
        model.last_error_code = None
        model.last_error_message = None
        model.updated_at = enabled_at
        self._session.flush()
        return _job_record(model)

    def stale_running(self, before: datetime) -> tuple[AutomationJobRecord, ...]:
        threshold = as_utc(before)
        models = self._session.scalars(
            select(AutomationJobModel)
            .where(
                AutomationJobModel.state == AutomationJobState.RUNNING,
                func.coalesce(
                    AutomationJobModel.heartbeat_at,
                    AutomationJobModel.last_started_at,
                    AutomationJobModel.updated_at,
                )
                < threshold,
            )
            .order_by(AutomationJobModel.key)
            .with_for_update(skip_locked=True)
        )
        return tuple(_job_record(model) for model in models)

    def _model(self, job_key: str, *, for_update: bool = False) -> AutomationJobModel:
        if for_update:
            model = self._session.scalar(
                select(AutomationJobModel)
                .where(AutomationJobModel.key == job_key)
                .with_for_update()
            )
        else:
            model = self._session.get(AutomationJobModel, job_key)
        if model is None:
            raise AutomationJobNotFoundError(job_key)
        return model

    def _running_model(self, job_key: str) -> AutomationJobModel:
        model = self._model(job_key, for_update=True)
        if model.state is not AutomationJobState.RUNNING:
            raise AutomationJobStateError(
                job_key,
                model.state,
                AutomationJobState.RUNNING,
            )
        return model

    @staticmethod
    def _error_code(value: str) -> str:
        compact = value.strip()[:64]
        return compact or "AUTOMATION_FAILED"

    @staticmethod
    def _error_message(value: str) -> str:
        compact = " ".join(value.split())[:1000]
        return compact or "Фоновое задание завершилось: ошибка"
