from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    ApplicationModel,
    ApplicationTaskModel,
    CandidateProfileModel,
    CoverLetterFactModel,
    CoverLetterModel,
    ResumeModel,
    VacancyModel,
    VerifiedFactModel,
)
from hugin.domain.content import ConfirmationState, CoverLetterState
from hugin.domain.directions import VacancyState
from hugin.domain.hh import HhApplyResult, HhApplyStatus
from hugin.domain.tasks import SystemState, TaskState
from hugin.domain.vacancies import VacancyData
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    QueueTaskRepository,
    ResumeRepository,
    SystemStateRepository,
)
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.cover_letter import (
    MANUAL_REVIEW_MODEL,
    MAX_LETTER_LENGTH,
    CoverLetterService,
    CoverLetterValidationError,
    _ensure_relevant_evidence,
    _letter_similarity,
    _relevant_excerpt,
    _SelectedFact,
    _set_similarity,
    _without_future_plans,
    _without_irrelevant_context_lines,
    _work_experience_excerpt,
    build_cover_letter_prompt,
    normalize_cover_letter,
    validate_cover_letter,
)
from hugin.services.vacancy_analysis import RULES_VERSION

pytestmark = pytest.mark.integration


class FakeModel:
    model_name = "yandexgpt-test"

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.prompts.append((system_prompt, user_prompt))
        return self.responses.pop(0)


def _letter() -> str:
    return (
        "Здравствуйте!\n\n"
        "Разрабатывал серверные приложения на Python с FastAPI и PostgreSQL, поэтому знаком "
        "с задачами развития серверной части и интеграций. В одном из проектов реализовал "
        "прикладную логику сервиса и настроил автоматические проверки, чтобы изменения можно "
        "было безопасно проверять перед выпуском. При доработке таких служб отделял "
        "прикладную логику от доступа к данным и проверял обработку ошибок.\n\n"
        "Буду рад подробнее обсудить задачи серверной части и рассказать о реализованных "
        "решениях."
    )


def _letter_with_template_phrase() -> str:
    return (
        "Здравствуйте!\n\n"
        "Вижу, что вы ищете разработчика серверных приложений. Разрабатывал сервисы на Python "
        "и FastAPI, работал с PostgreSQL и настраивал автоматические проверки. Реализовывал "
        "прикладную логику и интеграции, проверял обработку ошибок и изменения перед выпуском. "
        "При доработке сервисов отделял прикладную логику от доступа к данным.\n\n"
        "Буду рад подробнее рассказать о выполненных проектах и обсудить задачи команды."
    )


def _prepare_data(
    session: object,
    *,
    with_duplicate: bool = False,
) -> tuple[int, int, int, tuple[int, ...]]:
    account = AccountRepository(session).create("Кандидат", "account-letters")  # type: ignore[arg-type]
    resume = ResumeRepository(session).upsert(  # type: ignore[arg-type]
        account.id,
        "resume-letters",
        "Python-разработчик",
    )
    directions = DirectionRepository(session)  # type: ignore[arg-type]
    direction = directions.create(account.id, "Python backend")
    directions.attach_resume(direction.id, resume.id)
    vacancies = VacancyRepository(session)  # type: ignore[arg-type]
    first = vacancies.upsert(
        VacancyData(
            hh_id="letter-1",
            title="Python-разработчик",
            source_url="https://hh.ru/vacancy/letter-1",
            employer_name="Тестовая компания",
            published_at=datetime(2026, 7, 22, tzinfo=UTC),
            description="Полное описание: FastAPI, PostgreSQL, интеграции и проверка кода.",
            responsibilities="Развивать серверную часть и интеграции.",
            required_qualifications="Python, FastAPI, PostgreSQL.",
            key_skills=("Python", "FastAPI", "PostgreSQL"),
            details_fetched_at=datetime(2026, 7, 22, tzinfo=UTC),
        )
    )
    stored = [first]
    if with_duplicate:
        duplicate = vacancies.upsert(
            VacancyData(
                hh_id="letter-2",
                title="Python-разработчик",
                source_url="https://hh.ru/vacancy/letter-2",
                employer_name="Тестовая компания",
                published_at=datetime(2026, 7, 23, tzinfo=UTC),
                description="Повторная публикация: FastAPI, PostgreSQL и интеграции.",
                key_skills=("Python", "FastAPI", "PostgreSQL"),
                details_fetched_at=datetime(2026, 7, 23, tzinfo=UTC),
            )
        )
        vacancies.mark_duplicate(duplicate.id, first.id, 0.95)
        stored.append(duplicate)

    for vacancy in stored:
        directions.track_vacancy(direction.id, vacancy.id)
        directions.apply_rules(
            direction.id,
            vacancy.id,
            state=VacancyState.ANALYZED,
            score=85,
            details={
                "category": "MATCH",
                "accepted": True,
                "reasons": ["совпадают Python, FastAPI и PostgreSQL"],
            },
            rules_version=RULES_VERSION,
        )

    profile = CandidateProfileModel(
        account_id=account.id,
        active_resume_id=resume.id,
        display_name="Кандидат",
    )
    session.add(profile)  # type: ignore[attr-defined]
    session.flush()  # type: ignore[attr-defined]
    allowed = VerifiedFactModel(
        profile_id=profile.id,
        category="work_experience",
        content=(
            "Разрабатывал серверные приложения на Python. Работал с FastAPI и PostgreSQL. "
            "Настраивал автоматические проверки.\nGitHub: github.com/candidate"
        ),
        source_type="resume",
        resume_id=resume.id,
        state=ConfirmationState.CONFIRMED,
        allow_in_letters=True,
    )
    denied = VerifiedFactModel(
        profile_id=profile.id,
        category="work_experience",
        content="Руководил командой и работал с Kubernetes.",
        source_type="resume",
        resume_id=resume.id,
        state=ConfirmationState.PENDING,
        allow_in_letters=False,
    )
    session.add_all((allowed, denied))  # type: ignore[attr-defined]
    session.flush()  # type: ignore[attr-defined]
    ApplicationAutomationService(session).prepare_for_account_id(  # type: ignore[arg-type]
        account_id=account.id,
        direction_name=direction.name,
        include_stretch=True,
    )
    return account.id, direction.id, resume.id, tuple(item.id for item in stored)


