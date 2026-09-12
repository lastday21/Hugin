from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import Row, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationModel,
    ApplicationSettingsModel,
    ApplicationTaskModel,
    AutomationJobModel,
    BackgroundProcessRunModel,
    CareerDirectionModel,
    DirectionSearchQueryModel,
    DirectionVacancyModel,
    HhAccountModel,
    SemanticStageModel,
    SystemStateModel,
    VacancyModel,
)
from hugin.domain.applications import ApplicationState
from hugin.domain.automation import AutomationJobKind, AutomationJobState
from hugin.domain.tasks import SystemState, TaskState
from hugin.domain.time import as_utc, day_start_utc, timezone_by_name
from hugin.domain.vacancies import VacancyAvailability
from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.tasks import SystemStateRepository
from hugin.services.autonomy import AutonomyPolicyService
from hugin.services.vacancy_analysis import MAX_VACANCY_AGE, RULES_VERSION

ProcessKey = Literal["search", "evaluation", "applications", "synchronization", "replies"]
PROCESS_KEYS: tuple[ProcessKey, ...] = (
    "search",
    "evaluation",
    "applications",
    "synchronization",
    "replies",
)
PROCESS_NAMES = (
    "Поиск и загрузка",
    "Оценка вакансий",
    "Отклики",
    "Переписка и статусы",
    "Ответы работодателям",
)
STALE_AFTER = timedelta(minutes=10)


