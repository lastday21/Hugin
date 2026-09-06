from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.base import Base
from hugin.database.models import ScreeningAnswerModel, ScreeningFormModel, ScreeningQuestionModel


def _row(model: Base) -> dict[str, Any]:
    result = {}
    for column in model.__table__.columns:
        value = getattr(model, column.name)
        result[column.name] = value.isoformat() if isinstance(value, (date, datetime)) else value
    return result


def screening_evidence(session: Session, application_id: int) -> list[dict[str, Any]]:
    forms = session.scalars(
        select(ScreeningFormModel)
        .where(ScreeningFormModel.application_id == application_id)
        .order_by(ScreeningFormModel.id)
    )
    result = []
    for form in forms:
        snapshot = _row(form)
        questions = []
        for question, answer in session.execute(
            select(ScreeningQuestionModel, ScreeningAnswerModel)
            .outerjoin(
                ScreeningAnswerModel,
                ScreeningAnswerModel.question_id == ScreeningQuestionModel.id,
            )
            .where(ScreeningQuestionModel.form_id == form.id)
            .order_by(ScreeningQuestionModel.position, ScreeningQuestionModel.id)
        ):
            questions.append({**_row(question), "answer": _row(answer) if answer else None})
        snapshot["questions"] = questions
        result.append(snapshot)
    return result