def test_yandex_letter_uses_only_confirmed_facts_and_is_saved(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel([_letter()])
    try:
        with database.sessions.begin() as session:
            account_id, direction_id, _, _ = _prepare_data(session)
            before = CoverLetterService(session).status(
                account_id=account_id,
                direction_name="Python backend",
            )
            assert before.missing == 1
            result = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
            )

            assert result.generated == 1
            assert result.failed == 0
            assert "Полное описание" in model.prompts[0][1]
            assert "Настраивал автоматические проверки" in model.prompts[0][1]
            assert "Kubernetes" not in model.prompts[0][1]
            assert "github.com" not in model.prompts[0][1]

            letter = session.scalar(select(CoverLetterModel))
            assert letter is not None
            assert letter.state == CoverLetterState.READY
            assert letter.text == _letter()
            assert letter.context_hash
            fact_ids = tuple(
                session.scalars(
                    select(CoverLetterFactModel.fact_id).where(
                        CoverLetterFactModel.cover_letter_id == letter.id
                    )
                )
            )
            assert len(fact_ids) == 1
            after = CoverLetterService(session).status(
                account_id=account_id,
                direction_name="Python backend",
            )
            assert after.ready == 1
            assert after.missing == 0
            repeated = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
                limit=1,
            )
            assert repeated.already_ready == 1
            assert len(model.prompts) == 1

            SystemStateRepository(session).transition(SystemState.RUNNING)
            job = ApplicationAutomationService(session).claim_next(
                direction_id,
                require_cover_letter=True,
            )
            assert job is not None
            assert job.cover_letter == _letter()
            selected_instruction_version = letter.instruction_version
            letter.instruction_version = "changed-after-submit"
            replacement = CoverLetterModel(
                application_id=job.application.id,
                vacancy_id=job.application.vacancy_id,
                direction_id=job.application.direction_id,
                resume_id=job.application.resume_id,
                text=_letter(),
                instruction_version=selected_instruction_version,
                model_name="replacement-model",
                context_hash=letter.context_hash,
                state=CoverLetterState.READY,
            )
            session.add(replacement)
            session.flush()
            ApplicationAutomationService(session).record_result(
                job,
                HhApplyResult(HhApplyStatus.APPLIED, job.vacancy.source_url, "успешно"),
            )
            sent_letter = session.get(CoverLetterModel, letter.id)
            assert sent_letter is not None
            assert sent_letter.state == CoverLetterState.SENT
            assert sent_letter.sent_at is not None
            assert replacement.state == CoverLetterState.READY
            assert replacement.sent_at is None
    finally:
        database.close()


def test_prepare_uses_at_most_two_sources_and_links_only_reflected_fact(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel([_letter()])
    try:
        with database.sessions.begin() as session:
            account_id, _, resume_id, vacancy_ids = _prepare_data(session)
            profile = session.scalar(
                select(CandidateProfileModel).where(CandidateProfileModel.account_id == account_id)
            )
            vacancy = session.get(VacancyModel, vacancy_ids[0])
            assert profile is not None
            assert vacancy is not None
            work_fact_id = session.scalar(
                select(VerifiedFactModel.id).where(
                    VerifiedFactModel.profile_id == profile.id,
                    VerifiedFactModel.category == "work_experience",
                    VerifiedFactModel.state == ConfirmationState.CONFIRMED,
                )
            )
            assert work_fact_id is not None
            vacancy.key_skills = ["Python", "FastAPI", "PostgreSQL", "Redis"]
            vacancy.required_qualifications = "Python, FastAPI, PostgreSQL, Redis."
            session.add_all(
                (
                    VerifiedFactModel(
                        profile_id=profile.id,
                        category="about",
                        content=(
                            "Разрабатывал серверные приложения на Python, FastAPI и PostgreSQL."
                        ),
                        source_type="resume",
                        resume_id=resume_id,
                        state=ConfirmationState.CONFIRMED,
                        allow_in_letters=True,
                    ),
                    VerifiedFactModel(
                        profile_id=profile.id,
                        category="skills",
                        content="Redis",
                        source_type="resume",
                        resume_id=resume_id,
                        state=ConfirmationState.CONFIRMED,
                        allow_in_letters=True,
                    ),
                    VerifiedFactModel(
                        profile_id=profile.id,
                        category="education",
                        content="Бакалавр нефтегазового дела.",
                        source_type="resume",
                        resume_id=resume_id,
                        state=ConfirmationState.CONFIRMED,
                        allow_in_letters=True,
                    ),
                )
            )
            session.flush()

            result = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
            )

            assert result.generated == 1
            assert model.prompts[0][1].count("<fact id=") == 2
            assert "Redis" in model.prompts[0][1]
            letter = session.scalar(select(CoverLetterModel))
            assert letter is not None
            linked_ids = tuple(
                session.scalars(
                    select(CoverLetterFactModel.fact_id).where(
                        CoverLetterFactModel.cover_letter_id == letter.id
                    )
                )
            )
            assert linked_ids == (work_fact_id,)
    finally:
        database.close()


def test_manually_reviewed_letter_is_revalidated_and_saved(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, _, _, _ = _prepare_data(session)
            CoverLetterService(session, FakeModel([_letter()])).prepare(
                account_id=account_id,
                direction_name="Python backend",
            )
            letter = session.scalar(select(CoverLetterModel))
            assert letter is not None
            reviewed_text = _letter().replace("Буду рад", "Готов")

            saved = CoverLetterService(session).save_reviewed(
                account_id=account_id,
                letter_id=letter.id,
                text=reviewed_text,
            )

            assert saved.state is CoverLetterState.READY
            assert saved.text == reviewed_text
            assert saved.model_name == MANUAL_REVIEW_MODEL
            assert saved.context_hash

            replacement_model = FakeModel([_letter()])
            repeated = CoverLetterService(session, replacement_model).prepare(
                account_id=account_id,
                direction_name="Python backend",
            )

            assert repeated.already_ready == 1
            assert replacement_model.prompts == []
            assert saved.text == reviewed_text
    finally:
        database.close()


def test_ready_model_letter_without_current_prompt_version_is_regenerated(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel([_letter(), _letter().replace("Буду рад", "Готов")])
    try:
        with database.sessions.begin() as session:
            account_id, _, _, _ = _prepare_data(session)
            CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
            )
            letter = session.scalar(select(CoverLetterModel))
            assert letter is not None
            letter.prompt_version_id = None
            session.flush()

            repeated = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
            )

            assert repeated.generated == 1
            assert repeated.already_ready == 0
            assert len(model.prompts) == 2
            assert letter.prompt_version_id is not None
            assert letter.text == _letter().replace("Буду рад", "Готов")
    finally:
        database.close()


