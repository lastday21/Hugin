from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from hugin.database.models import CandidateProfileModel, ResumeModel, VerifiedFactModel
from hugin.domain.applications import ApplicationRecord, EventPayload
from hugin.domain.content import ConfirmationState


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def application_profile_snapshot(session: Session, application: ApplicationRecord) -> EventPayload:
    resume = session.get(ResumeModel, application.resume_id)
    if resume is None or resume.account_id != application.account_id:
        raise LookupError("Resume does not belong to the application account")
    facts = [
        {
            "id": fact.id,
            "category": fact.category,
            "content": fact.content,
            "source_reference": fact.source_reference,
            "resume_id": fact.resume_id,
            "direction_id": fact.direction_id,
        }
        for fact in session.scalars(
            select(VerifiedFactModel)
            .join(CandidateProfileModel)
            .where(
                CandidateProfileModel.account_id == application.account_id,
                VerifiedFactModel.state == ConfirmationState.CONFIRMED,
                or_(
                    VerifiedFactModel.resume_id.is_(None), VerifiedFactModel.resume_id == resume.id
                ),
                or_(
                    VerifiedFactModel.direction_id.is_(None),
                    VerifiedFactModel.direction_id == application.direction_id,
                ),
            )
            .order_by(VerifiedFactModel.id)
        )
    ]
    resume_content = resume.content_text or ""
    return {
        "schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "resume_source_type": resume.source_type,
        "resume_content_sha256": _fingerprint(resume_content) if resume_content else None,
        "resume_content": resume_content or None,
        "resume_source_reference": resume.source_reference,
        "resume_title": resume.title,
        "profile_facts_sha256": _fingerprint(facts) if facts else None,
        "profile_facts": facts,
    }
