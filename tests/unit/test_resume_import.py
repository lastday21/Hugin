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
from hugin.domain.resumes import ParsedResumeProfile, ResumeFactCandidate
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
            fact_service.confirm(
                account.id,
                first_fact.id,
                allow_in_letters=True,
                allow_in_forms=True,
                allow_in_messages=True,
            )
            confirmed = session.get(VerifiedFactModel, first_fact.id)
            assert confirmed is not None
            assert confirmed.state is ConfirmationState.CONFIRMED
            assert confirmed.allow_in_letters
            assert confirmed.actual_at is not None
            fact_service.reject(account.id, first_fact.id)
            rejected = session.get(VerifiedFactModel, first_fact.id)
            assert rejected is not None
            assert rejected.state == ConfirmationState.REJECTED
            assert not rejected.allow_in_forms

            question_keys = {question.key for question in questions}
            assert "salary_expectation" in question_keys
            assert "available_from" in question_keys
            assert "work_format" not in question_keys

            with pytest.raises(ValueError, match="повреждённые символы"):
                ProfileQuestionService(session).answer(
                    account.id,
                    "salary_expectation",
                    "п»ї??????????? ? ????????",
                )

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
            answer_fact = session.get(VerifiedFactModel, answer.verified_fact_id)
            assert answer_fact is not None
            assert answer_fact.actual_at is not None
            assert question is not None
            assert question.state is ProfileQuestionState.ANSWERED

            ResumeImportService(session, local_settings.data_dir).import_file(account.id, source)
            assert "salary_expectation" not in {
                item.key for item in ProfileQuestionService(session).list_pending(account.id)
            }
    finally:
        database.close()