def test_unconfirmed_number_fails_without_fallback(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel(
        [
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. У меня 5 лет опыта, поэтому "
            "задачи серверной разработки хорошо знакомы. Также реализовывал прикладную логику "
            "и интеграции. Буду рад подробнее рассказать о проектах и обсудить задачи команды."
        ]
    )
    try:
        with database.sessions.begin() as session:
            account_id, direction_id, _, _ = _prepare_data(session)
            result = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
            )

            assert result.failed == 1
            letter = session.scalar(select(CoverLetterModel))
            assert letter is not None
            assert letter.state is CoverLetterState.FAILED
            assert letter.text is None
            assert letter.failure_reason == "UNCONFIRMED_NUMBER"
            assert len(model.prompts) == 1
            assert (
                ApplicationAutomationService(session).claim_next(
                    direction_id,
                    require_cover_letter=True,
                )
                is None
            )
    finally:
        database.close()


def test_required_library_name_with_digit_is_allowed_from_vacancy() -> None:
    vacancy = _vacancy()
    vacancy.description = "Стек проекта включает psycopg3."
    text = _letter().replace(
        "Буду рад подробнее обсудить",
        "С psycopg3 готов быстро освоиться. Буду рад подробнее обсудить",
    )

    validate_cover_letter(text, vacancy, _fact())


def test_library_name_does_not_allow_same_digit_as_experience_claim() -> None:
    vacancy = _vacancy()
    vacancy.description = "Стек проекта включает psycopg3."
    text = _letter().replace(
        "Буду рад подробнее обсудить",
        "С psycopg3 готов быстро освоиться. У меня 3 года опыта. Буду рад подробнее обсудить",
    )

    with pytest.raises(CoverLetterValidationError) as error:
        validate_cover_letter(text, vacancy, _fact())

    assert error.value.code == "UNCONFIRMED_NUMBER"


def test_template_phrase_is_corrected_once_with_specific_reason(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel([_letter_with_template_phrase(), _letter()])
    try:
        with database.sessions.begin() as session:
            account_id, _, _, _ = _prepare_data(session)

            result = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
                limit=1,
            )

            assert result.generated == 1
            assert result.failed == 0
            assert len(model.prompts) == 2
            first_prompt = model.prompts[0][1]
            correction_prompt = model.prompts[1][1]
            assert correction_prompt.startswith(first_prompt)
            assert "Код проверки: TEMPLATE_PHRASE" in correction_prompt
            assert "запрещённая шаблонная фраза «вижу, что»" in correction_prompt
            assert _letter_with_template_phrase() not in correction_prompt
            assert result.items[0].reason is not None
            assert "«вижу, что»" in result.items[0].reason

            letter = session.scalar(select(CoverLetterModel))
            assert letter is not None
            assert letter.state is CoverLetterState.READY
            assert letter.text == _letter()
            assert letter.failure_reason is None
    finally:
        database.close()


def test_failed_template_correction_requires_review_and_stops_retries(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    unconfirmed_number = (
        "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
        "с PostgreSQL и настраивал автоматические проверки. У меня 5 лет опыта, поэтому "
        "задачи серверной разработки хорошо знакомы. Также реализовывал прикладную логику "
        "и интеграции. Буду рад подробнее рассказать о проектах и обсудить задачи команды."
    )
    model = FakeModel(
        [
            _letter_with_template_phrase(),
            unconfirmed_number,
            _letter(),
        ]
    )
    try:
        with database.sessions.begin() as session:
            account_id, _, _, _ = _prepare_data(session)
            service = CoverLetterService(session, model)

            result = service.prepare(
                account_id=account_id,
                direction_name="Python backend",
                limit=1,
            )

            assert result.generated == 0
            assert result.failed == 1
            assert len(model.prompts) == 2
            assert len(model.responses) == 1
            assert "не прошёл проверку" in (result.items[0].reason or "")

            letter = session.scalar(select(CoverLetterModel))
            task = session.scalar(select(ApplicationTaskModel))
            assert letter is not None
            assert letter.state is CoverLetterState.FAILED
            assert letter.text is None
            assert (
                letter.failure_reason
                == "COVER_LETTER_RETRY_FAILED:TEMPLATE_PHRASE->UNCONFIRMED_NUMBER"
            )
            assert task is not None
            assert task.state is TaskState.REVIEW_REQUIRED
            assert task.last_error_code == "COVER_LETTER_RETRY_FAILED"

            repeated = service.prepare(
                account_id=account_id,
                direction_name="Python backend",
                limit=1,
            )
            assert repeated.generated == 0
            assert repeated.failed == 0
            assert repeated.items == ()
            assert len(model.prompts) == 2
            assert len(model.responses) == 1
    finally:
        database.close()


def test_related_publication_is_not_prepared_twice(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel([_letter()])
    try:
        with database.sessions.begin() as session:
            account_id, _, _, vacancy_ids = _prepare_data(session, with_duplicate=True)
            result = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
            )

            assert result.generated == 1
            assert result.reused == 0
            assert len(model.prompts) == 1
            letters = list(session.scalars(select(CoverLetterModel).order_by(CoverLetterModel.id)))
            applications = list(session.scalars(select(ApplicationModel)))
            assert len(letters) == 1
            assert len(applications) == 1
            assert applications[0].vacancy_id == vacancy_ids[0]
            assert letters[0].application_id == applications[0].id
    finally:
        database.close()


def test_unrelated_near_duplicate_is_rejected_without_second_model_call(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel([_letter(), _letter().replace("Буду рад", "Готов")])
    try:
        with database.sessions.begin() as session:
            account_id, direction_id, _, _ = _prepare_data(session)
            first = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
                vacancy_hh_id="letter-1",
            )
            assert first.generated == 1

            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="letter-unrelated",
                    title="Python-разработчик внутренних интеграций",
                    source_url="https://hh.ru/vacancy/letter-unrelated",
                    employer_name="Другая компания",
                    published_at=datetime(2026, 7, 24, tzinfo=UTC),
                    description=("Разработка интеграций, фоновых задач и внутренних сервисов."),
                    responsibilities="Развивать интеграции и фоновые задачи.",
                    required_qualifications="Python, FastAPI, PostgreSQL.",
                    key_skills=("Python", "FastAPI", "PostgreSQL"),
                    details_fetched_at=datetime(2026, 7, 24, tzinfo=UTC),
                )
            )
            directions = DirectionRepository(session)
            directions.track_vacancy(direction_id, vacancy.id)
            directions.apply_rules(
                direction_id,
                vacancy.id,
                state=VacancyState.ANALYZED,
                score=85,
                details={
                    "category": "MATCH",
                    "accepted": True,
                    "reasons": ["совпадают Python, FastAPI и PostgreSQL"],
                },
                rules_version=RULES_VERSION,
            )
            ApplicationAutomationService(session).prepare_for_account_id(
                account_id=account_id,
                direction_name="Python backend",
                include_stretch=True,
            )

            second = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
                vacancy_hh_id="letter-unrelated",
            )

            assert second.failed == 1
            failed_letter = session.scalar(
                select(CoverLetterModel)
                .join(VacancyModel, VacancyModel.id == CoverLetterModel.vacancy_id)
                .where(VacancyModel.hh_id == "letter-unrelated")
            )
            assert failed_letter is not None
            assert failed_letter.failure_reason == "NEAR_DUPLICATE_TEXT"
            assert len(model.prompts) == 2
    finally:
        database.close()


