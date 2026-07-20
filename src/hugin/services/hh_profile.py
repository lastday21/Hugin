from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from hugin.domain.directions import AccountRecord, ResumeRecord
from hugin.domain.hh import HhProfileData
from hugin.repositories.directions import AccountRepository, ResumeRepository


@dataclass(frozen=True, slots=True)
class HhProfileSyncResult:
    account: AccountRecord
    resumes: tuple[ResumeRecord, ...]


class HhProfileSyncService:
    def __init__(self, session: Session) -> None:
        self._accounts = AccountRepository(session)
        self._resumes = ResumeRepository(session)

    def synchronize(self, profile: HhProfileData) -> HhProfileSyncResult:
        account = self._accounts.upsert(profile.label, profile.external_id)
        resumes = self._resumes.synchronize(account.id, profile.resumes)
        return HhProfileSyncResult(account=account, resumes=tuple(resumes))
