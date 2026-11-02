from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import SQLColumnExpression, case, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from hugin.database.models import ApplicationModel, RecruiterMessageModel, VacancyModel
from hugin.domain.content import MessageDirection, RecruiterMessageState
from hugin.domain.time import as_utc, timezone_by_name


def normalized_role_text(value: SQLColumnExpression[str | None]) -> ColumnElement[str]:
    return func.btrim(
        func.regexp_replace(
            func.lower(func.replace(func.coalesce(value, ""), "\u00a0", " ")),
            "[[:space:]]+",
            " ",
            "g",
        )
    )


@dataclass(frozen=True, slots=True)
class ApplicationPeriod:
    key: str
    starts_at: datetime
    applications: int
    unique_roles: int
    unique_employers: int
    unknown_roles: int
    unknown_employers: int
    repeated_applications: int
    viewed: int
    rejections: int
    replies: int
    invitations: int
    confirmed_interview_invitations: int
    scheduled_interviews: int
    mature_applications: int
    checked_last_48_hours: int
    reply_percent: float | None
    interview_percent: float | None


@dataclass(frozen=True, slots=True)
class RepeatPublication:
    application_id: int
    hh_id: str
    published_at: datetime | None
    confirmed_at: datetime


@dataclass(frozen=True, slots=True)
class RepeatRole:
    employer: str
    title: str
    region: str | None
    application_ids: tuple[int, ...]
    publications: tuple[RepeatPublication, ...]
    viewed: int
    rejections: int
    replies: int
    confirmed_interview_invitations: int


def application_statistics(
    session: Session,
    sent_at: dict[int, datetime],
    *,
    now: datetime,
    timezone_name: str,
    viewed: set[int],
    rejected: set[int],
    invited: set[int],
    interviewed: set[int],
    scheduled: set[int],
    recently_checked: set[int],
) -> tuple[tuple[ApplicationPeriod, ...], tuple[RepeatRole, ...]]:
    # Short keys and descriptions share one database snapshot, including concurrent edits.
    short_metadata = (
        select(
            ApplicationModel.id,
            VacancyModel.id.label("vacancy_id"),
            VacancyModel.hh_id,
            VacancyModel.title,
            VacancyModel.employer_name,
            VacancyModel.region,
            VacancyModel.published_at,
            normalized_role_text(VacancyModel.employer_name).label("employer_key"),
            normalized_role_text(VacancyModel.title).label("title_key"),
            func.coalesce(VacancyModel.description, "")
            .op("~")("[^[:space:]\u00a0]")
            .label("has_description"),
            normalized_role_text(VacancyModel.region).label("region_key"),
            normalized_role_text(VacancyModel.work_format).label("format_key"),
        )
        .join(VacancyModel)
        .where(ApplicationModel.id.in_(sent_at))
        .cte("role_metadata")
        .prefix_with("MATERIALIZED")
    )
    candidates = select(
        short_metadata,
        func.count()
        .filter(short_metadata.c.has_description, short_metadata.c.employer_key != "")
        .over(
            partition_by=(
                short_metadata.c.employer_key,
                short_metadata.c.title_key,
                short_metadata.c.region_key,
                short_metadata.c.format_key,
            )
        )
        .label("candidate_count"),
    ).cte("role_candidates")
    metadata = {
        row.id: row
        for row in session.execute(
            select(
                candidates,
                case(
                    (
                        (candidates.c.candidate_count > 1)
                        & candidates.c.has_description
                        & (candidates.c.employer_key != ""),
                        func.md5(normalized_role_text(VacancyModel.description)),
                    ),
                    else_=None,
                ).label("description_key"),
            ).join(VacancyModel, VacancyModel.id == candidates.c.vacancy_id)
        )
    }
    groups: dict[tuple[object, ...], list[int]] = defaultdict(list)
    roles: dict[int, tuple[object, ...]] = {}
    for app_id in sorted(sent_at, key=lambda item: (as_utc(sent_at[item]), item)):
        row = metadata[app_id]
        key = (
            (
                row.employer_key,
                row.title_key,
                row.description_key,
                row.region_key,
                row.format_key,
            )
            if row.description_key is not None
            else ("unique", app_id)
        )
        roles[app_id] = key
        groups[key].append(app_id)
    repeated = {app_id for group in groups.values() for app_id in group[1:]}
    replies = {
        row.application_id
        for row in session.execute(
            select(
                RecruiterMessageModel.application_id,
                RecruiterMessageModel.received_at,
            ).where(
                RecruiterMessageModel.application_id.in_(sent_at),
                RecruiterMessageModel.direction == MessageDirection.INCOMING,
                RecruiterMessageModel.state != RecruiterMessageState.CANCELLED,
                RecruiterMessageModel.received_at <= now,
                RecruiterMessageModel.created_at <= now,
            )
        )
        if row.received_at is not None
        and as_utc(row.received_at) >= as_utc(sent_at[row.application_id])
    }
    local = now.astimezone(timezone_by_name(timezone_name))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    starts = (midnight, midnight - timedelta(days=midnight.weekday()), midnight.replace(day=1))
    periods: list[ApplicationPeriod] = []
    for period_key, start in zip(("today", "week", "month"), starts, strict=True):
        start = start.astimezone(UTC)
        ids = {app_id for app_id, at in sent_at.items() if start <= as_utc(at) <= now}
        count = len(ids)
        periods.append(
            ApplicationPeriod(
                key=period_key,
                starts_at=start,
                applications=count,
                unique_roles=len({roles[app_id] for app_id in ids}),
                unique_employers=len(
                    {
                        metadata[app_id].employer_key
                        for app_id in ids
                        if metadata[app_id].employer_key
                    }
                ),
                unknown_roles=sum(
                    not metadata[app_id].employer_key or not metadata[app_id].has_description
                    for app_id in ids
                ),
                unknown_employers=sum(not metadata[app_id].employer_key for app_id in ids),
                repeated_applications=len(ids & repeated),
                viewed=len(ids & viewed),
                rejections=len(ids & rejected),
                replies=len(ids & replies),
                invitations=len(ids & invited),
                confirmed_interview_invitations=len(ids & interviewed),
                scheduled_interviews=len(ids & scheduled),
                mature_applications=sum(
                    as_utc(sent_at[app_id]) <= now - timedelta(days=14) for app_id in ids
                ),
                checked_last_48_hours=len(ids & recently_checked),
                reply_percent=round(100 * len(ids & replies) / count, 2) if count else None,
                interview_percent=round(100 * len(ids & interviewed) / count, 2) if count else None,
            )
        )
    repeats: list[RepeatRole] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        row = metadata[group[0]]
        ids = set(group)
        repeats.append(
            RepeatRole(
                employer=row.employer_name,
                title=row.title,
                region=row.region,
                application_ids=tuple(group),
                publications=tuple(
                    RepeatPublication(
                        app_id,
                        metadata[app_id].hh_id,
                        metadata[app_id].published_at,
                        as_utc(sent_at[app_id]),
                    )
                    for app_id in group
                ),
                viewed=len(ids & viewed),
                rejections=len(ids & rejected),
                replies=len(ids & replies),
                confirmed_interview_invitations=len(ids & interviewed),
            )
        )
    return tuple(periods), tuple(repeats)