def test_prepare_can_target_exactly_one_vacancy(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel([_letter()])
    try:
        with database.sessions.begin() as session:
            account_id, _, _, _ = _prepare_data(session, with_duplicate=True)
            result = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
                limit=20,
                vacancy_hh_id="letter-1",
            )

            assert result.generated == 1
            assert len(result.items) == 1
            assert result.items[0].hh_id == "letter-1"
            assert len(model.prompts) == 1

            with pytest.raises(LookupError, match="missing"):
                CoverLetterService(session, model).prepare(
                    account_id=account_id,
                    direction_name="Python backend",
                    vacancy_hh_id="missing",
                )
            assert len(model.prompts) == 1
    finally:
        database.close()


def test_prepare_targets_exact_application_when_resumes_share_vacancy(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel([_letter()])
    try:
        with database.sessions.begin() as session:
            account_id, direction_id, first_resume_id, vacancy_ids = _prepare_data(session)
            second_resume = ResumeRepository(session).upsert(
                account_id,
                "resume-letters-second",
                "Python-разработчик второй вариант",
            )
            DirectionRepository(session).attach_resume(direction_id, second_resume.id, priority=1)
            second_application = ApplicationRepository(session).create_apply_intent(
                account_id,
                vacancy_ids[0],
                second_resume.id,
                direction_id,
            )
            QueueTaskRepository(session).enqueue(second_application.id, 84)
            profile = session.scalar(
                select(CandidateProfileModel).where(CandidateProfileModel.account_id == account_id)
            )
            assert profile is not None
            session.add(
                VerifiedFactModel(
                    profile_id=profile.id,
                    category="work_experience",
                    content=(
                        "Разрабатывал серверные приложения на Python с FastAPI и PostgreSQL. "
                        "Реализовывал прикладную логику и настраивал автоматические проверки."
                    ),
                    source_type="resume",
                    resume_id=second_resume.id,
                    state=ConfirmationState.CONFIRMED,
                    allow_in_letters=True,
                )
            )
            session.flush()

            result = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
                vacancy_hh_id="letter-1",
                application_id=second_application.id,
                limit=1,
            )

            assert result.generated == 1
            letters = tuple(session.scalars(select(CoverLetterModel)))
            assert len(letters) == 1
            assert letters[0].application_id == second_application.id
            assert letters[0].resume_id == second_resume.id
            first_application_id = session.scalar(
                select(ApplicationModel.id).where(
                    ApplicationModel.resume_id == first_resume_id,
                    ApplicationModel.vacancy_id == vacancy_ids[0],
                )
            )
            assert first_application_id is not None
            assert not any(letter.application_id == first_application_id for letter in letters)
            assert len(model.prompts) == 1
    finally:
        database.close()


def test_stale_automatic_letter_returns_to_preflight_after_session_restart(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel([_letter()])
    try:
        with database.sessions.begin() as session:
            account_id, direction_id, resume_id, _ = _prepare_data(session)
            prepared = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
                limit=1,
            )
            assert prepared.generated == 1
            letter = session.scalar(select(CoverLetterModel))
            application = session.scalar(select(ApplicationModel))
            task = session.scalar(select(ApplicationTaskModel))
            resume = session.get(ResumeModel, resume_id)
            assert letter is not None
            assert application is not None
            assert task is not None
            assert resume is not None
            resume.updated_at = letter.updated_at + timedelta(seconds=1)
            session.flush()
            SystemStateRepository(session).transition(SystemState.RUNNING)

            assert (
                ApplicationAutomationService(session).claim_next(
                    direction_id,
                    require_cover_letter=True,
                )
                is None
            )
            application_id = application.id
            task_id = task.id
            letter_id = letter.id

        with database.sessions.begin() as restarted_session:
            restarted_letter = restarted_session.get(CoverLetterModel, letter_id)
            restarted_task = restarted_session.get(ApplicationTaskModel, task_id)
            assert restarted_letter is not None
            assert restarted_letter.state is CoverLetterState.FAILED
            assert restarted_letter.text is None
            assert restarted_letter.failure_reason == "COVER_LETTER_STALE"
            assert restarted_task is not None
            assert restarted_task.state is TaskState.RETRY_SCHEDULED
            restarted = ApplicationAutomationService(restarted_session)
            assert restarted.recover_interrupted() == 0

            preflight = restarted.claim_next_form_preflight(account_id=account_id)

            assert preflight is not None
            assert preflight.application.id == application_id
            assert preflight.task.id == task_id
    finally:
        database.close()


