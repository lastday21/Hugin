from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, true
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationStatusObservationModel,
    ApplicationTaskModel,
    DirectionVacancyModel,
    RecruiterMessageModel,
    VacancyModel,
)
from hugin.domain.applications import ApplicationEventType, ApplicationState, EventPayload
from hugin.domain.content import MessageDirection, RecruiterMessageState
from hugin.domain.tasks import TaskState
from hugin.domain.time import as_utc
from hugin.domain.vacancies import VacancyRecord
from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.application_exposure import application_profile_snapshot
from hugin.services.application_statistics import normalized_role_text
from hugin.services.decision_evidence import fingerprint, source_fingerprint
from hugin.services.vacancy_duplicates import VacancyDuplicateDetector

REAPPLICATION_INTERVAL = timedelta(days=30)
REAPPLICATION_WINDOW = timedelta(days=90)
REAPPLICATION_LIMIT = 2


@dataclass(frozen=True, slots=True)
class PreviousApplication:
    application_id: int
    hh_id: str
    confirmed_at: datetime
    state: ApplicationState
    last_reply_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReapplicationDecision:
    allowed: bool
    code: str | None = None
    reason: str = ""
    recheck_at: datetime | None = None
    previous_application_ids: tuple[int, ...] = ()
    review_key: str | None = None

    def payload(self) -> EventPayload:
        result: EventPayload = {
            "version": 1,
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "recheck_at": self.recheck_at.isoformat() if self.recheck_at else None,
            "previous_application_ids": list(self.previous_application_ids),
        }
        if self.review_key is not None:
            result["review_key"] = self.review_key
        return result


def decide_reapplication(
    history: tuple[PreviousApplication, ...],
    *,
    now: datetime,
    profile_changed: bool = False,
) -> ReapplicationDecision:
    if not history:
        return ReapplicationDecision(True)
    history = tuple(
        sorted(history, key=lambda item: (as_utc(item.confirmed_at), item.application_id))
    )
    ids = tuple(item.application_id for item in history)
    if any(
        item.state is ApplicationState.INVITED
        or (
            item.last_reply_at is not None
            and as_utc(item.last_reply_at) > now - REAPPLICATION_INTERVAL
            and item.state is not ApplicationState.REJECTED
        )
        for item in history
    ):
        return ReapplicationDecision(
            False,
            "REPOST_CONVERSATION",
            "По этой роли уже есть приглашение или недавняя переписка. Продолжить её; "
            "новый отклик пересмотреть после завершения разговора.",
            previous_application_ids=ids,
        )
    if profile_changed:
        return ReapplicationDecision(
            False,
            "REPOST_PROFILE_CHANGED",
            "Подтверждённый опыт изменился. Проверить, снимают ли новые сведения прежние "
            "препятствия; одна правка профиля не разрешает повтор автоматически.",
            previous_application_ids=ids,
        )
    if any(item.state is ApplicationState.REJECTED for item in history):
        return ReapplicationDecision(
            False,
            "REPOST_REJECTION",
            "На неизменную роль уже был отказ. Пересмотреть при существенном изменении "
            "требований или подтверждённого опыта, с сохранением обоснования.",
            previous_application_ids=ids,
        )
    return _timed_reapplication(history, now=now)


def _timed_reapplication(
    history: tuple[PreviousApplication, ...], *, now: datetime
) -> ReapplicationDecision:
    if not history:
        return ReapplicationDecision(True)
    history = tuple(
        sorted(history, key=lambda item: (as_utc(item.confirmed_at), item.application_id))
    )
    ids = tuple(item.application_id for item in history)
    last = as_utc(history[-1].confirmed_at)
    recent = [item for item in history if as_utc(item.confirmed_at) > now - REAPPLICATION_WINDOW]
    recheck = last + REAPPLICATION_INTERVAL
    if len(recent) >= REAPPLICATION_LIMIT:
        recheck = max(
            recheck, as_utc(recent[-REAPPLICATION_LIMIT].confirmed_at) + REAPPLICATION_WINDOW
        )
        return ReapplicationDecision(
            False,
            "REPOST_LIMIT",
            f"На эту роль уже отправлено {len(recent)} отклика за 90 дней. "
            f"Повторно проверить не раньше {recheck.isoformat()}; "
            "новый номер публикации историю не сбрасывает.",
            recheck,
            ids,
        )
    if recheck > now:
        return ReapplicationDecision(
            False,
            "REPOST_INTERVAL",
            "С последнего отклика ещё не прошло 30 дней. "
            f"Повторно проверить {recheck.isoformat()}.",
            recheck,
            ids,
        )
    return ReapplicationDecision(True, previous_application_ids=ids)


