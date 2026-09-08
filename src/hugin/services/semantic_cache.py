from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import SemanticStageModel
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
                return load_stage(session, self._account_id, self._vacancy_id, key)
        finally:
            database.close()

    def put(self, record: StageRecord) -> None:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                session.add(
                    SemanticStageModel(
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
                )
        finally:
            database.close()