class BackgroundProcessService:
    def __init__(self, session: Session, account_id: int = 1) -> None:
        self._session = session
        self._account_id = account_id

    def _settings(self, *, lock: bool = False) -> ApplicationSettingsModel:
        statement = select(ApplicationSettingsModel).where(ApplicationSettingsModel.id == 1)
        if lock:
            statement = statement.with_for_update()
        return self._session.scalars(statement.execution_options(populate_existing=True)).one()

    def _blocked(self) -> str:
        if not self._session.scalar(
            select(HhAccountModel.is_active).where(HhAccountModel.id == self._account_id)
        ):
            return "Аккаунт hh.ru не настроен или выключен"
        state = SystemStateRepository(self._session)
        current = (
            self._session.scalars(
                select(SystemStateModel)
                .where(SystemStateModel.id == 1)
                .execution_options(populate_existing=True)
            )
            .one()
            .state
        )
        if current not in {SystemState.RUNNING, SystemState.PAUSED}:
            return f"Требуется действие пользователя: {current.value}"
        if state.supervised_lease_active():
            return "Выполняется отдельный разрешённый отклик"
        return ""

    def _configured(self, key: ProcessKey) -> bool:
        if key not in PROCESS_KEYS:
            raise ValueError("Неизвестный процесс")
        settings = self._settings()
        if key == "applications":
            state, recovery = self._session.execute(
                select(
                    SystemStateModel.state,
                    SystemStateModel.recovery_state,
                ).where(SystemStateModel.id == 1)
            ).one()
            return state == SystemState.RUNNING or (
                state not in {SystemState.RUNNING, SystemState.PAUSED}
                and recovery == SystemState.RUNNING
            )
        if key == "replies":
            policy = AutonomyPolicyService(self._session).get()
            return policy.auto_prepare_replies and policy.auto_send_approved_replies
        return bool(getattr(settings, f"{key}_enabled"))

    def enabled(self, key: ProcessKey) -> bool:
        return self._configured(key) and not self._blocked()

    def set_enabled(self, key: ProcessKey, enabled: bool) -> None:
        if key not in PROCESS_KEYS:
            raise ValueError("Неизвестный процесс")
        settings = self._settings(lock=True)
        if enabled and (reason := self._blocked()):
            raise ValueError(reason)
        if key == "applications":
            state = self._session.get(SystemStateModel, 1)
            if state is not None and state.state not in {SystemState.RUNNING, SystemState.PAUSED}:
                state.recovery_state = SystemState.PAUSED
            elif state is not None and state.state != (
                SystemState.RUNNING if enabled else SystemState.PAUSED
            ):
                SystemStateRepository(self._session).transition(
                    SystemState.RUNNING if enabled else SystemState.PAUSED
                )
        elif key == "replies":
            policy = AutonomyPolicyService(self._session)
            policy.update(
                {
                    **policy.get().as_payload(),
                    "auto_prepare_replies": enabled,
                    "auto_send_approved_replies": enabled,
                }
            )
        else:
            setattr(settings, f"{key}_enabled", enabled)
        if (
            key == "synchronization"
            and not enabled
            and self._session.get(HhAccountModel, self._account_id) is not None
        ):
            runtime = self._runtime(key)
            runtime.check_now_pending = False
            runtime.cancel_generation += 1
        self._session.flush()

    def stop_all(self) -> None:
        self._settings(lock=True)
        state = self._session.get(SystemStateModel, 1)
        if state is not None and state.supervised_lease_token:
            SystemStateRepository(self._session).release_supervised_lease(
                state.supervised_lease_token
            )
        for key in PROCESS_KEYS:
            self.set_enabled(key, False)

    def _runtime(self, key: ProcessKey) -> BackgroundProcessRunModel:
        if key not in PROCESS_KEYS:
            raise ValueError("Неизвестный процесс")
        self._session.execute(
            insert(BackgroundProcessRunModel)
            .values(
                account_id=self._account_id,
                key=key,
                state="waiting",
                reason="",
                runs=0,
                completed=0,
                check_now_pending=False,
                cancel_generation=0,
            )
            .on_conflict_do_nothing()
        )
        return self._session.scalars(
            select(BackgroundProcessRunModel)
            .where(
                BackgroundProcessRunModel.account_id == self._account_id,
                BackgroundProcessRunModel.key == key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()

    def request_check_now(self) -> None:
        self._settings(lock=True)
        if reason := self._blocked():
            raise ValueError(reason)
        self._runtime("synchronization").check_now_pending = True
        self._session.flush()

    def claim_check_now(self) -> bool:
        return self.claim_check_now_token() is not None

    def claim_check_now_token(self) -> int | None:
        if self._blocked():
            return None
        runtime = self._runtime("synchronization")
        token = runtime.cancel_generation if runtime.check_now_pending else None
        runtime.check_now_pending = False
        self._session.flush()
        return token

    def one_shot_allowed(self, token: int) -> bool:
        generation = self._session.scalar(
            select(BackgroundProcessRunModel.cancel_generation).where(
                BackgroundProcessRunModel.account_id == self._account_id,
                BackgroundProcessRunModel.key == "synchronization",
            )
        )
        return generation == token and not self._blocked()

    def set_schedule(self, message_interval_minutes: int, status_interval_minutes: int) -> None:
        if any(
            isinstance(value, bool) or not 1 <= value <= 1440
            for value in (message_interval_minutes, status_interval_minutes)
        ):
            raise ValueError("Интервал должен быть от 1 до 1440 минут")
        settings = self._settings(lock=True)
        settings.message_interval_minutes = message_interval_minutes
        settings.status_interval_minutes = status_interval_minutes
        self._session.flush()

    def started(self, key: ProcessKey, now: datetime | None = None) -> None:
        runtime = self._runtime(key)
        runtime.last_started_at = runtime.heartbeat_at = as_utc(now or datetime.now(UTC))
        runtime.state, runtime.reason = "running", ""
        runtime.runs += 1
        self._session.flush()

    def heartbeat(self, key: ProcessKey, now: datetime | None = None) -> None:
        self._runtime(key).heartbeat_at = as_utc(now or datetime.now(UTC))
        self._session.flush()

    def finished(
        self, key: ProcessKey, worked: bool, reason: str = "", now: datetime | None = None
    ) -> None:
        runtime = self._runtime(key)
        runtime.last_finished_at = runtime.heartbeat_at = as_utc(now or datetime.now(UTC))
        runtime.state = "waiting"
        runtime.reason = reason or ("Ход завершён" if worked else "Ожидает данных или срока")
        runtime.completed += int(worked)
        self._session.flush()

    def failed(self, key: ProcessKey, reason: str, now: datetime | None = None) -> None:
        self.finished(key, worked=False, reason=reason, now=now)
        self._runtime(key).state = "error"
        self._session.flush()

    def snapshot(self) -> dict[str, object]:
        settings = self._settings()
        funnel = self._funnel()
        now = datetime.now(UTC)
        runtimes = {
            row.key: row
            for row in self._session.scalars(
                select(BackgroundProcessRunModel).where(
                    BackgroundProcessRunModel.account_id == self._account_id
                )
            )
        }
        processes = []
        blocked = self._blocked()
        for key, name in zip(PROCESS_KEYS, PROCESS_NAMES, strict=True):
            enabled = self._configured(key)
            runtime = runtimes.get(key)
            state = "waiting" if enabled else "disabled"
            reason = "Ожидает данных или срока" if enabled else "Выключено пользователем"
            if runtime and runtime.state == "running":
                stale = runtime.heartbeat_at is None or (
                    datetime.now(UTC) - as_utc(runtime.heartbeat_at) > STALE_AFTER
                )
                state = "interrupted" if stale else ("running" if enabled else "stopping")
                reason = "Исполнитель не подтвердил работу" if stale else "Завершает текущий ход"
            elif enabled and runtime and runtime.reason:
                state, reason = runtime.state, runtime.reason
            if enabled and state == "waiting":
                if runtime is None or runtime.last_started_at is None:
                    reason = "Ожидает первого хода исполнителя"
                elif reason in {"", "Ожидает данных или срока", "Ход завершён"}:
                    reason = self._waiting_reason(key, settings, funnel, now)
            if (
                enabled
                and key == "replies"
                and runtime is not None
                and runtime.state != "running"
                and runtime.retry_after_at is not None
                and as_utc(runtime.retry_after_at) > now
            ):
                retry = (
                    f"Повтор ответа не раньше {self._local_time(runtime.retry_after_at, settings)}"
                )
                reason = f"{reason}. {retry}" if state == "error" else retry
            if blocked and enabled:
                state, reason = "blocked", blocked
            processes.append(
                {
                    "key": key,
                    "name": name,
                    "enabled": enabled,
                    "state": state,
                    "reason": reason,
                    "last_started_at": _iso(runtime.last_started_at) if runtime else None,
                    "last_finished_at": _iso(runtime.last_finished_at) if runtime else None,
                    "heartbeat_at": _iso(runtime.heartbeat_at) if runtime else None,
                    "runs": runtime.runs if runtime else 0,
                    "completed": runtime.completed if runtime else 0,
                }
            )
        sync = runtimes.get("synchronization")
        return {
            "processes": processes,
            "synchronization": {
                "message_interval_minutes": settings.message_interval_minutes,
                "status_interval_minutes": settings.status_interval_minutes,
                "check_now_pending": bool(sync and sync.check_now_pending),
            },
            "funnel": funnel,
            "last_search": self._last_search(),
        }

    @staticmethod
    def _local_time(value: datetime, settings: ApplicationSettingsModel) -> str:
        return (
            as_utc(value)
            .astimezone(timezone_by_name(settings.timezone_name))
            .strftime("%d.%m.%Y %H:%M %Z")
        )

    def _waiting_reason(
        self,
        key: ProcessKey,
        settings: ApplicationSettingsModel,
        funnel: dict[str, object],
        now: datetime,
    ) -> str:
        if key in {"search", "synchronization"}:
            return self._job_waiting_reason(key, settings, now)
        if key == "evaluation":
            stages = funnel.get("stages")
            pending = (
                next(
                    (
                        stage.get("count")
                        for stage in stages
                        if isinstance(stage, dict) and stage.get("key") == "awaiting_evaluation"
                    ),
                    None,
                )
                if isinstance(stages, list)
                else None
            )
            if pending == 0:
                return "Нет сохранённых вакансий для оценки"
            return "Ожидает хода исполнителя для оценки сохранённых вакансий"
        if key == "applications":
            sent = ApplicationRepository(self._session).count_applied_since(
                self._account_id, day_start_utc(settings.timezone_name, now)
            )
            if sent >= settings.hh_apply_daily_limit:
                return f"Достигнут дневной предел откликов ({settings.hh_apply_daily_limit})"
            next_apply = self._session.scalar(
                select(SystemStateModel.next_apply_at).where(SystemStateModel.id == 1)
            )
            if next_apply is not None and as_utc(next_apply) > now:
                return f"Следующая отправка не раньше {self._local_time(next_apply, settings)}"
            return "Ожидает готовых вакансий для подготовки и отправки отклика"
        return "Ожидает сообщений, требующих ответа"

    def _job_waiting_reason(
        self,
        key: ProcessKey,
        settings: ApplicationSettingsModel,
        now: datetime,
    ) -> str:
        statement = select(AutomationJobModel).where(
            AutomationJobModel.account_id == self._account_id
        )
        if key == "search":
            active_queries = (
                select(DirectionSearchQueryModel.id)
                .join(CareerDirectionModel)
                .where(
                    CareerDirectionModel.account_id == self._account_id,
                    CareerDirectionModel.is_active.is_(True),
                    DirectionSearchQueryModel.is_active.is_(True),
                    DirectionSearchQueryModel.area == "",
                )
            )
            if self._session.scalar(active_queries.limit(1)) is None:
                return "Нет активных поисковых запросов"
            statement = statement.where(
                AutomationJobModel.kind == AutomationJobKind.SEARCH,
                AutomationJobModel.search_query_id.in_(active_queries),
            )
        else:
            statement = statement.where(
                AutomationJobModel.kind.in_(
                    (AutomationJobKind.MESSAGES, AutomationJobKind.STATUSES)
                )
            )
        jobs = list(self._session.scalars(statement))
        if not jobs:
            return "Ожидает создания заданий исполнителем"
        completed = [job for job in jobs if job.last_finished_at is not None]
        latest = max(completed, key=lambda job: as_utc(job.last_finished_at or now), default=None)
        prefix = ""
        if latest is not None and latest.state in {
            AutomationJobState.FAILED,
            AutomationJobState.BLOCKED,
        }:
            cause = latest.last_error_message or latest.last_error_code or "причина не сохранена"
            prefix = f"Ошибка последнего хода: {cause}. "
        elif latest is not None and latest.last_result.get("deferred") is True:
            code = str(latest.last_result.get("reason") or "причина не сохранена")
            cause = {
                "BROWSER_PROFILE_BUSY": "профиль hh.ru занят",
                "APPLICATION_READY": "ожидание завершения отклика",
                "APPLICATIONS_PENDING": "ожидание завершения откликов",
            }.get(code, code)
            prefix = f"Последний ход отложен: {cause}. "
        scheduled = [
            job
            for job in jobs
            if job.next_run_at is not None
            and job.state in {AutomationJobState.WAITING, AutomationJobState.FAILED}
        ]
        if not scheduled:
            return prefix + "Нет назначенного срока; задания остановлены или ожидают исполнителя"
        next_job = min(scheduled, key=lambda job: as_utc(job.next_run_at or now))
        next_run = as_utc(next_job.next_run_at or now)
        if next_run <= now:
            return prefix + "Задание готово; ожидает хода исполнителя"
        label = {
            AutomationJobKind.SEARCH: "Следующий поиск",
            AutomationJobKind.MESSAGES: "Следующая проверка переписки",
            AutomationJobKind.STATUSES: "Следующая проверка статусов",
        }[next_job.kind]
        return prefix + f"{label}: {self._local_time(next_run, settings)}"

    def _funnel(self) -> dict[str, object]:
        rows = self._session.execute(
            select(
                DirectionVacancyModel,
                VacancyModel,
                CareerDirectionModel,
            )
            .join(VacancyModel, VacancyModel.id == DirectionVacancyModel.vacancy_id)
            .join(
                CareerDirectionModel, CareerDirectionModel.id == DirectionVacancyModel.direction_id
            )
            .where(
                CareerDirectionModel.account_id == self._account_id,
                CareerDirectionModel.is_active.is_(True),
            )
        ).all()
        grouped = defaultdict(list)
        for tracked, vacancy, direction in rows:
            grouped[vacancy.id].append((tracked, vacancy, direction))
        applications = (
            self._session.execute(
                select(
                    ApplicationModel.vacancy_id,
                    ApplicationModel.state,
                    ApplicationTaskModel.state,
                    ApplicationTaskModel.last_error_code,
                )
                .outerjoin(
                    ApplicationTaskModel, ApplicationTaskModel.application_id == ApplicationModel.id
                )
                .where(
                    ApplicationModel.account_id == self._account_id,
                    ApplicationModel.vacancy_id.in_(grouped),
                )
            ).all()
            if grouped
            else []
        )
        sent, uncertain = set(), set()
        for vacancy_id, application_state, task_state, error in applications:
            if application_state != ApplicationState.APPLYING or error == "ALREADY_APPLIED_ON_HH":
                sent.add(vacancy_id)
            if task_state in {
                TaskState.UNKNOWN_RESULT,
                TaskState.REVIEW_REQUIRED,
                TaskState.INPUT_REQUIRED,
            }:
                uncertain.add(vacancy_id)
        semantic = self.semantic_statuses(rows)
        labels = {
            "sent": "Отправлены",
            "unavailable": "Недоступны",
            "awaiting_details": "Ожидают описания",
            "awaiting_evaluation": "Ожидают оценки",
            "review": "Требуют решения",
            "ready": "Прошли отбор",
            "rejected": "Отсеяны",
        }
        counts = dict.fromkeys(labels, 0)
        oldest_publication = datetime.now(UTC) - MAX_VACANCY_AGE
        for vacancy_id, links in grouped.items():
            vacancy = links[0][1]
            if vacancy_id in sent:
                stage = "sent"
            elif vacancy.availability != VacancyAvailability.ACTIVE:
                stage = "unavailable"
            elif vacancy_id in uncertain:
                stage = "review"
            elif vacancy.duplicate_of_id is not None or (
                vacancy.published_at is not None
                and as_utc(vacancy.published_at) < oldest_publication
            ):
                stage = "rejected"
            elif vacancy.details_fetched_at is None:
                stage = "awaiting_details"
            else:
                states = []
                for tracked, _, direction in links:
                    category = tracked.rules_details.get("category")
                    status = semantic.get((direction.id, vacancy_id), "DISABLED")
                    if tracked.rules_version != RULES_VERSION or status == "PENDING":
                        states.append("awaiting_evaluation")
                    elif category in {"MATCH", "STRETCH"} and status in {"DISABLED", "ALLOW"}:
                        states.append("ready")
                    elif category in {"REJECTED", "ROUTED"} or status == "REJECT":
                        states.append("rejected")
                    else:
                        states.append("review")
                if "ready" in states:
                    stage = "ready"
                elif "awaiting_evaluation" in states:
                    stage = "awaiting_evaluation"
                elif "review" in states:
                    stage = "review"
                else:
                    stage = "rejected"
            counts[stage] += 1
        return {
            "total": len(grouped),
            "scope": "Уникальные вакансии активных направлений; "
            "подходит хотя бы одному направлению. Дубли и публикации старше 30 дней отсеяны. "
            "Пройденный отбор не означает подготовленного письма. "
            "Внешняя выдача сюда не входит.",
            "stages": [
                {"key": key, "name": name, "count": counts[key]} for key, name in labels.items()
            ],
        }

    def semantic_statuses(
        self, rows: Sequence[Row[tuple[DirectionVacancyModel, VacancyModel, CareerDirectionModel]]]
    ) -> dict[tuple[int, int], str]:
        from hugin.repositories.directions import _direction_record
        from hugin.repositories.vacancies import _to_record
        from hugin.services.decision_evidence import fingerprint
        from hugin.services.semantic_results import StoredSelection, decision_from_stored
        from hugin.services.semantic_snapshot import selection_snapshot, selection_source_lines

        relevant = [
            (tracked, vacancy, direction)
            for tracked, vacancy, direction in rows
            if isinstance(direction.scoring_config.get("semantic_selection"), dict)
            and direction.scoring_config["semantic_selection"].get("enabled", True)
            and vacancy.details_fetched_at is not None
            and tracked.rules_version == RULES_VERSION
        ]
        if not relevant:
            return {
                (direction.id, vacancy.id): "PENDING"
                for tracked, vacancy, direction in rows
                if isinstance(direction.scoring_config.get("semantic_selection"), dict)
                and direction.scoring_config["semantic_selection"].get("enabled", True)
            }
        stages = {}
        assessments: dict[int, list[SemanticStageModel]] = {}
        for row in self._session.scalars(
            select(SemanticStageModel)
            .where(
                SemanticStageModel.account_id == self._account_id,
                SemanticStageModel.vacancy_id.in_({vacancy.id for _, vacancy, _ in relevant}),
            )
            .order_by(SemanticStageModel.id)
        ):
            stages[(row.vacancy_id, row.cache_key)] = row
            if row.stage == "assess":
                assessments.setdefault(row.vacancy_id, []).append(row)
        templates, result = {}, {}
        for tracked, vacancy, direction in relevant:
            identity = (direction.id, vacancy.id)
            result[identity] = "PENDING"
            try:
                if direction.id not in templates:
                    templates[direction.id] = selection_snapshot(
                        self._session, _direction_record(direction), _to_record(vacancy)
                    )
                template = templates[direction.id]
                if template is None:
                    continue
                lines = selection_source_lines(
                    self._session,
                    self._account_id,
                    _to_record(vacancy),
                    previous_stages=assessments.get(vacancy.id, ()),
                )
                snapshot = replace(
                    template,
                    vacancy_id=vacancy.id,
                    lines=lines,
                    request={**template.request, "source": [line.model_dump() for line in lines]},
                )
                evidence = tracked.rules_details.get("semantic_selection")
                if not isinstance(evidence, dict) or evidence.get("key") != snapshot.key:
                    continue
                stage = stages.get((vacancy.id, snapshot.key))
                if (
                    stage is None
                    or stage.stage != "selection"
                    or fingerprint(stage.request) != snapshot.key
                    or fingerprint(stage.response_text) != stage.response_sha256
                ):
                    continue
                stored = StoredSelection.model_validate_json(stage.response_text)
                if stored.retryable:
                    continue
                result[identity] = decision_from_stored(snapshot, stored).status
                if tracked.rules_details.get("manual_override") == "ACCEPT":
                    result[identity] = "ALLOW"
            except ValueError:
                continue
        return result

    def _last_search(self) -> dict[str, object] | None:
        observations = []
        for job in self._session.scalars(
            select(AutomationJobModel).where(
                AutomationJobModel.account_id == self._account_id,
                AutomationJobModel.kind == AutomationJobKind.SEARCH,
            )
        ):
            result = job.last_result
            observed_at = result.get("observed_at")
            if not isinstance(observed_at, str):
                continue
            observations.append(
                {
                    "observed_at": observed_at,
                    "query": result.get("observed_query"),
                    "region": result.get("observed_region"),
                    "page": result.get("observed_page"),
                    "found": result.get("observed_found"),
                    "coverage_exhausted": result.get("coverage_exhausted"),
                    "coverage_page_limit": result.get("coverage_page_limit"),
                    "job_key": job.key,
                }
            )
        return max(observations, key=lambda item: str(item["observed_at"]), default=None)


def _iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value else None
