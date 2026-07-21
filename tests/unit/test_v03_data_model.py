from __future__ import annotations

from sqlalchemy import inspect, select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    AnswerTemplateModel,
    ApplicationSettingsModel,
    CandidateProfileModel,
    CompanyRuleModel,
    CoverLetterFactModel,
    CoverLetterModel,
    DirectionResumeModel,
    DirectionSearchQueryModel,
    IncidentModel,
    InvitationModel,
    NotificationModel,
    PromptVersionModel,
    RecruiterMessageFactModel,
    RecruiterMessageModel,
    ScreeningAnswerModel,
    ScreeningFormModel,
    ScreeningQuestionModel,
    VacancyDiscoveryModel,
    VacancyModel,
    VerifiedFactModel,
)
from hugin.domain.content import (
    AnswerSource,
    CompanyRuleType,
    ConfirmationState,
    CoverLetterState,
    DeliveryState,
    IncidentSeverity,
    IncidentState,
    InvitationState,
    MessageDirection,
    NotificationChannel,
    RecruiterMessageState,
    ScreeningFormState,
)
from hugin.domain.vacancies import VacancyData
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    ResumeRepository,
    VacancyRepository,
)


def test_v03_schema_persists_required_entities(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    required_tables = {
        "answer_templates",
        "application_settings",
        "candidate_profiles",
        "company_rules",
        "cover_letter_facts",
        "cover_letters",
        "incidents",
        "invitations",
        "notifications",
        "prompt_versions",
        "recruiter_message_facts",
        "recruiter_messages",
        "screening_answers",
        "screening_forms",
        "screening_questions",
        "vacancy_discoveries",
        "verified_facts",
    }

    try:
        assert required_tables <= set(inspect(database.engine).get_table_names())

        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "account-v03")
            resume = ResumeRepository(session).upsert(
                account.id,
                "resume-v03",
                "Python-разработчик",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python")
            directions.attach_resume(direction.id, resume.id)
            query = directions.upsert_query(
                direction.id,
                "Python",
                area="113",
                filters={"order_by": "publication_time"},
            )
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "vacancy-v03",
                    "Python-разработчик",
                    "https://hh.ru/vacancy/vacancy-v03",
                )
            )
            directions.track_vacancy(direction.id, vacancy.id)
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )

            profile = CandidateProfileModel(account_id=account.id, display_name="Иван")
            prompt = PromptVersionModel(
                purpose="COVER_LETTER",
                version=1,
                model_name="yandexgpt",
                instruction_text="Создай письмо только из подтверждённых фактов.",
            )
            incident = IncidentModel(
                code="TEST",
                severity=IncidentSeverity.INFO,
                state=IncidentState.OPEN,
                message="Проверка модели данных",
            )
            session.add_all((profile, prompt, incident))
            session.flush()

            fact = VerifiedFactModel(
                profile_id=profile.id,
                category="skill",
                content="Python",
                source_type="resume",
                resume_id=resume.id,
                direction_id=direction.id,
                state=ConfirmationState.CONFIRMED,
                allow_in_letters=True,
                allow_in_forms=True,
                allow_in_messages=True,
            )
            company_rule = CompanyRuleModel(
                direction_id=direction.id,
                company_pattern="Компания",
                rule_type=CompanyRuleType.ALLOW,
            )
            discovery = VacancyDiscoveryModel(
                vacancy_id=vacancy.id,
                direction_id=direction.id,
                search_query_id=query.id,
                query_text=query.query,
                region=query.area,
            )
            cover_letter = CoverLetterModel(
                application_id=application.id,
                vacancy_id=vacancy.id,
                direction_id=direction.id,
                resume_id=resume.id,
                prompt_version_id=prompt.id,
                text="Здравствуйте!",
                instruction_version="1",
                model_name="yandexgpt",
                state=CoverLetterState.READY,
            )
            form = ScreeningFormModel(
                application_id=application.id,
                version_hash="form-v1",
                state=ScreeningFormState.INPUT_REQUIRED,
                requires_confirmation=True,
            )
            template = AnswerTemplateModel(
                profile_id=profile.id,
                key="city",
                question_pattern="Город",
                answer_text="Уфа",
            )
            message = RecruiterMessageModel(
                application_id=application.id,
                hh_id="message-v1",
                direction=MessageDirection.OUTGOING,
                body="Проект ответа",
                state=RecruiterMessageState.REVIEW_REQUIRED,
            )
            invitation = InvitationModel(
                application_id=application.id,
                hh_id="invitation-v1",
                title="Собеседование",
                state=InvitationState.RECEIVED,
            )
            notification = NotificationModel(
                application_id=application.id,
                incident_id=incident.id,
                event_type="INVITATION",
                channel=NotificationChannel.WINDOWS,
                state=DeliveryState.PENDING,
                payload={"title": "Собеседование"},
            )
            session.add_all(
                (
                    fact,
                    company_rule,
                    discovery,
                    cover_letter,
                    form,
                    template,
                    message,
                    invitation,
                    notification,
                )
            )
            session.flush()

            question = ScreeningQuestionModel(
                form_id=form.id,
                field_key="city",
                question_text="Ваш город?",
                is_required=True,
                field_type="text",
            )
            session.add(question)
            session.flush()
            session.add_all(
                (
                    ScreeningAnswerModel(
                        question_id=question.id,
                        answer_text="Уфа",
                        source=AnswerSource.USER,
                        is_confirmed=True,
                    ),
                    CoverLetterFactModel(cover_letter_id=cover_letter.id, fact_id=fact.id),
                    RecruiterMessageFactModel(message_id=message.id, fact_id=fact.id),
                )
            )

            settings_row = session.get(ApplicationSettingsModel, 1)
            mapping = session.get(DirectionResumeModel, (direction.id, resume.id))
            query_model = session.get(DirectionSearchQueryModel, query.id)
            vacancy_model = session.get(VacancyModel, vacancy.id)

            assert settings_row is not None
            assert settings_row.hh_apply_daily_limit == 25
            assert mapping is not None
            assert mapping.role.value == "PRIMARY"
            assert query_model is not None
            assert query_model.schedule_minutes == 120
            assert vacancy_model is not None
            assert not vacancy_model.has_screening_form
            assert session.scalar(select(VerifiedFactModel.content)) == "Python"
    finally:
        database.close()
