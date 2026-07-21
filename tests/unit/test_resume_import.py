from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    AnswerTemplateModel,
    CandidateProfileModel,
    ProfileQuestionModel,
    ResumeModel,
    VerifiedFactModel,
)
from hugin.domain.content import ConfirmationState, ProfileQuestionState
from hugin.repositories import AccountRepository, ResumeRepository
from hugin.services.resume_profile import (
    ProfileFactService,
    ProfileQuestionService,
    ResumeImportService,
)
from tests.unit.test_resume_documents import write_resume

pytestmark = pytest.mark.integration


def test_resume_import_is_idempotent_and_questions_are_reusable(
    settings: Settings,
    tmp_path: Path,
) -> None:
    local_settings = settings.model_copy(update={"data_dir": tmp_path / "data"})
    source = tmp_path / "Резюме ИТ.docx"
    write_resume(source)
    upgrade_database(local_settings)
    database = create_database(local_settings)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "account-resume-import")
            site_resume = ResumeRepository(session).upsert(
                account.id,
                "resume-it",
                "Python backend разработчик",
            )
            first = ResumeImportService(session, local_settings.data_dir).import_file(
                account.id,
                source,
            )
            second = ResumeImportService(session, local_settings.data_dir).import_file(
                account.id,
                source,
            )

            assert not first.unchanged
            assert second.unchanged
            assert first.stored_path == second.stored_path
            assert first.stored_path.is_file()
            assert first.stored_path.read_bytes() == source.read_bytes()

            profile = session.scalar(select(CandidateProfileModel))
            resume = session.scalar(select(ResumeModel))
            facts = list(session.scalars(select(VerifiedFactModel)))
            questions = list(session.scalars(select(ProfileQuestionModel)))

            assert profile is not None
            assert resume is not None
            assert profile.active_resume_id == resume.id
            assert resume.id == site_resume.id
            assert resume.hh_id == "resume-it"
            assert resume.source_original_name == "Резюме ИТ.docx"
            assert resume.source_sha256 == first.source_sha256
            assert resume.source_size_bytes == source.stat().st_size
            assert len(facts) == first.facts_pending
            assert all(fact.state is ConfirmationState.PENDING for fact in facts)
            assert len({(fact.category, fact.content) for fact in facts}) == len(facts)

            fact_service = ProfileFactService(session)
            first_fact = fact_service.list_pending(account.id)[0]
            fact_service.confirm(account.id, first_fact.id)
            confirmed = session.get(VerifiedFactModel, first_fact.id)
            assert confirmed is not None
            assert confirmed.state is ConfirmationState.CONFIRMED
            assert confirmed.allow_in_letters
            fact_service.reject(account.id, first_fact.id)
            rejected = session.get(VerifiedFactModel, first_fact.id)
            assert rejected is not None
            assert rejected.state == ConfirmationState.REJECTED
            assert not rejected.allow_in_forms

            question_keys = {question.key for question in questions}
            assert "salary_expectation" in question_keys
            assert "available_from" in question_keys
            assert "work_format" not in question_keys

            ProfileQuestionService(session).answer(
                account.id,
                "salary_expectation",
                "от 180 000 рублей после вычета налогов",
            )
            answer = session.scalar(
                select(AnswerTemplateModel).where(AnswerTemplateModel.key == "salary_expectation")
            )
            question = session.scalar(
                select(ProfileQuestionModel).where(ProfileQuestionModel.key == "salary_expectation")
            )
            assert answer is not None
            assert answer.answer_text == "от 180 000 рублей после вычета налогов"
            assert answer.verified_fact_id is not None
            assert question is not None
            assert question.state is ProfileQuestionState.ANSWERED

            ResumeImportService(session, local_settings.data_dir).import_file(account.id, source)
            assert "salary_expectation" not in {
                item.key for item in ProfileQuestionService(session).list_pending(account.id)
            }
    finally:
        database.close()