class FailingModel:
    model_name = "yandexgpt-test"

    def complete(self, _system_prompt: str, _user_prompt: str) -> str:
        from hugin.adapters.yandex_ai import YandexAIError

        raise YandexAIError("временная ошибка")


def test_yandex_failure_is_saved_without_common_letter(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, _, _, _ = _prepare_data(session)
            result = CoverLetterService(session, FailingModel()).prepare(
                account_id=account_id,
                direction_name="Python backend",
            )

            assert result.failed == 1
            letter = session.scalar(select(CoverLetterModel))
            assert letter is not None
            assert letter.state == CoverLetterState.FAILED
            assert letter.text is None
            assert letter.failure_reason == "YANDEXGPT_ERROR"
    finally:
        database.close()


def _vacancy() -> VacancyModel:
    return VacancyModel(
        id=10,
        hh_id="validation",
        title="Python-разработчик",
        source_url="https://hh.ru/vacancy/validation",
        employer_name="Тестовая компания",
        key_skills=["Python", "FastAPI", "PostgreSQL"],
    )


def _fact() -> tuple[_SelectedFact, ...]:
    return (
        _SelectedFact(
            id=1,
            category="work_experience",
            content="Разрабатывал серверные приложения на Python и FastAPI. Работал с PostgreSQL.",
        ),
    )


@pytest.mark.parametrize(
    "description",
    [
        "В сопроводительном письме обязательно ответьте на три вопроса.",
        "В отклике укажите ожидаемый доход и доступную дату начала.",
        "Отклик должен содержать ссылки на два примера кода.",
        "Обязательно напишите в отклике кодовое слово и ожидаемый доход.",
        "Без ответов на вопросы отклик не рассматривается.",
        "Как откликнуться:\nНапиши короткое сообщение:\n1. О себе\n2. Ссылка на код.",
        (
            "В сопроводительном письме коротко:\n"
            "1) как используете средства разработки;\n"
            "2) пример выполненной задачи."
        ),
        (
            "Отклик должен сопровождаться сопроводительным письмом "
            "со ссылками на ваши работы и информацией о себе."
        ),
        "Откликайся и опиши опыт ИМЕННО С ИНТЕГРАЦИЕЙ ДЛЯ МАРКЕТПЛЕЙСОВ.",
        "При отклике, пожалуйста, укажите Telegram.",
        "Пожалуйста, заполните данную форму https://forms.gle/example.",
    ],
)
def test_mandatory_letter_answers_require_manual_review(description: str) -> None:
    vacancy = _vacancy()
    vacancy.description = description

    with pytest.raises(CoverLetterValidationError) as error:
        _ensure_relevant_evidence(vacancy, _fact())

    assert error.value.code == "MANUAL_INPUT_REQUIRED"


def test_plain_cover_letter_requirement_is_autonomous() -> None:
    vacancy = _vacancy()
    vacancy.description = "Отклик должен сопровождаться сопроводительным письмом."

    _ensure_relevant_evidence(vacancy, _fact())


def test_relevance_guard_accepts_confirmed_strong_skill() -> None:
    vacancy = _vacancy()
    vacancy.title = "Разработчик хранилища"
    vacancy.key_skills = ["Redis"]
    facts = (
        _SelectedFact(
            id=1,
            category="work_experience",
            content="Использовал Redis для хранения активной корзины.",
        ),
    )

    _ensure_relevant_evidence(vacancy, facts)


def test_relevance_guard_accepts_two_specific_task_terms() -> None:
    vacancy = _vacancy()
    vacancy.title = "Инженер платежей"
    vacancy.key_skills = []
    vacancy.responsibilities = "Обработка платежей и внешние интеграции."
    facts = (
        _SelectedFact(
            id=1,
            category="work_experience",
            content="Реализовал обработку платежей и внешние интеграции.",
        ),
    )

    _ensure_relevant_evidence(vacancy, facts)


def test_relevance_guard_uses_description_when_structured_fields_are_empty() -> None:
    vacancy = _vacancy()
    vacancy.title = "Стажёр в отдел разработки"
    vacancy.key_skills = []
    vacancy.required_qualifications = None
    vacancy.responsibilities = None
    vacancy.description = "Базовое владение Python и понимание FastAPI."

    _ensure_relevant_evidence(vacancy, _fact())


def test_relevance_guard_rejects_unrelated_confirmed_facts() -> None:
    vacancy = _vacancy()
    vacancy.title = "Java-разработчик"
    vacancy.key_skills = ["Java", "Spring"]

    with pytest.raises(CoverLetterValidationError) as error:
        _ensure_relevant_evidence(vacancy, _fact())

    assert error.value.code == "NO_RELEVANT_EVIDENCE"


def test_relevance_guard_rejects_python_as_the_only_overlap() -> None:
    vacancy = _vacancy()
    vacancy.key_skills = ["Python"]
    vacancy.required_qualifications = "Требуется Python."

    with pytest.raises(CoverLetterValidationError) as error:
        _ensure_relevant_evidence(vacancy, _fact())

    assert error.value.code == "NO_RELEVANT_EVIDENCE"


def test_unchanged_manual_failure_does_not_block_next_vacancy(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel([_letter()])
    try:
        with database.sessions.begin() as session:
            account_id, direction_id, _, vacancy_ids = _prepare_data(session)
            second_vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="letter-next",
                    title="Python-разработчик интеграций",
                    source_url="https://hh.ru/vacancy/letter-next",
                    employer_name="Другая компания",
                    published_at=datetime(2026, 7, 21, tzinfo=UTC),
                    description=(
                        "Разработка серверных интеграций на Python, FastAPI и PostgreSQL."
                    ),
                    responsibilities="Развивать серверные интеграции.",
                    required_qualifications="Python, FastAPI, PostgreSQL.",
                    key_skills=("Python", "FastAPI", "PostgreSQL"),
                    details_fetched_at=datetime(2026, 7, 21, tzinfo=UTC),
                )
            )
            directions = DirectionRepository(session)
            directions.track_vacancy(direction_id, second_vacancy.id)
            directions.apply_rules(
                direction_id,
                second_vacancy.id,
                state=VacancyState.ANALYZED,
                score=84,
                details={
                    "category": "MATCH",
                    "accepted": True,
                    "reasons": ["совпадают Python, FastAPI и PostgreSQL"],
                },
                rules_version=RULES_VERSION,
            )
            ApplicationAutomationService(session).prepare_for_account_id(
                account_id=account_id,
                direction_name="Python backend",
                include_stretch=True,
            )
            first_vacancy = session.get(VacancyModel, vacancy_ids[0])
            assert first_vacancy is not None
            first_vacancy.description = (
                "В сопроводительном письме обязательно ответьте на отдельные вопросы."
            )

            first = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
                limit=1,
            )
            second = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
                limit=1,
            )

            assert first.failed == 1
            assert second.generated == 1
            first_task = session.scalar(
                select(ApplicationTaskModel)
                .join(ApplicationModel)
                .where(ApplicationModel.vacancy_id == vacancy_ids[0])
            )
            assert first_task is not None
            assert first_task.state is TaskState.REVIEW_REQUIRED
            assert first_task.last_error_code == "MANUAL_INPUT_REQUIRED"
            assert len(model.prompts) == 1
    finally:
        database.close()


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "EMPTY"),
        ("Слишком коротко", "TOO_SHORT"),
        ("Я" * (MAX_LETTER_LENGTH + 1), "TOO_LONG"),
        (
            "Вот готовое письмо: заинтересовала вакансия Python-разработчика в Тестовой "
            "компании. Подтвержден опыт с Python и FastAPI. Готов обсудить задачи команды.",
            "SERVICE_TEXT",
        ),
        (
            "Здравствуйте!\n\nЗаинтересовала вакансия Python-разработчика в Тестовой компании. "
            "Подтвержден опыт с Python и FastAPI. [Укажите здесь достижение.] Буду рад "
            "обсудить задачи команды.",
            "PLACEHOLDER",
        ),
        (
            "Разрабатывал серверные приложения на Python и FastAPI, работал с PostgreSQL и "
            "настраивал автоматические проверки. Этот опыт связан с задачами развития "
            "серверной части и интеграций. Буду рад подробнее рассказать о реализованных "
            "решениях и обсудить задачи команды.",
            "MISSING_GREETING",
        ),
        (
            "Здравствуйте!\n\nВижу, что вы ищете разработчика серверных приложений. "
            "Разрабатывал сервисы на Python и FastAPI, работал с PostgreSQL и настраивал "
            "автоматические проверки. Этот опыт связан с развитием серверной части и "
            "интеграциями. Буду рад подробнее рассказать о выполненных проектах и обсудить "
            "задачи команды на собеседовании.",
            "TEMPLATE_PHRASE",
        ),
        (
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. Также интегрировал внешние "
            "сервисы через API. Этот опыт использовал при создании AI-агентов и RAG-модулей. "
            "Буду рад подробнее рассказать о выполненных проектах и обсудить задачи команды "
            "на собеседовании.",
            "UNCONFIRMED_SPECIALIST_TERM",
        ),
        (
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. У меня 5 лет опыта. Также "
            "реализовывал прикладную логику и интеграции. Буду рад подробнее рассказать о "
            "проектах и обсудить задачи команды.",
            "UNCONFIRMED_NUMBER",
        ),
        (
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. У меня пять лет опыта. Также "
            "реализовывал прикладную логику и интеграции. Буду рад подробнее рассказать о "
            "проектах и обсудить задачи команды.",
            "UNCONFIRMED_EXPERIENCE",
        ),
        (
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. Ранее сотрудничал с компанией "
            "«Чужая». Также реализовывал прикладную логику и интеграции. Буду рад подробнее "
            "рассказать о проектах и обсудить задачи команды.",
            "OTHER_EMPLOYER",
        ),
        (
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. Опыт работы с чужим кодом и "
            "доведение его до стабильной работы в проде — часть моей текущей практики. Также "
            "реализовывал прикладную логику и интеграции. Готов подробно обсудить выполненные "
            "проекты и подход к проверке результата.",
            "UNCONFIRMED_CLAIM",
        ),
        (
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. Ранее работал с микросервисной "
            "архитектурой, обеспечивал надежность взаимодействий и устойчивость к сбоям. "
            "Также реализовывал прикладную логику и интеграции. Готов подробно обсудить "
            "выполненные проекты и подход к проверке результата.",
            "UNCONFIRMED_CLAIM",
        ),
        (
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. Получил практический опыт "
            "работы с транзакциями и согласованностью данных. Также реализовывал прикладную "
            "логику и интеграции. Готов подробно обсудить выполненные проекты и подход к "
            "проверке результата.",
            "UNCONFIRMED_CLAIM",
        ),
        (
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. Обеспечивал целостность "
            "данных при высокой нагрузке и проверял прикладную логику. Также реализовывал "
            "интеграции и обработку ошибок. Готов подробно обсудить выполненные проекты "
            "и подход к проверке результата.",
            "UNCONFIRMED_CLAIM",
        ),
        (
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. Использовал асинхронное "
            "взаимодействие с базой данных через SQLAlchemy. Также реализовывал прикладную "
            "логику и обработку ошибок. Готов подробно обсудить выполненные проекты "
            "и подход к проверке результата.",
            "UNCONFIRMED_CLAIM",
        ),
    ],
)
def test_objective_letter_validation(text: str, code: str) -> None:
    with pytest.raises(CoverLetterValidationError) as error:
        validate_cover_letter(text, _vacancy(), _fact())
    assert error.value.code == code


