from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationStatusObservationModel,
)
from hugin.domain.applications import ApplicationEventType, ApplicationState
from hugin.domain.time import as_utc
from hugin.repositories.directions import AccountRepository
from hugin.services.application_outcomes import ApplicationOutcomeService


@dataclass(frozen=True, slots=True)
class OutcomeCohort:
    age_days: int
    applications: int
    invitations: int
    confirmed_interview_invitations: int
    scheduled_interviews: int
    rejections: int
    without_decision: int
    checked_after_window: int
    with_selection_snapshot: int
    checked_last_48_hours: int


@dataclass(frozen=True, slots=True)
class OutcomeVersionCohort:
    age_days: int
    rules_version: str | None
    applications: int
    invitations: int
    invitations_per_100: float | None
    confirmed_interview_invitations: int
    confirmed_interview_invitations_per_100: float | None
    with_outcome_context: int
    checked_after_window: int
    first_sent_at: datetime
    last_sent_at: datetime
    youngest_days: int
    oldest_days: int


@dataclass(frozen=True, slots=True)
class SearchOutcomes:
    measured_at: datetime
    sent_by_hugin: int
    imported_or_unattributed: int
    invitations: int
    confirmed_interview_invitations: int
    scheduled_interviews: int
    confirmed_rejection_reasons: dict[str, int]
    cohorts: tuple[OutcomeCohort, ...]
    limitations: tuple[str, ...]
    versions: tuple[OutcomeVersionCohort, ...] = ()
    comparison_limitations: tuple[str, ...] = ()


