from __future__ import annotations

import pytest
from sqlalchemy import func, select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    AnswerTemplateModel,
    CandidateProfileModel,
    ScreeningFormModel,
    VerifiedFactModel,
)
from hugin.domain import (
    AnswerSource,
    ConfirmationState,
    HhScreeningField,
    HhScreeningForm,
    ScreeningFormState,
    VacancyData,
)
from hugin.repositories import AccountRepository, ApplicationRepository, ResumeRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.screening_forms import ScreeningDraftService

pytestmark = pytest.mark.integration


def test_draft_uses_only_confirmed_safe_answers_and_replaces_changed_form(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "forms-account")
            resume = ResumeRepository(session).upsert(account.id, "resume-1", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "vacancy-1",
                    "Python разработчик",
                    "https://hh.ru/vacancy/vacancy-1",
                    employer_name="Компания",
                )
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
            )
            profile = CandidateProfileModel(
                account_id=account.id,
                active_resume_id=resume.id,
                display_name="Иван",
            )
            session.add(profile)
            session.flush()
            telegram = VerifiedFactModel(
                profile_id=profile.id,
                category="telegram",
                content="@ivan",
                source_type="resume",
                state=ConfirmationState.CONFIRMED,
                allow_in_forms=True,
            )
            salary = VerifiedFactModel(
                profile_id=profile.id,
                category="salary_expectation",
                content="120000 рублей на руки",
                source_type="user",
                state=ConfirmationState.CONFIRMED,
                allow_in_forms=True,
            )
            session.add_all((telegram, salary))
            session.flush()
            session.add(
                AnswerTemplateModel(
                    profile_id=profile.id,
                    key="salary_expectation",
                    question_pattern="Какие зарплатные ожидания?",
                    answer_text="120000 рублей на руки",
                    verified_fact_id=salary.id,
                )
            )
            session.flush()

            draft = ScreeningDraftService(session).capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "name:telegram",
                            "Укажите Telegram",
                            "text",
                            is_required=True,
                        ),
                        HhScreeningField(
                            "name:salary",
                            "Какие зарплатные ожидания?",
                            "text",
                            is_required=True,
                        ),
                        HhScreeningField(
                            "name:motivation",
                            "Почему хотите работать у нас?",
                            "textarea",
                            is_required=True,
                        ),
                        HhScreeningField(
                            "name:passport",
                            "Укажите серию и номер паспорта",
                            "text",
                            is_required=True,
                        ),
                    )
                ),
            )

            assert draft.state is ScreeningFormState.INPUT_REQUIRED
            assert draft.answers == {
                "name:telegram": "@ivan",
                "name:salary": "120000 рублей на руки",
            }
            assert draft.questions[0].source is AnswerSource.PROFILE
            assert draft.questions[1].source is AnswerSource.BANK
            assert draft.unanswered_count == 2
            pending = ScreeningDraftService(session).list_pending(account.id)
            assert len(pending) == 1
            assert pending[0].form_id == draft.form_id
            assert (
                ScreeningDraftService(session).get_pending(account.id, vacancy.hh_id).form_id
                == draft.form_id
            )

            changed = ScreeningDraftService(session).capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "name:telegram",
                            "Укажите Telegram",
                            "text",
                            is_required=True,
                        ),
                    )
                ),
            )

            assert changed.state is ScreeningFormState.REVIEW_REQUIRED
            assert changed.answers == {"name:telegram": "@ivan"}
            assert session.scalar(select(func.count()).select_from(ScreeningFormModel)) == 1
            ScreeningDraftService(session).invalidate(changed.form_id)
            assert ScreeningDraftService(session).list_pending(account.id) == ()
    finally:
        database.close()


def test_option_answer_is_used_only_on_exact_match(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "options-account")
            resume = ResumeRepository(session).upsert(account.id, "resume-1", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData("vacancy-2", "Python", "https://hh.ru/vacancy/vacancy-2")
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
            )
            profile = CandidateProfileModel(account_id=account.id, display_name="Иван")
            session.add(profile)
            session.flush()
            session.add(
                AnswerTemplateModel(
                    profile_id=profile.id,
                    key="work_format",
                    question_pattern="Какой формат работы вам подходит?",
                    answer_text="Удалённо",
                )
            )
            session.flush()

            exact = ScreeningDraftService(session).capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "name:format",
                            "Какой формат работы вам подходит?",
                            "radio",
                            is_required=True,
                            options=("Офис", "Удалённо"),
                        ),
                    )
                ),
            )
            assert exact.answers == {"name:format": "Удалённо"}

            incompatible = ScreeningDraftService(session).capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "name:format",
                            "Какой формат работы вам подходит?",
                            "radio",
                            is_required=True,
                            options=("Офис", "Гибрид"),
                        ),
                    )
                ),
            )
            assert incompatible.answers == {}
            assert incompatible.state is ScreeningFormState.INPUT_REQUIRED
    finally:
        database.close()