def test_prompt_normalization_and_context_selection() -> None:
    assert normalize_cover_letter('```text\n"Готовый текст"\n```') == "Готовый текст"
    assert normalize_cover_letter("«Еще один текст»") == "Еще один текст"

    long_context = (
        "Общая строка без совпадения.\n" * 20
        + "Разрабатывал интеграции на FastAPI и Python.\n"
        + "PostgreSQL использовал для хранения данных."
    )
    excerpt = _relevant_excerpt(long_context, {"python", "fastapi"}, 150)
    assert "FastAPI" in excerpt
    fallback = _relevant_excerpt("Оченьдлиннаястрока" * 20, {"python"}, 20)
    assert fallback
    minimal = _relevant_excerpt(
        "ООО Предыдущий работодатель\nРазрабатывал сервис на Python\nОбщая информация",
        {"python"},
        200,
        minimal=True,
    )
    assert "Предыдущий работодатель" not in minimal
    assert "Разрабатывал сервис" in minimal

    prompt = build_cover_letter_prompt(
        _vacancy(),
        "Python backend",
        None,
        _fact(),
    )
    assert "Причины совпадения отдельно не выделены" in prompt
    assert "Полное описание" not in prompt
    assert "не повторяй название вакансии и компании" in prompt
    assert "1–2 наиболее подходящих проекта" in prompt
    assert "не смешивай сведения разных должностей и проектов" in prompt
    assert "нет требуемой технологии" in prompt
    assert "список навыков подтверждает знание технологии" in prompt
    assert "Здравствуйте!" in prompt


