from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import SemanticStageModel, VacancyChangeModel
from hugin.services.decision_evidence import fingerprint
from hugin.services.semantic_analyzer import StageRecord


def load_stage(session: Session, account_id: int, vacancy_id: int, key: str) -> StageRecord | None:
    row = session.scalar(
        select(SemanticStageModel)
        .where(
            SemanticStageModel.account_id == account_id,
            SemanticStageModel.vacancy_id == vacancy_id,
            SemanticStageModel.cache_key == key,
        )
        .order_by(SemanticStageModel.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return _record(row)


def _record(row: SemanticStageModel) -> StageRecord:
    return StageRecord(
        row.cache_key,
        row.stage,
        row.model,
        dict(row.request),
        row.response_text,
        row.response_sha256,
        tuple(row.errors),
        row.created_at,
        row.duration_seconds,
    )


class DatabaseStageCache:
    def __init__(self, settings: Settings, account_id: int, vacancy_id: int) -> None:
        self._settings = settings
        self._account_id = account_id
        self._vacancy_id = vacancy_id

    def get(self, key: str) -> StageRecord | None:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                existing = load_stage(session, self._account_id, self._vacancy_id, key)
                if existing is not None:
                    return existing
                rows = session.scalars(
                    select(SemanticStageModel)
                    .where(
                        SemanticStageModel.account_id == self._account_id,
                        SemanticStageModel.vacancy_id != self._vacancy_id,
                        SemanticStageModel.cache_key == key,
                    )
                    .order_by(SemanticStageModel.id.desc())
                )
                for row in rows:
                    record = _record(row)
                    if (
                        record.errors
                        or fingerprint(record.request) != key
                        or fingerprint(record.response_text) != record.response_sha256
                    ):
                        continue
                    reused = replace(record, created_at=datetime.now(UTC), duration_seconds=0.0)
                    session.add(self._row(reused))
                    session.add(
                        VacancyChangeModel(
                            vacancy_id=self._vacancy_id,
                            event_type="SEMANTIC_STAGE_REUSED",
                            changes={
                                "account_id": self._account_id,
                                "source_vacancy_id": row.vacancy_id,
                                "source_stage_id": row.id,
                                "source_created_at": row.created_at.isoformat(),
                                "stage": record.stage,
                                "cache_key": key,
                                "model_calls": 0,
                            },
                        )
                    )
                    return reused
                return None
        finally:
            database.close()

    def put(self, record: StageRecord) -> None:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                session.add(self._row(record))
        finally:
            database.close()

    def _row(self, record: StageRecord) -> SemanticStageModel:
        return SemanticStageModel(
            account_id=self._account_id,
            vacancy_id=self._vacancy_id,
            cache_key=record.cache_key,
            stage=record.stage,
            model=record.model,
            request=record.request,
            response_text=record.response_text,
            response_sha256=record.response_sha256,
            errors=list(record.errors),
            created_at=record.created_at,
            duration_seconds=record.duration_seconds,
        )