def _confirmed_facts(
    profile: object, *, experience_only: bool = True
) -> set[tuple[str, str]] | None:
    if not isinstance(profile, dict) or not isinstance(profile.get("profile_facts"), list):
        return None
    return {
        (str(fact.get("category")), " ".join(str(fact.get("content", "")).casefold().split()))
        for fact in profile["profile_facts"]
        if isinstance(fact, dict)
        and (
            not experience_only
            or fact.get("category")
            in {
                "experience",
                "work_experience",
                "skills",
                "projects",
                "project",
                "education",
                "courses",
                "languages",
            }
        )
    }


class ReapplicationPolicyService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def review_changed_application(
        self,
        account_id: int,
        application_id: int,
        *,
        reason: str,
        expected_review_key: str | None = None,
    ) -> ReapplicationDecision:
        application = self._session.scalar(
            select(ApplicationModel)
            .where(ApplicationModel.id == application_id, ApplicationModel.account_id == account_id)
            .with_for_update()
        )
        if application is None:
            raise LookupError("Отклик этого аккаунта не найден")
        reason = reason.strip()
        if len(reason) < 20:
            raise ValueError(
                "Укажите проверенное существенное изменение и основание повторного отклика"
            )
        task = self._session.scalar(
            select(ApplicationTaskModel)
            .where(ApplicationTaskModel.application_id == application_id)
            .with_for_update()
        )
        if (
            application.state is not ApplicationState.APPLYING
            or task is None
            or task.state is not TaskState.REVIEW_REQUIRED
        ):
            raise ValueError("Разбор возможен только для остановленного повторного отклика")
        from hugin.services.application_automation import ApplicationAutomationService

        record = ApplicationRepository(self._session).get(application_id)
        if not ApplicationAutomationService(self._session)._application_selection_current(record):
            raise ValueError("Сначала завершите действующий отбор по новым требованиям и профилю")
        vacancy = VacancyRepository(self._session).get(record.vacancy_id)
        decision = self.evaluate(
            account_id, vacancy, exclude_application_id=record.id, use_review=False
        )
        if (
            decision.code not in {"REPOST_CHANGED", "REPOST_PROFILE_CHANGED"}
            or not decision.review_key
        ):
            raise ValueError("Нет подтверждённого изменения для исключения из правила повторов")
        if expected_review_key is not None and expected_review_key != decision.review_key:
            raise ValueError(
                "Источники изменились. Обновите разбор и проверьте обоснование ещё раз"
            )
        tracked = self._session.scalar(
            select(DirectionVacancyModel)
            .where(
                DirectionVacancyModel.direction_id == record.direction_id,
                DirectionVacancyModel.vacancy_id == record.vacancy_id,
            )
            .with_for_update()
        )
        if tracked is None:
            raise LookupError("Карточка направления не найдена")
        saved = tracked.rules_details.get("repeat_policy_review")
        if (
            isinstance(saved, dict)
            and saved.get("application_id") == record.id
            and saved.get("review_key") == decision.review_key
            and saved.get("reason") == reason
        ):
            existing = self.evaluate(account_id, vacancy, exclude_application_id=record.id)
            if existing.allowed or existing.code in {"REPOST_INTERVAL", "REPOST_LIMIT"}:
                return existing
        reviewed_at = datetime.now(UTC)
        review: EventPayload = {
            "version": 1,
            "source_sha256": source_fingerprint(),
            "application_id": record.id,
            "review_key": decision.review_key,
            "reason": reason,
            "created_at": reviewed_at.isoformat(),
            "valid_until": (reviewed_at + timedelta(days=7)).isoformat(),
            "previous_application_ids": list(decision.previous_application_ids),
        }
        tracked.rules_details = {**tracked.rules_details, "repeat_policy_review": review}
        ApplicationRepository(self._session).append_event(
            record.id,
            ApplicationEventType.APPLY_INTENT,
            {"source": "reapplication_review", "review": review},
        )
        return self.evaluate(account_id, vacancy, exclude_application_id=record.id)

    def _reviewed_decision(
        self,
        decision: ReapplicationDecision,
        *,
        application: ApplicationModel | None,
        vacancy: VacancyRecord,
        history: tuple[PreviousApplication, ...],
        profile: EventPayload | None,
        now: datetime,
        use_review: bool,
    ) -> ReapplicationDecision:
        if application is None or decision.code not in {"REPOST_CHANGED", "REPOST_PROFILE_CHANGED"}:
            return decision
        key = fingerprint(
            {
                "application_id": application.id,
                "resume_id": application.resume_id,
                "vacancy": {
                    key: value
                    for key, value in asdict(vacancy).items()
                    if key not in {"created_at", "updated_at", "details_fetched_at"}
                },
                "history": history,
                "code": decision.code,
                "profile_facts": sorted(_confirmed_facts(profile, experience_only=False) or ()),
                "resume_content_sha256": profile.get("resume_content_sha256") if profile else None,
            }
        )
        decision = replace(decision, review_key=key)
        if not use_review:
            return decision
        tracked = self._session.scalar(
            select(DirectionVacancyModel).where(
                DirectionVacancyModel.direction_id == application.direction_id,
                DirectionVacancyModel.vacancy_id == application.vacancy_id,
            )
        )
        review = tracked.rules_details.get("repeat_policy_review") if tracked else None
        if (
            not isinstance(review, dict)
            or review.get("application_id") != application.id
            or review.get("review_key") != key
        ):
            return decision
        try:
            valid_until = datetime.fromisoformat(str(review.get("valid_until")))
            reviewed_at = datetime.fromisoformat(str(review.get("created_at")))
            valid = (
                valid_until.tzinfo is not None
                and reviewed_at.tzinfo is not None
                and as_utc(reviewed_at) <= now < as_utc(valid_until)
            )
        except ValueError:
            valid = False
        reason = review.get("reason")
        if not valid or not isinstance(reason, str) or len(reason.strip()) < 20:
            return decision
        timing = _timed_reapplication(history, now=now)
        if not timing.allowed:
            return timing
        return replace(
            decision,
            allowed=True,
            code=None,
            reason=f"После проверки существенных изменений: {reason}",
        )

    def evaluate(
        self,
        account_id: int,
        vacancy: VacancyRecord,
        *,
        exclude_application_id: int | None = None,
        now: datetime | None = None,
        profile: EventPayload | None = None,
        use_review: bool = True,
    ) -> ReapplicationDecision:
        selected_at = as_utc(now or datetime.now(UTC))
        if not vacancy.employer_name or not vacancy.description:
            return ReapplicationDecision(True)

        # Exact complete sources protect agency clients; known changed families go to review.
        def normalized(value: str | None) -> str:
            return " ".join((value or "").casefold().split())

        root = vacancy.duplicate_of_id or vacancy.id
        same_source = (
            (normalized_role_text(VacancyModel.employer_name) == normalized(vacancy.employer_name))
            & (normalized_role_text(VacancyModel.title) == normalized(vacancy.title))
            & (normalized_role_text(VacancyModel.description) == normalized(vacancy.description))
            & (normalized_role_text(VacancyModel.region) == normalized(vacancy.region))
            & (normalized_role_text(VacancyModel.work_format) == normalized(vacancy.work_format))
        )
        rows = self._session.execute(
            select(
                ApplicationModel.id,
                ApplicationModel.vacancy_id,
                ApplicationModel.state,
                VacancyModel.hh_id,
                same_source.label("same_source"),
            )
            .join(VacancyModel)
            .where(
                ApplicationModel.account_id == account_id,
                normalized_role_text(VacancyModel.employer_name)
                == normalized(vacancy.employer_name),
                ApplicationModel.id != exclude_application_id
                if exclude_application_id is not None
                else true(),
                or_(same_source, VacancyModel.id == root, VacancyModel.duplicate_of_id == root),
            )
        ).all()
        if not rows:
            return ReapplicationDecision(True)
        candidates = {row.id: row for row in rows}
        # Decode payloads in Python: old JSON sources can contain PostgreSQL-incompatible Unicode.
        events = self._session.scalars(
            select(ApplicationEventModel)
            .where(
                ApplicationEventModel.application_id.in_(candidates),
                ApplicationEventModel.event_type == ApplicationEventType.APPLIED,
                ApplicationEventModel.created_at <= selected_at,
            )
            .order_by(ApplicationEventModel.created_at, ApplicationEventModel.id)
        )
        confirmations: dict[int, ApplicationEventModel] = {}
        for event in events:
            previous = VacancyRepository(self._session).get(
                candidates[event.application_id].vacancy_id
            )
            if (
                VacancyDuplicateDetector().conflict_reason(vacancy, previous)
                != "different_profession_or_required_language"
            ):
                confirmations.setdefault(event.application_id, event)
        if not confirmations:
            return ReapplicationDecision(True)
        unknown_dates = tuple(
            app_id
            for app_id, event in confirmations.items()
            if event.payload.get("source") not in {"hugin_send", "hugin_reconciliation"}
            or event.payload.get("hh_status") != "APPLIED"
        )
        if unknown_dates:
            return ReapplicationDecision(
                False,
                "REPOST_HISTORY_UNKNOWN",
                "По этой роли есть прежний отклик с неизвестной датой или источником отправки. "
                "Сверить историю hh.ru перед повтором; дату импорта не считать датой отправки.",
                previous_application_ids=tuple(confirmations),
            )
        rejected = set(
            self._session.scalars(
                select(ApplicationStatusObservationModel.application_id).where(
                    ApplicationStatusObservationModel.application_id.in_(confirmations),
                    ApplicationStatusObservationModel.state == ApplicationState.REJECTED,
                    ApplicationStatusObservationModel.checked_at <= selected_at,
                    ApplicationStatusObservationModel.recorded_at <= selected_at,
                )
            )
        )
        replies: dict[int, datetime] = {}
        for app_id, received_at in self._session.execute(
            select(
                RecruiterMessageModel.application_id,
                RecruiterMessageModel.received_at,
            ).where(
                RecruiterMessageModel.application_id.in_(confirmations),
                RecruiterMessageModel.direction == MessageDirection.INCOMING,
                RecruiterMessageModel.state != RecruiterMessageState.CANCELLED,
                RecruiterMessageModel.received_at <= selected_at,
            )
        ):
            if received_at is not None:
                replies[app_id] = max(as_utc(received_at), replies.get(app_id, as_utc(received_at)))
        history = tuple(
            PreviousApplication(
                app_id,
                candidates[app_id].hh_id,
                as_utc(event.created_at),
                ApplicationState.REJECTED if app_id in rejected else candidates[app_id].state,
                replies.get(app_id),
            )
            for app_id, event in confirmations.items()
        )
        conversation = decide_reapplication(history, now=selected_at)
        if conversation.code == "REPOST_CONVERSATION":
            return conversation
        application = (
            self._session.scalar(
                select(ApplicationModel).where(
                    ApplicationModel.id == exclude_application_id,
                    ApplicationModel.account_id == account_id,
                )
            )
            if exclude_application_id is not None
            else None
        )
        if profile is None and application is not None:
            profile = application_profile_snapshot(
                self._session, ApplicationRepository(self._session).get(application.id)
            )
        if any(not candidates[app_id].same_source for app_id in confirmations):
            decision = ReapplicationDecision(
                False,
                "REPOST_CHANGED",
                "Публикация связана с прежней ролью, но текст или условия изменились. "
                "Проверить, действительно ли это другая роль, клиент или существенное изменение; "
                "после проверки сохранить решение.",
                previous_application_ids=tuple(confirmations),
            )
            return self._reviewed_decision(
                decision,
                application=application,
                vacancy=vacancy,
                history=history,
                profile=profile,
                now=selected_at,
                use_review=use_review,
            )
        current_facts = _confirmed_facts(profile)
        changed = False
        if confirmations and current_facts is not None:
            latest = next(reversed(confirmations.values()))
            context = latest.payload.get("outcome_context")
            old_facts = _confirmed_facts(
                context.get("profile") if isinstance(context, dict) else None
            )
            changed = old_facts is not None and bool(current_facts - old_facts)
        decision = decide_reapplication(history, now=selected_at, profile_changed=changed)
        return self._reviewed_decision(
            decision,
            application=application,
            vacancy=vacancy,
            history=history,
            profile=profile,
            now=selected_at,
            use_review=use_review,
        )