def test_letter_similarity_detects_paraphrased_template_but_not_distinct_evidence() -> None:
    first = (
        "Здравствуйте!\n\nРазрабатываю сервис на Python и FastAPI, работаю с PostgreSQL "
        "через SQLAlchemy и поддерживаю миграции Alembic. Реализовал повторную проверку "
        "цен и остатков и защиту от потери изменений при одновременных запросах. "
        "Проверяю сервис тестами на pytest и запускаю через Docker Compose."
    )
    paraphrase = (
        "Здравствуйте!\n\nНа Python и FastAPI разрабатываю сервис, использую PostgreSQL "
        "и SQLAlchemy, а схему обновляю через Alembic. Добавил повторную проверку цен "
        "и остатков и защитил одновременные изменения от потери. Сценарии проверяю "
        "на pytest, окружение запускаю через Docker Compose."
    )
    distinct = (
        "Здравствуйте!\n\nДля SmartPVD подготовил производственные данные и разработал "
        "алгоритм анализа влияния скважин. Проверил результат на независимых выборках, "
        "после чего представил работу на внутренней конференции. Этот опыт относится "
        "к обработке данных и проверке моделей."
    )

    assert _letter_similarity(first, paraphrase) >= 0.75
    assert _letter_similarity(first, distinct) < 0.80
    assert _letter_similarity("", distinct) == 0.0
    assert _set_similarity(set(), {"python"}) == 0.0


def test_work_experience_context_keeps_project_boundaries() -> None:
    content = """Январь 2024 — настоящее время
1 год
Компания
Разработчик
- Разрабатывал сервисы на Python.
Проекты:
- Цифровой подкастер — генерировал аудио из текста через SpeechKit.
- Аналитик — собирал новости и формировал отчет через LLM.
Стек: Python, LLM, SpeechKit."""

    excerpt = _work_experience_excerpt(
        content,
        {"llm", "speechkit", "новости", "аудио"},
        3000,
    )

    assert '<experience_item type="PROJECT" label="Цифровой подкастер">' in excerpt
    assert "генерировал аудио из текста через SpeechKit" in excerpt
    assert '<experience_item type="PROJECT" label="Аналитик">' in excerpt
    assert "собирал новости и формировал отчет через LLM" in excerpt


def test_work_experience_context_drops_much_weaker_role() -> None:
    content = """Январь 2026 — настоящее время
Компания
Python backend-разработчик
- Разрабатываю REST API на Python и FastAPI.
- Работаю с PostgreSQL через SQLAlchemy.
Август 2025 — декабрь 2025
Компания
Специалист по автоматизации
- Создавал прототипы с LLM."""

    excerpt = _work_experience_excerpt(
        content,
        {"python", "fastapi", "postgresql", "sqlalchemy", "rest", "api"},
        3000,
    )

    assert "Разрабатываю REST API" in excerpt
    assert "прототипы с LLM" not in excerpt


def test_work_experience_context_keeps_relevant_second_role() -> None:
    content = """Январь 2026 — настоящее время
Компания
Python backend-разработчик
- Разрабатываю REST API на Python и FastAPI.
- Работаю с PostgreSQL через SQLAlchemy.
Август 2025 — декабрь 2025
Компания
Специалист по автоматизации
- Создавал backend-прототипы с LLM.
- Интегрировал WebSocket и внешние API."""

    excerpt = _work_experience_excerpt(
        content,
        {"python", "fastapi", "postgresql", "sqlalchemy", "llm", "websocket", "api"},
        3000,
    )

    assert "Разрабатываю REST API" in excerpt
    assert "backend-прототипы с LLM" in excerpt


def test_work_experience_context_keeps_relevant_company_first_role() -> None:
    content = """PointPulse
Python backend-разработчик
январь 2026 — настоящее время
- Разрабатываю REST API на Python и FastAPI.
- Работаю с PostgreSQL через SQLAlchemy.
Яндекс Крауд
Специалист по автоматизации
август 2025 — декабрь 2025
- Создавал backend-прототипы с LLM и AI Studio.
- Интегрировал WebSocket и внешние API.
Газпромнефть
Ведущий специалист
август 2022 — июнь 2025
- Анализировал производственные данные, выявлял отклонения и причины.
- Проверял модели и повышал качество результатов."""

    excerpt = _work_experience_excerpt(
        content,
        {
            "python",
            "fastapi",
            "postgresql",
            "sqlalchemy",
            "llm",
            "websocket",
            "api",
            "данные",
            "данных",
            "качество",
            "результатов",
            "причины",
        },
        3000,
        priority_tokens={"python", "llm"},
    )

    assert 'label="Python backend-разработчик"' in excerpt
    assert "Разрабатываю REST API" in excerpt
    assert 'label="Специалист по автоматизации"' in excerpt
    assert "backend-прототипы с LLM и AI Studio" in excerpt
    assert "Интегрировал WebSocket" in excerpt
    assert "производственные данные" not in excerpt