def test_profile_fact_confirmation_and_correction_keep_one_active_version(
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
            account = AccountRepository(session).create("Иван", "account-fact-correction")
            ResumeRepository(session).upsert(
                account.id,
                "resume-fact-correction",
                "Python backend разработчик",
            )
            ResumeImportService(session, local_settings.data_dir).import_file(
                account.id,
                source,
            )
            profile = session.scalar(select(CandidateProfileModel))
            assert profile is not None
            original = session.scalar(
                select(VerifiedFactModel).where(VerifiedFactModel.category == "work_experience")
            )
            assert original is not None
            service = ProfileFactService(session)
            service.confirm(
                account.id,
                original.id,
                allow_in_letters=True,
                allow_in_forms=True,
                allow_in_messages=True,
            )
            replacement_resume = ResumeRepository(session).upsert(
                account.id,
                "resume-fact-correction-new",
                "Python backend разработчик",
            )

            replacement = VerifiedFactModel(
                profile_id=profile.id,
                category=original.category,
                content="Обновлённый опыт работы",
                source_type="resume",
                source_reference="new-resume#section:work_experience",
                resume_id=replacement_resume.id,
                direction_id=original.direction_id,
            )
            session.add(replacement)
            session.flush()
            service.confirm(
                account.id,
                replacement.id,
                allow_in_letters=True,
                allow_in_forms=False,
                allow_in_messages=False,
            )

            assert original.state is ConfirmationState.REJECTED
            assert not original.allow_in_letters
            assert not original.allow_in_forms
            assert not original.allow_in_messages
            assert replacement.state.value == ConfirmationState.CONFIRMED.value

            corrected = service.correct(
                account.id,
                replacement.id,
                "Исправленный опыт работы",
                allow_in_letters=True,
                allow_in_forms=True,
                allow_in_messages=False,
            )

            assert corrected.id != replacement.id
            assert corrected.profile_id == replacement.profile_id
            assert corrected.resume_id == replacement.resume_id
            assert corrected.direction_id == replacement.direction_id
            assert corrected.source_type == "user"
            assert corrected.source_reference == f"profile-fact:{replacement.id}"
            assert corrected.state is ConfirmationState.CONFIRMED
            assert corrected.allow_in_letters
            assert corrected.allow_in_forms
            assert not corrected.allow_in_messages
            assert corrected.actual_at is not None
            assert replacement.state is ConfirmationState.REJECTED
            assert not replacement.allow_in_letters

            service.reject(account.id, corrected.id)
            fact_count = len(list(session.scalars(select(VerifiedFactModel))))
            reactivated = service.correct(
                account.id,
                corrected.id,
                "\nИсправленный опыт работы\n",
                allow_in_letters=False,
                allow_in_forms=True,
                allow_in_messages=True,
            )

            assert reactivated.id == corrected.id
            assert len(list(session.scalars(select(VerifiedFactModel)))) == fact_count
            assert reactivated.state is ConfirmationState.CONFIRMED
            assert not reactivated.allow_in_letters
            assert reactivated.allow_in_forms
            assert reactivated.allow_in_messages

            with pytest.raises(ValueError, match="не может быть пустым"):
                service.correct(account.id, corrected.id, "   ")
            with pytest.raises(ValueError, match="повреждённые символы"):
                service.correct(account.id, corrected.id, "???")
    finally:
        database.close()


def test_confirmed_fact_is_not_inherited_between_resumes(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_settings = settings.model_copy(update={"data_dir": tmp_path / "data"})
    source = tmp_path / "Резюме ИТ.docx"
    write_resume(source)
    upgrade_database(local_settings)
    database = create_database(local_settings)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "account-two-resumes")
            first_resume = ResumeRepository(session).upsert(
                account.id,
                "resume-first",
                "Python-разработчик",
            )
            second_resume = ResumeRepository(session).upsert(
                account.id,
                "resume-second",
                "Инженер по данным",
            )
            profile_data = ParsedResumeProfile(
                display_name="Иван",
                title="Python-разработчик",
                facts=(
                    ResumeFactCandidate(
                        category="work_experience",
                        content="Более двух лет практической разработки на Python",
                        source_reference="section:work_experience",
                    ),
                ),
                missing_questions=(),
            )
            first_import = ResumeImportService(session, local_settings.data_dir)
            monkeypatch.setattr(
                first_import._extractor,
                "extract",
                lambda _document: profile_data,
            )
            first_import.import_file(
                account.id,
                source,
                hh_resume_id=first_resume.hh_id,
            )
            first_fact = session.scalar(
                select(VerifiedFactModel).where(
                    VerifiedFactModel.resume_id == first_resume.id,
                )
            )
            assert first_fact is not None
            ProfileFactService(session).confirm(
                account.id,
                first_fact.id,
                allow_in_letters=True,
                allow_in_forms=True,
                allow_in_messages=True,
            )
            profile = session.scalar(select(CandidateProfileModel))
            assert profile is not None
            common_fact = VerifiedFactModel(
                profile_id=profile.id,
                category=first_fact.category,
                content=first_fact.content,
                source_type="user",
                source_reference="profile",
                state=ConfirmationState.CONFIRMED,
                allow_in_letters=True,
                allow_in_forms=True,
                allow_in_messages=True,
            )
            session.add(common_fact)
            session.flush()

            second_profile_data = ParsedResumeProfile(
                display_name="Иван",
                title="Инженер по данным",
                facts=profile_data.facts,
                missing_questions=(),
            )
            second_import = ResumeImportService(session, local_settings.data_dir)
            monkeypatch.setattr(
                second_import._extractor,
                "extract",
                lambda _document: second_profile_data,
            )
            result = second_import.import_file(
                account.id,
                source,
                hh_resume_id=second_resume.hh_id,
            )
            second_fact = session.scalar(
                select(VerifiedFactModel).where(
                    VerifiedFactModel.resume_id == second_resume.id,
                )
            )

            assert result.facts_pending == 1
            assert second_fact is not None
            assert second_fact.state is ConfirmationState.PENDING
            assert not second_fact.allow_in_letters
            assert not second_fact.allow_in_forms
            assert not second_fact.allow_in_messages
            assert first_fact.state is ConfirmationState.CONFIRMED
            assert first_fact.allow_in_letters
            assert first_fact.allow_in_forms
            assert first_fact.allow_in_messages
            assert common_fact.state is ConfirmationState.CONFIRMED
            assert common_fact.allow_in_letters
            assert common_fact.allow_in_forms
            assert common_fact.allow_in_messages
    finally:
        database.close()