class SearchOutcomeService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def snapshot(self, account_id: int, *, now: datetime | None = None) -> SearchOutcomes:
        AccountRepository(self._session).get(account_id)
        measured_at = as_utc(now or datetime.now(UTC))
        events: dict[int, list[ApplicationEventModel]] = defaultdict(list)
        for event in self._session.scalars(
            select(ApplicationEventModel)
            .join(ApplicationModel)
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationEventModel.created_at <= measured_at,
            )
            .order_by(ApplicationEventModel.created_at, ApplicationEventModel.id)
        ):
            events[event.application_id].append(event)
        observations: dict[int, list[ApplicationStatusObservationModel]] = defaultdict(list)
        for observation in self._session.scalars(
            select(ApplicationStatusObservationModel)
            .join(ApplicationModel)
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationStatusObservationModel.checked_at <= measured_at,
                ApplicationStatusObservationModel.recorded_at <= measured_at,
            )
            .order_by(
                ApplicationStatusObservationModel.checked_at,
                ApplicationStatusObservationModel.id,
            )
        ):
            observations[observation.application_id].append(observation)
        sent: dict[int, ApplicationEventModel] = {}
        other_sends: set[int] = set()
        for app_id, history in events.items():
            for event in history:
                if event.event_type is not ApplicationEventType.APPLIED:
                    continue
                if (
                    event.payload.get("source") in {"hugin_send", "hugin_reconciliation"}
                    and event.payload.get("hh_status") == "APPLIED"
                ):
                    sent.setdefault(app_id, event)
                else:
                    other_sends.add(app_id)

        def witnessed_state(app_id: int, state: ApplicationState) -> bool:
            sent_at = as_utc(sent[app_id].created_at)
            return any(
                event.payload.get("state") == state.value and as_utc(event.created_at) >= sent_at
                for event in events[app_id]
            ) or any(
                item.state is state and as_utc(item.checked_at) >= sent_at
                for item in observations[app_id]
            )

        invited = {app_id for app_id in sent if witnessed_state(app_id, ApplicationState.INVITED)}
        outcomes = ApplicationOutcomeService(self._session).for_account(
            account_id, recorded_before=measured_at
        )
        confirmed_invited = {
            app_id
            for app_id, outcome in outcomes.items()
            if app_id in sent and outcome.interview_evidence.strip()
        }
        scheduled = {
            app_id
            for app_id, outcome in outcomes.items()
            if app_id in sent and outcome.interview_at is not None and outcome.interview_evidence
        }
        cohorts: list[OutcomeCohort] = []
        versions: list[OutcomeVersionCohort] = []
        for age in (14, 21):
            ids = {
                app_id
                for app_id, event in sent.items()
                if as_utc(event.created_at) <= measured_at - timedelta(days=age)
            }
            rejected = {
                app_id
                for app_id in ids
                if witnessed_state(app_id, ApplicationState.REJECTED)
                or (
                    app_id in outcomes
                    and outcomes[app_id].rejection_reason
                    and outcomes[app_id].rejection_evidence
                )
            }
            checked = {
                app_id
                for app_id in ids
                if any(
                    as_utc(item.checked_at) >= as_utc(sent[app_id].created_at) + timedelta(days=age)
                    for item in observations[app_id]
                )
            }
            cohorts.append(
                OutcomeCohort(
                    age_days=age,
                    applications=len(ids),
                    invitations=len(ids & invited),
                    confirmed_interview_invitations=len(ids & confirmed_invited),
                    scheduled_interviews=len(ids & scheduled),
                    rejections=len(rejected),
                    without_decision=len(ids - invited - rejected - confirmed_invited),
                    checked_after_window=len(checked),
                    with_selection_snapshot=sum(
                        bool(sent[app_id].payload.get("rules_version"))
                        and sent[app_id].payload.get("snapshot_missing") is False
                        for app_id in ids
                    ),
                    checked_last_48_hours=sum(
                        any(
                            measured_at - timedelta(hours=48) <= as_utc(item.checked_at)
                            for item in observations[app_id]
                        )
                        for app_id in ids
                    ),
                )
            )
            groups: dict[str | None, set[int]] = defaultdict(set)
            for app_id in ids:
                version = sent[app_id].payload.get("rules_version")
                groups[version if isinstance(version, str) and version else None].add(app_id)
            for version, group in sorted(groups.items(), key=lambda item: item[0] or ""):
                first = min(as_utc(sent[app_id].created_at) for app_id in group)
                last = max(as_utc(sent[app_id].created_at) for app_id in group)
                versions.append(
                    OutcomeVersionCohort(
                        age_days=age,
                        rules_version=version,
                        applications=len(group),
                        invitations=len(group & invited),
                        invitations_per_100=round(100 * len(group & invited) / len(group), 2),
                        confirmed_interview_invitations=len(group & confirmed_invited),
                        confirmed_interview_invitations_per_100=round(
                            100 * len(group & confirmed_invited) / len(group), 2
                        ),
                        with_outcome_context=sum(
                            _has_outcome_context(sent[app_id]) for app_id in group
                        ),
                        checked_after_window=len(group & checked),
                        first_sent_at=first,
                        last_sent_at=last,
                        youngest_days=(measured_at - last).days,
                        oldest_days=(measured_at - first).days,
                    )
                )
        reasons: Counter[str] = Counter()
        for app_id, outcome in outcomes.items():
            if app_id in sent and outcome.rejection_reason and outcome.rejection_evidence:
                reasons[outcome.rejection_reason] += 1
        return SearchOutcomes(
            measured_at=measured_at,
            sent_by_hugin=len(sent),
            imported_or_unattributed=len(other_sends - sent.keys()),
            invitations=len(invited),
            confirmed_interview_invitations=len(confirmed_invited),
            scheduled_interviews=len(scheduled),
            confirmed_rejection_reasons=dict(reasons),
            cohorts=tuple(cohorts),
            versions=tuple(versions),
            comparison_limitations=(
                "Это описательные группы. Разный возраст откликов, время отправки и состав "
                "вакансий могут объяснять разницу долей; рост эффекта Hugin пока не установлен.",
                "Для сопоставления нужны сохранённые условия вакансии, версия местного "
                "резюме, подтверждённые факты и письмо на момент попытки, а также история "
                "наблюдений. Старые пропуски не восстанавливаются из сегодняшнего профиля.",
                "Местная копия резюме не подтверждает неизменность резюме на hh.ru. "
                "Описание вакансии также берётся из местной копии; дата его чтения сохраняется. "
                "Перед проверкой влияния изменений нужно сверить внешние тексты.",
            ),
            limitations=(
                "В выборку входят только подтверждённые отправки Hugin. "
                "Импортированные отклики и отправки с неизвестным авторством показаны отдельно.",
                "Статус hh.ru, подтверждённое приглашение именно на собеседование и "
                "согласованная дата встречи показаны отдельно. Подтверждение приглашения "
                "на разговор с работодателем или техническим специалистом записывается "
                "пользователем в разделе общения; дата может быть ещё не назначена. "
                "Автоматический опрос и тестовое задание не считаются таким приглашением.",
                "Группы 14 и 21 день отсчитываются от подтверждения отправки и показывают "
                "последнее известное состояние, а не результат строго на 14-й или 21-й день. "
                "При восстановлении неизвестной отправки подтверждение может быть позже отклика.",
                "Отсутствие приглашения не доказывает нехватку опыта. Причина остаётся "
                "неизвестной без подтверждённого объяснения работодателя.",
                "Проверки статуса до появления истории наблюдений могли не сохраняться. "
                "Неполнота наблюдений ограничивает выводы о результате.",
            ),
        )


def _has_outcome_context(event: ApplicationEventModel) -> bool:
    context = event.payload.get("outcome_context")
    if not isinstance(context, dict) or context.get("schema_version") != 1:
        return False
    profile = context.get("profile")
    vacancy = context.get("vacancy")
    return (
        isinstance(profile, dict)
        and bool(profile.get("resume_content"))
        and bool(profile.get("resume_content_sha256"))
        and bool(profile.get("profile_facts_sha256"))
        and bool(context.get("letter_sha256"))
        and isinstance(vacancy, dict)
        and bool(vacancy.get("description"))
        and bool(vacancy.get("details_fetched_at"))
        and bool(vacancy.get("description_sha256"))
    )