def test_work_experience_context_prefers_new_llm_evidence_over_repeated_python() -> None:
    content = """PointPulse
Python backend-разработчик
январь 2026 — настоящее время
- Разрабатываю REST API на Python и FastAPI.
Яндекс Крауд
Специалист по автоматизации
август 2025 — декабрь 2025
- Создавал backend-прототипы с LLM.
- Интегрировал WebSocket и внешние API.
Газпромнефть
Ведущий специалист
август 2022 — июнь 2025
Проект SmartPVD:
- Разработал алгоритм анализа данных.
Стек: Python, pandas, numpy."""

    excerpt = _work_experience_excerpt(
        content,
        {"python", "fastapi", "llm", "websocket", "api", "данных"},
        3000,
        priority_tokens={"python"},
    )

    assert "Разрабатываю REST API" in excerpt
    assert "backend-прототипы с LLM" in excerpt
    assert "SmartPVD" not in excerpt


def test_irrelevant_context_is_removed_from_fact_before_generation() -> None:
    content = """Разрабатываю REST API на Python и FastAPI.
Создавал прототипы в Yandex Cloud и подключал AI Studio.
Работаю с PostgreSQL через SQLAlchemy."""

    cleaned = _without_irrelevant_context_lines(
        content,
        "Разработка REST API на Python, FastAPI и PostgreSQL.",
    )

    assert "REST API" in cleaned
    assert "PostgreSQL" in cleaned
    assert "Yandex Cloud" not in cleaned
    assert "AI Studio" not in cleaned


def test_future_technology_is_removed_from_letter_context() -> None:
    content = """Разработчик
- Разрабатываю сервис на Python и FastAPI.
- Заложил возможность публикации событий в Kafka как опциональную фичу.
Стек: Python, FastAPI, Kafka, PostgreSQL."""

    cleaned = _without_future_plans(content)

    assert "Kafka" not in cleaned
    assert "Python" in cleaned
    assert "FastAPI" in cleaned


def test_irrelevant_cloud_details_are_rejected() -> None:
    text = (
        "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
        "с PostgreSQL и настраивал автоматические проверки. Также создавал прототипы в "
        "Yandex Cloud и подключал AI Studio, хотя эти средства не относятся к основным "
        "задачам команды. Реализовывал прикладную логику и обработку ошибок.\n\n"
        "Буду рад подробнее обсудить серверную часть и интеграции."
    )
    facts = (
        _SelectedFact(
            1,
            "work_experience",
            "Python, FastAPI, PostgreSQL, Yandex Cloud и AI Studio.",
        ),
    )

    with pytest.raises(CoverLetterValidationError) as error:
        validate_cover_letter(text, _vacancy(), facts)

    assert error.value.code == "IRRELEVANT_DETAIL"


@pytest.mark.parametrize(
    ("technology", "description"),
    [
        ("gRPC", "Разработка межсервисных интеграций через gRPC."),
        ("OpenTelemetry", "Настройка трассировки через OpenTelemetry."),
        ("Yandex Cloud", "Разработка облачных служб в Yandex Cloud."),
        ("OpenAI", "Разработка ИИ-служб с использованием OpenAI."),
    ],
)
def test_relevant_but_unconfirmed_technology_is_rejected(
    technology: str,
    description: str,
) -> None:
    text = (
        "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
        f"с PostgreSQL. В проекте также применял {technology} для задач этой команды. "
        "Реализовывал прикладную логику, обработку ошибок и автоматические проверки.\n\n"
        "Буду рад подробнее обсудить задачи серверной части и реализованные решения."
    )
    vacancy = _vacancy()
    vacancy.description = description

    with pytest.raises(CoverLetterValidationError) as error:
        validate_cover_letter(text, vacancy, _fact())

    assert error.value.code == "UNCONFIRMED_SPECIALIST_TERM"


def test_specialist_term_boundaries_do_not_match_storage() -> None:
    text = (
        "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
        "с PostgreSQL и настраивал автоматические проверки. Для storage-слоя применял "
        "PostgreSQL, а прикладную логику отделял от доступа к данным. Также реализовывал "
        "интеграции и обработку ошибок.\n\nБуду рад подробнее рассказать о выполненных "
        "проектах и обсудить задачи команды на собеседовании."
    )

    validate_cover_letter(text, _vacancy(), _fact())


@pytest.mark.parametrize(
    ("technology", "claim"),
    [
        ("Django", "В другом проекте разрабатывал серверную часть на Django."),
        ("Kafka", "В другом проекте интегрировал Kafka для обмена событиями."),
    ],
)
def test_skill_list_does_not_confirm_technology_experience(
    technology: str,
    claim: str,
) -> None:
    text = (
        "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
        f"с PostgreSQL. {claim} Настраивал автоматические проверки и обработку ошибок.\n\n"
        "Буду рад подробнее обсудить задачи серверной части и реализованные решения."
    )
    facts = (
        *_fact(),
        _SelectedFact(2, "skills", technology),
    )

    with pytest.raises(CoverLetterValidationError) as error:
        validate_cover_letter(text, _vacancy(), facts)

    assert error.value.code == "UNCONFIRMED_TECHNOLOGY_EXPERIENCE"


def test_confirmed_work_fact_supports_technology_experience() -> None:
    text = (
        "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и Django, работал "
        "с PostgreSQL и интегрировал Kafka для обмена событиями. Настраивал автоматические "
        "проверки и обработку ошибок. При доработке таких служб отделял прикладную логику "
        "от доступа к данным и проверял основные сценарии перед выпуском изменений.\n\n"
        "Буду рад подробнее обсудить задачи серверной части и реализованные решения."
    )
    facts = (
        _SelectedFact(
            1,
            "work_experience",
            (
                "Разрабатывал серверные приложения на Python и Django. Работал с PostgreSQL. "
                "Интегрировал Kafka для обмена событиями."
            ),
        ),
    )

    validate_cover_letter(text, _vacancy(), facts)


def test_skill_list_can_confirm_knowledge_without_claiming_experience() -> None:
    text = (
        "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
        "с PostgreSQL и настраивал автоматические проверки. Знаю основы Django. При доработке "
        "служб отделял прикладную логику от доступа к данным, проверял обработку ошибок "
        "и основные сценарии перед выпуском изменений.\n\nБуду рад подробнее обсудить задачи "
        "серверной части и реализованные решения."
    )
    facts = (
        *_fact(),
        _SelectedFact(2, "skills", "Django"),
    )

    validate_cover_letter(text, _vacancy(), facts)
