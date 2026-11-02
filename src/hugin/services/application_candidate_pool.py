from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from hugin.core.settings import get_settings
from hugin.database.models import (
    ApplicationCandidatePoolModel,
    ApplicationModel,
    ApplicationSettingsModel,
    ApplicationTaskModel,
    CareerDirectionModel,
    DirectionResumeModel,
    DirectionSearchQueryModel,
    ResumeModel,
)
from hugin.domain.applications import ApplicationState
from hugin.domain.directions import VacancyState
from hugin.domain.tasks import TaskState
from hugin.domain.time import as_utc, day_start_utc
from hugin.repositories.automation import search_configuration_key
from hugin.repositories.tasks import SELECTION_RECOVERY_ERRORS, QueueTaskRepository
from hugin.services.autonomy import AutonomyPolicyService
from hugin.services.selection_status import semantic_statuses
from hugin.services.vacancy_analysis import RULES_VERSION


@dataclass(frozen=True)
class CandidatePoolStatus:
    identity: tuple[int, datetime, str]
    eligible_ids: frozenset[int]
    saved_ids: frozenset[int] | None
    reason: str | None
    eligible_paths: frozenset[tuple[int, int]]


class ApplicationCandidatePool:
    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def enabled() -> bool:
        return get_settings().application_pool_target > 0

    def status(
        self,
        account_id: int,
        now: datetime | None = None,
        *,
        candidate_ids: frozenset[int] | None = None,
    ) -> CandidatePoolStatus:
        from hugin.services.application_selection_gate import ApplicationSelectionGate

        selected_at = as_utc(now or datetime.now(UTC))
        settings = get_settings()
        options = self._session.get(ApplicationSettingsModel, 1)
        day = day_start_utc(options.timezone_name if options else "UTC", selected_at)
        directions = list(
            self._session.scalars(
                select(CareerDirectionModel).where(
                    CareerDirectionModel.account_id == account_id,
                    CareerDirectionModel.is_active.is_(True),
                )
            )
        )
        queries = self._session.scalars(
            select(DirectionSearchQueryModel)
            .join(CareerDirectionModel)
            .where(
                CareerDirectionModel.account_id == account_id,
                CareerDirectionModel.is_active.is_(True),
                DirectionSearchQueryModel.is_active.is_(True),
            )
        ).all()
        stretch = AutonomyPolicyService(self._session).get().auto_apply_stretch
        configuration = {
            "rules": RULES_VERSION,
            "target": settings.application_pool_target,
            "minimum": settings.application_pool_minimum,
            "stretch": stretch,
            "directions": sorted(
                (d.id, as_utc(d.updated_at).isoformat(), d.scoring_config) for d in directions
            ),
            "queries": sorted((q.id, search_configuration_key(q)) for q in queries),
        }
        key = hashlib.sha256(
            json.dumps(configuration, sort_keys=True, default=str).encode()
        ).hexdigest()
        identity = (account_id, day, key)
        saved = self._session.get(ApplicationCandidatePoolModel, identity)
        gate = ApplicationSelectionGate(self._session)
        categories = frozenset({"MATCH", "STRETCH"} if stretch else {"MATCH"})
        rows = gate._current_rows(
            account_id, selected_at, current_only=False, candidate_categories=categories
        )
        statuses = semantic_statuses(self._session, account_id, rows)
        resumable_directions = set(
            self._session.scalars(
                select(DirectionResumeModel.direction_id)
                .join(ResumeModel)
                .where(ResumeModel.account_id == account_id, ResumeModel.is_active.is_(True))
            )
        )
        blocked = set(
            self._session.scalars(
                select(ApplicationModel.vacancy_id)
                .outerjoin(
                    ApplicationTaskModel, ApplicationTaskModel.application_id == ApplicationModel.id
                )
                .where(
                    ApplicationModel.account_id == account_id,
                    or_(
                        ApplicationModel.state != ApplicationState.APPLYING,
                        and_(
                            ApplicationTaskModel.state.not_in(
                                (TaskState.PENDING, TaskState.RETRY_SCHEDULED)
                            ),
                            and_(
                                ApplicationTaskModel.last_error_code.in_(SELECTION_RECOVERY_ERRORS),
                                or_(
                                    ApplicationTaskModel.state == TaskState.SKIPPED,
                                    and_(
                                        ApplicationTaskModel.state == TaskState.REVIEW_REQUIRED,
                                        ApplicationTaskModel.last_error_code
                                        == "SEMANTIC_SELECTION_STALE",
                                    ),
                                ),
                            ).is_not(True),
                        ),
                    ),
                )
            )
        )
        eligible_paths = frozenset(
            (d.id, v.id)
            for t, v, d in rows
            if (candidate_ids is None or v.id in candidate_ids)
            and v.id not in blocked
            and v.duplicate_of_id is None
            and d.id in resumable_directions
            and t.state in {VacancyState.ANALYZED, VacancyState.QUEUED}
            and not gate._needs_details(v)
            and t.rules_version == RULES_VERSION
            and t.rules_details.get("category") in categories
            and statuses.get((d.id, v.id), "ALLOW") == "ALLOW"
        )
        eligible = frozenset(vacancy_id for _, vacancy_id in eligible_paths)
        saved_ids = frozenset(saved.vacancy_ids) if saved else None
        reason = None
        if saved is None:
            count = len(eligible)
            if count < settings.application_pool_target:
                if gate.fresh_search_pending(account_id, selected_at, include_paused=True):
                    reason = (
                        f"Накоплено {count}/{settings.application_pool_target} "
                        "подходящих вакансий; "
                        "свежий проход ещё не завершён"
                    )
                elif count < settings.application_pool_minimum:
                    reason = (
                        "Свежий проход завершён; "
                        f"накоплено {count}/{settings.application_pool_minimum} "
                        "подходящих вакансий для начала рассылки"
                    )
                elif pending := gate.blocking_reason(account_id, selected_at):
                    reason = f"Накоплено {count}/{settings.application_pool_target}; {pending}"
        return CandidatePoolStatus(identity, eligible, saved_ids, reason, eligible_paths)

    def admit(self, account_id: int, now: datetime) -> frozenset[int] | None:
        """Called only by a claimant holding the system-state lock."""
        if not self.enabled():
            return None
        categories = frozenset(
            {"MATCH", "STRETCH"}
            if AutonomyPolicyService(self._session).get().auto_apply_stretch
            else {"MATCH"}
        )
        tasks = QueueTaskRepository(self._session)
        for direction_id in self._session.scalars(
            select(CareerDirectionModel.id).where(
                CareerDirectionModel.account_id == account_id,
                CareerDirectionModel.is_active.is_(True),
            )
        ):
            tasks.skip_ineligible(
                direction_id,
                rules_version=RULES_VERSION,
                allowed_categories=categories,
                eligible_states=(VacancyState.ANALYZED, VacancyState.QUEUED),
            )
        status = self.status(account_id, now)
        if status.reason is not None:
            return frozenset()
        from hugin.services.application_automation import ApplicationAutomationService

        # The saved row latches the start threshold, not membership. Each claim ranks
        # the current evaluated supply, including candidates found after opening.
        prepared_ids = self._prepared_ids(account_id, status.eligible_paths)
        ApplicationAutomationService(self._session).prepare_vacancies_for_account_id(
            account_id=account_id,
            vacancy_ids=tuple(sorted(status.eligible_ids - prepared_ids)),
            include_stretch=AutonomyPolicyService(self._session).get().auto_apply_stretch,
            now=now,
        )
        # Candidates evaluated concurrently join on the next claim, so this finite
        # preparation step never waits for an endlessly growing search result.
        status = self.status(account_id, now, candidate_ids=status.eligible_ids)
        if status.reason is not None:
            return frozenset()
        missing = status.eligible_ids - self._prepared_ids(account_id, status.eligible_paths)
        if missing:
            raise RuntimeError(f"Ожидают подготовки задания для {len(missing)} кандидатов очереди")
        if status.saved_ids is None:
            self._session.add(
                ApplicationCandidatePoolModel(
                    account_id=account_id,
                    day_started_at=status.identity[1],
                    configuration_key=status.identity[2],
                    vacancy_ids=sorted(status.eligible_ids),
                    opened_at=as_utc(now),
                )
            )
        elif not status.eligible_ids.issubset(status.saved_ids):
            saved = self._session.get(ApplicationCandidatePoolModel, status.identity)
            assert saved is not None
            saved.vacancy_ids = sorted(status.saved_ids | status.eligible_ids)
        self._session.flush()
        return status.eligible_ids

    def _prepared_ids(
        self, account_id: int, eligible_paths: frozenset[tuple[int, int]]
    ) -> frozenset[int]:
        return frozenset(
            vacancy_id
            for vacancy_id, direction_id in self._session.execute(
                select(ApplicationModel.vacancy_id, ApplicationModel.direction_id)
                .join(ApplicationTaskModel)
                .join(ResumeModel, ResumeModel.id == ApplicationModel.resume_id)
                .join(
                    DirectionResumeModel,
                    and_(
                        DirectionResumeModel.direction_id == ApplicationModel.direction_id,
                        DirectionResumeModel.resume_id == ApplicationModel.resume_id,
                    ),
                )
                .where(
                    ApplicationModel.account_id == account_id,
                    ApplicationModel.state == ApplicationState.APPLYING,
                    ApplicationTaskModel.state.in_((TaskState.PENDING, TaskState.RETRY_SCHEDULED)),
                    ResumeModel.is_active.is_(True),
                )
            )
            if (direction_id, vacancy_id) in eligible_paths
        )
