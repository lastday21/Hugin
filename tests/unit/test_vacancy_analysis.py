from __future__ import annotations

from decimal import Decimal

import pytest

from hugin.domain.directions import DirectionScope, SearchRegion, WorkFormat
from hugin.domain.vacancies import VacancyAvailability, VacancyData
from hugin.services.vacancy_analysis import (
    RULES_VERSION,
    AdjacentItRules,
    PythonBackendRules,
    RuleCategory,
    RuleContext,
)


def test_rules_version_is_python_it_v18() -> None:
    assert RULES_VERSION == "python_it_v18"


@pytest.mark.parametrize(
    ("vacancy", "reason"),
    [
        (
            VacancyData(
                "2",
                "Продуктовый аналитик",
                "https://hh.ru/vacancy/2",
                description="Используем Python и SQL",
            ),
            "другое направление: аналитика",
        ),
        (
            VacancyData(
                "3",
                "Backend-разработчик",
                "https://hh.ru/vacancy/3",
                description="Разработка на Go",
            ),
            "Python не указан",
        ),
    ],
)
def test_rules_reject_irrelevant_vacancies(vacancy: VacancyData, reason: str) -> None:
    result = PythonBackendRules().evaluate(vacancy)

    assert not result.accepted
    assert result.category is RuleCategory.REJECTED
    assert any(reason in item for item in result.reasons)


def test_rules_accept_junior_python_backend_with_explanation() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "4",
            "Python backend разработчик",
            "https://hh.ru/vacancy/4",
            description="Разработка службы на FastAPI и PostgreSQL",
            experience="Опыт 1\N{EN DASH}3 года",
            work_format="Формат работы: удалённо",
            key_skills=("Python", "FastAPI", "PostgreSQL", "Docker"),
        )
    )

    assert result.accepted
    assert result.category is RuleCategory.MATCH
    assert result.score >= 55
    assert "Python указан в названии" in result.reasons


def test_three_to_six_years_is_not_a_rejection_for_non_senior_role() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "5",
            "Python-разработчик",
            "https://hh.ru/vacancy/5",
            description="Backend на FastAPI и PostgreSQL",
            experience="Опыт 3\N{EN DASH}6 лет",
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        )
    )

    assert result.accepted
    assert result.category is RuleCategory.MATCH
    assert any("пожелание" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "title",
    [
        "Senior Python developer",
        "Lead Python developer",
        "Tech\N{NO-BREAK SPACE}Lead / Backend Python",
        "Principal Backend Engineer (Python)",
        "Python техлид",
        "Ведущий Python-разработчик",
        "Старший инженер-разработчик Python",
        "Главный разработчик Python",
        "Middle+/Senior Python Developer",
        "Middle+ Python Developer",
    ],
)
def test_higher_level_titles_are_stretch_without_excessive_responsibility(title: str) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"higher-level-{title}",
            title,
            "https://hh.ru/vacancy/higher-level",
            description="Python backend на FastAPI и PostgreSQL",
            experience="От 3 до 6 лет",
        )
    )

    assert result.category is RuleCategory.STRETCH
    assert any("сам по себе не блокирует" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "title",
    [
        "Head of Python Development",
        "Backend Architect (Python)",
        "Руководитель команды разработки Python",
        "Архитектор-разработчик Python",
    ],
)
def test_managerial_or_architect_titles_are_rejected(title: str) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"managerial-level-{title}",
            title,
            "https://hh.ru/vacancy/managerial-level",
            description="Python backend на FastAPI и PostgreSQL",
            experience="От 3 до 6 лет",
        )
    )

    assert result.category is RuleCategory.REJECTED
    assert any("руководящая или архитектурная" in reason for reason in result.reasons)


def test_senior_with_excessive_responsibility_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "senior-manager",
            "Senior Python developer",
            "https://hh.ru/vacancy/senior-manager",
            description=(
                "Руководить командой разработки, отвечать за найм и техническую стратегию продукта."
            ),
            experience="От 3 до 6 лет",
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        )
    )

    assert result.category is RuleCategory.REJECTED
    assert any("обязанностями, существенно выше" in reason for reason in result.reasons)


def test_senior_without_team_management_is_not_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "senior-hands-on",
            "Senior Python developer",
            "https://hh.ru/vacancy/senior-hands-on",
            description=(
                "Разрабатывать backend на Python и FastAPI. "
                "Это hands-on роль без управления командой."
            ),
            experience="От 3 до 6 лет",
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        )
    )

    assert result.category is RuleCategory.STRETCH
    assert not any("обязанностями, существенно выше" in reason for reason in result.reasons)


def test_senior_role_not_assuming_team_management_is_not_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "senior-no-management",
            "Senior Python developer",
            "https://hh.ru/vacancy/senior-no-management",
            description=(
                "Разрабатывать backend на Python и FastAPI. "
                "Роль не предполагает управления командой."
            ),
            experience="От 3 до 6 лет",
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        )
    )

    assert result.category is RuleCategory.STRETCH
    assert not any("обязанностями, существенно выше" in reason for reason in result.reasons)


def test_senior_without_architecture_responsibility_is_not_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "senior-no-architecture",
            "Senior Python developer",
            "https://hh.ru/vacancy/senior-no-architecture",
            description=(
                "Разрабатывать backend на Python и FastAPI. Нет ответственности за архитектуру."
            ),
            experience="От 3 до 6 лет",
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        )
    )

    assert result.category is RuleCategory.STRETCH
    assert not any("обязанностями, существенно выше" in reason for reason in result.reasons)


def test_hidden_team_lead_responsibility_is_rejected() -> None:
    result = AdjacentItRules().evaluate(
        VacancyData(
            "hidden-lead",
            "Fullstack-разработчик",
            "https://hh.ru/vacancy/hidden-lead",
            description=(
                "Ищем лида. В подчинении 3 разработчика. "
                "Руководство командой и разработка API на Python и TypeScript."
            ),
            experience="3–6 лет",
        )
    )

    assert result.category is RuleCategory.REJECTED
    assert any("обязанностями, существенно выше" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "title",
    [
        "Quant Trader",
        "Наставник курса «ИИ-инженер»",
        "Технический специалист / Вайбкодер-инженер платформы запусков",
        "Вайбкодер / Junior разработчик (AI / no-code)",
        "Инженер внедрения",
        "Инженер ИБ (AppSec&Pentest)",
        "BI-разработчик",
        "Data Scientist в команду «Анализ цены»",
        "Администратор баз данных NoSQL",
        "Продюсер вебинаров (EdTech)",
        "Специалист технической поддержки",
        "Младший научный сотрудник (математик)",
        "Разработчик BigData",
        "DBA PostgreSQL",
        "Database Administrator/DBA Cassandra",
        "Менеджер по продажам / Диагност в онлайн-школу",
    ],
)
def test_obviously_different_primary_roles_are_rejected(title: str) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"other-role-{title}",
            title,
            "https://hh.ru/vacancy/other-role",
            description=(
                "Работа с Python, FastAPI, Docker и PostgreSQL. "
                "Интеграция сервисов и автоматизация процессов."
            ),
            key_skills=("Python", "FastAPI", "Docker", "PostgreSQL"),
        )
    )

    assert result.category is RuleCategory.REJECTED


@pytest.mark.parametrize(
    "title",
    [
        "DevOps-инженер (Python)",
        "Python AQA Engineer",
        "Дата-инженер",
    ],
)
def test_supported_technical_roles_are_routed_to_it(title: str) -> None:
    vacancy = VacancyData(
        f"supported-role-{title}",
        title,
        "https://hh.ru/vacancy/supported-role",
        description=(
            "Писать код на Python, автоматизировать проверки и процессы, "
            "работать с Docker, PostgreSQL и pytest."
        ),
        key_skills=("Python", "Docker", "PostgreSQL", "pytest"),
    )

    backend = PythonBackendRules().evaluate(vacancy)
    adjacent = AdjacentItRules().evaluate(vacancy)

    assert backend.category is RuleCategory.ROUTED
    assert adjacent.category in {RuleCategory.MATCH, RuleCategory.STRETCH}


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Junior Product Manager",
            "Работа с командой, которая создаёт сервисы на Python.",
        ),
        (
            "Педагог программирования Python",
            "Обучать школьников основам Python и проверять домашние задания.",
        ),
        (
            "Инженер сопровождения",
            "Поддерживать систему, читать журналы и иногда запускать Python-скрипты.",
        ),
        (
            "Vibe Coding Specialist",
            "Собирать решения на no-code платформах, Python будет плюсом.",
        ),
    ],
)
def test_non_development_primary_roles_are_rejected(
    title: str,
    description: str,
) -> None:
    vacancy = VacancyData(
        f"non-development-{title}",
        title,
        "https://hh.ru/vacancy/non-development",
        description=description,
    )

    assert PythonBackendRules().evaluate(vacancy).category is RuleCategory.REJECTED
    assert AdjacentItRules().evaluate(vacancy).category is RuleCategory.REJECTED


def test_no_code_development_and_testing_words_do_not_prove_coding() -> None:
    vacancy = VacancyData(
        "no-code-actions",
        "No-code разработчик",
        "https://hh.ru/vacancy/no-code-actions",
        description=(
            "Разрабатывать и тестировать бизнес-сценарии на no-code платформе. "
            "Собирать процессы из готовых визуальных блоков."
        ),
        key_skills=("No-code", "Тестирование"),
    )

    backend_result = PythonBackendRules().evaluate(vacancy)
    adjacent_result = AdjacentItRules().evaluate(vacancy)

    assert backend_result.category is RuleCategory.REJECTED
    assert adjacent_result.category is RuleCategory.REJECTED
    assert any("no-code" in reason for reason in backend_result.reasons)
    assert any("no-code" in reason for reason in adjacent_result.reasons)


def test_no_code_action_and_python_in_separate_sentences_do_not_prove_coding() -> None:
    vacancy = VacancyData(
        "no-code-separated-python",
        "No-code разработчик",
        "https://hh.ru/vacancy/no-code-separated-python",
        description=(
            "Разрабатывать и тестировать решения на no-code платформе. "
            "Python применяется для интеграций."
        ),
    )

    backend_result = PythonBackendRules().evaluate(vacancy)
    adjacent_result = AdjacentItRules().evaluate(vacancy)

    assert backend_result.category is RuleCategory.REJECTED
    assert adjacent_result.category is RuleCategory.REJECTED
    assert any("no-code" in reason for reason in backend_result.reasons)
    assert any("no-code" in reason for reason in adjacent_result.reasons)


def test_sql_or_bitrix_primary_role_is_not_accepted() -> None:
    for title in ("Стажёр SQL-разработчик", "Разработчик 1С-Битрикс"):
        vacancy = VacancyData(
            f"other-stack-{title}",
            title,
            "https://hh.ru/vacancy/other-stack",
            description="Основная разработка на SQL или Битрикс, Python будет плюсом.",
        )

        backend = PythonBackendRules().evaluate(vacancy)
        adjacent = AdjacentItRules().evaluate(vacancy)

        assert not backend.accepted
        assert not adjacent.accepted


def test_mlops_with_python_automation_is_routed_as_stretch() -> None:
    vacancy = VacancyData(
        "mlops",
        "MLOps engineer",
        "https://hh.ru/vacancy/mlops",
        description=("Автоматизировать пайплайны на Python, работать с Docker, Linux и CI/CD."),
    )

    backend = PythonBackendRules().evaluate(vacancy)
    adjacent = AdjacentItRules().evaluate(vacancy)

    assert backend.category is RuleCategory.ROUTED
    assert adjacent.category is RuleCategory.STRETCH


@pytest.mark.parametrize(
    ("title", "description", "key_skills"),
    [
        (
            "Middle Backend-разработчик",
            "Коммерческий опыт NodeJS от 3 лет. Основной фреймворк — Nest.js.",
            ("Node.js", "Nest.js", "PostgreSQL"),
        ),
        (
            "Junior backend разработчик",
            "Разработка на JavaScript, TypeScript и PHP/Symfony.",
            ("JavaScript", "PHP", "MySQL"),
        ),
        (
            "Разработчик (Backend)",
            "Во всех проектах используется собственный язык программирования MPL.",
            ("Git", "SQL", "Linux"),
        ),
    ],
)
def test_python_backend_requires_python_even_with_resume_skill_overlap(
    title: str,
    description: str,
    key_skills: tuple[str, ...],
) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"non-python-{title}",
            title,
            "https://hh.ru/vacancy/non-python-backend",
            description=description,
            key_skills=key_skills,
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL, Docker, Git, SQL, Linux",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("Python не указан" in reason for reason in result.reasons)


def test_python_as_one_of_many_optional_languages_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "optional-python",
            "Программист (Junior - младший разработчик)",
            "https://hh.ru/vacancy/optional-python",
            description=(
                "Знание одного из объектно-ориентированных языков программирования, "
                "к примеру: C++, C#, Delphi, Python, Pascal или 1С. "
                "Доработка программного комплекса и внедрение информационной системы."
            ),
            key_skills=("Delphi", "Pascal", "PHP", "JavaScript"),
        )
    )

    assert result.category is RuleCategory.REJECTED
    assert any("один из необязательных языков" in reason for reason in result.reasons)


def test_mandatory_rust_with_python_as_second_language_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "rust-primary",
            "Middle Backend Разработчик (алготрейдинг)",
            "https://hh.ru/vacancy/rust-primary",
            description=(
                "Технический стек: Rust — обязательно. "
                "Python или JavaScript/TypeScript как второй язык."
            ),
            key_skills=("Python", "Rust", "JavaScript"),
        )
    )

    assert result.category is RuleCategory.REJECTED
    assert any("Rust" in reason for reason in result.reasons)


def test_pawn_as_server_side_language_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "pawn-primary",
            "Разработчик серверной игровой логики",
            "https://hh.ru/vacancy/pawn-primary",
            description=(
                "Проектировать игровые механики на скриптовом языке Pawn, "
                "используемом на серверной стороне. "
                "Подойдёт опыт серверной разработки на Python, JS, Dart или Pawn."
            ),
            key_skills=("Python", "JavaScript", "MySQL"),
        )
    )

    assert result.category is RuleCategory.REJECTED
    assert any("Pawn" in reason for reason in result.reasons)


def test_business_process_robotization_is_rejected_from_python_backend() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "rpa-intern",
            "Начинающий разработчик (стажер)",
            "https://hh.ru/vacancy/rpa-intern",
            description=(
                "Стажировка для разработчиков в сфере автоматизации и роботизации "
                "бизнес-процессов. Решения создаются на платформах PHP и Python."
            ),
            key_skills=("Python", "SQL"),
        )
    )

    assert result.category is RuleCategory.REJECTED
    assert any("роботизация бизнес-процессов" in reason for reason in result.reasons)


def test_python_infrastructure_backend_is_not_rejected_for_devops_in_description() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "python-infrastructure",
            "Веб-разработчик (инфраструктурные сервисы)",
            "https://hh.ru/vacancy/python-infrastructure",
            description=(
                "Ищем Python-разработчика. Развитие backend-сервисов на Django и FastAPI. "
                "Взаимодействие с командами DevOps и сетевыми инженерами."
            ),
            key_skills=("Python", "Django", "FastAPI", "PostgreSQL"),
        )
    )

    assert result.category is RuleCategory.MATCH


@pytest.mark.parametrize(
    "level",
    [
        "Уровень: Middle / Senior",
        "Уровень: Middle-Senior",
        "Грейд: Senior — Middle",
    ],
)
def test_middle_senior_level_in_description_is_stretch(level: str) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"described-level-{level}",
            "Backend-разработчик",
            "https://hh.ru/vacancy/described-level",
            description=f"{level}. Разработка backend на Python и FastAPI.",
            experience="От 3 до 6 лет",
        )
    )

    assert result.category is RuleCategory.STRETCH
    assert any("как риск" in reason for reason in result.reasons)


def test_ordinary_middle_title_is_not_rejected_by_level() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "middle",
            "Middle Python backend developer",
            "https://hh.ru/vacancy/middle",
            description="Python backend на FastAPI и PostgreSQL",
            experience="От 3 до 6 лет",
        )
    )

    assert result.category is RuleCategory.MATCH


def test_automation_is_routed_from_python_backend_to_it() -> None:
    vacancy = VacancyData(
        "low-score",
        "Инженер автоматизации",
        "https://hh.ru/vacancy/low-score",
        description="Писать небольшие инструменты на Python",
    )
    routed = PythonBackendRules().evaluate(vacancy)
    result = AdjacentItRules().evaluate(vacancy)

    assert routed.category is RuleCategory.ROUTED
    assert routed.target_scope is DirectionScope.IT_ADJACENT
    assert result.accepted


def test_more_than_six_years_hh_experience_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "more-than-six",
            "Python backend разработчик",
            "https://hh.ru/vacancy/more-than-six",
            description="Разработка API на Python и FastAPI.",
            experience="Более\N{NARROW NO-BREAK SPACE}6 лет",
        )
    )

    assert result.category is RuleCategory.REJECTED
    assert any("более 6 лет" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "required_qualifications",
    [
        "Опыт коммерческой разработки 4\N{NARROW NO-BREAK SPACE}– 5 лет.",
        "Опыт коммерческой разработки от\N{NO-BREAK SPACE}4 лет.",
        "Требуется не менее 4-х лет разработки серверных приложений.",
        "Backend development experience: 4+ years.",
    ],
)
def test_four_plus_year_requirement_requires_manual_review(
    required_qualifications: str,
) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"four-plus-{required_qualifications}",
            "Python backend разработчик",
            "https://hh.ru/vacancy/four-plus",
            description="Разработка API на Python и FastAPI.",
            required_qualifications=required_qualifications,
            experience="От 3 до 6 лет",
        )
    )

    assert result.category is RuleCategory.STRETCH
    assert any("дополнительной проверки" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "required_qualifications",
    [
        "Опыт коммерческой разработки от 2 лет.",
        "Коммерческий опыт разработки от 2,5 лет на Python/Django.",
        "Опыт разработки на Python от 3 лет.",
        "Опыт промышленной разработки на Python от трех лет.",
        "Опыт разработки backend-сервисов минимум 5 лет.",
        "Python development experience: 3+ years.",
    ],
)
def test_mandatory_two_plus_years_of_development_is_stretch(
    required_qualifications: str,
) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"mandatory-experience-{required_qualifications}",
            "Python backend разработчик",
            "https://hh.ru/vacancy/mandatory-experience",
            description="Разработка API на Python и FastAPI.",
            required_qualifications=required_qualifications,
        )
    )

    assert result.category is RuleCategory.STRETCH
    assert any("само по себе не блокирует" in reason for reason in result.reasons)


def test_mandatory_fullstack_experience_is_stretch_in_it_direction() -> None:
    result = AdjacentItRules().evaluate(
        VacancyData(
            "fullstack-experience",
            "Fullstack-разработчик",
            "https://hh.ru/vacancy/fullstack-experience",
            description="Разработка API на Python и интерфейса на React.",
            required_qualifications=(
                "От 3 лет коммерческого опыта в fullstack- или backend-разработке."
            ),
        )
    )

    assert result.category is RuleCategory.STRETCH
    assert any("само по себе не блокирует" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "required_qualifications",
    [
        "Опыт коммерческой разработки 1–3 года.",
        "Коммерческий опыт backend-разработки без жёсткого требования к стажу.",
        "Опыт коммерческой или серьёзной pet/open-source разработки.",
    ],
)
def test_development_experience_without_two_year_minimum_is_not_rejected(
    required_qualifications: str,
) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"experience-without-minimum-{required_qualifications}",
            "Python backend разработчик",
            "https://hh.ru/vacancy/experience-without-minimum",
            description="Разработка API на Python и FastAPI.",
            required_qualifications=required_qualifications,
            experience="1–3 года",
        )
    )

    assert result.category is RuleCategory.MATCH


def test_task_29_without_explicit_year_minimum_remains_a_match() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "task-29",
            "Python-разработчик middle",
            "https://hh.ru/vacancy/task-29",
            description=(
                "Разработка внутренних серверных приложений на Python. "
                "Опыт разработки серверных приложений на Python. "
                "PostgreSQL, SQLAlchemy и FastAPI."
            ),
            experience="1–3 года",
            key_skills=("Python", "SQL", "PostgreSQL"),
        )
    )

    assert result.category is RuleCategory.MATCH


def test_leading_python_test_automation_is_routed_and_kept_as_stretch() -> None:
    vacancy = VacancyData(
        "python-load-tests",
        "Ведущий инженер по тестированию",
        "https://hh.ru/vacancy/python-load-tests",
        responsibilities=("Разрабатывать сценарии нагрузочного тестирования на Locust и Python."),
        key_skills=("Python", "Locust"),
    )

    backend_result = PythonBackendRules().evaluate(vacancy)
    adjacent_result = AdjacentItRules().evaluate(vacancy)

    assert backend_result.category is RuleCategory.ROUTED
    assert adjacent_result.category is RuleCategory.STRETCH


def test_python_automation_with_llm_is_a_match() -> None:
    vacancy = VacancyData(
        "7",
        "Разработчик / Automation Engineer (интеграции, LLM/RAG)",
        "https://hh.ru/vacancy/7",
        description="Писать backend-сервисы на Python и работать через API моделей",
        experience="Опыт 1\N{EN DASH}3 года",
        key_skills=("Python", "FastAPI", "Docker", "Git"),
    )
    routed = PythonBackendRules().evaluate(vacancy)
    result = AdjacentItRules().evaluate(vacancy)

    assert routed.category is RuleCategory.ROUTED
    assert result.category is RuleCategory.MATCH


def test_ai_agent_engineer_is_a_stretch_match() -> None:
    vacancy = VacancyData(
        "8",
        "AI Agent Engineer (NLP/LLM)",
        "https://hh.ru/vacancy/8",
        description="Логика агентов на Python и интеграция через backend API",
        experience="Опыт 1\N{EN DASH}3 года",
        key_skills=("Python", "LangGraph", "LLM", "NLP"),
    )
    routed = PythonBackendRules().evaluate(vacancy)
    result = AdjacentItRules().evaluate(vacancy)

    assert routed.category is RuleCategory.ROUTED
    assert result.accepted
    assert result.category is RuleCategory.STRETCH
    assert any("дополнительная подготовка" in reason for reason in result.reasons)


def test_rule_components_use_known_settings_without_zero_for_unknown_values() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "settings",
            "Python backend разработчик",
            "https://hh.ru/vacancy/settings",
            description="Разработка API на FastAPI и PostgreSQL.",
            work_format="Удалённо",
            region="Москва",
            salary_from=Decimal("140000"),
            salary_currency="RUR",
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        ),
        RuleContext(
            skills=("Python, FastAPI, PostgreSQL, Docker",),
            work_formats=(WorkFormat.REMOTE,),
            regions=(SearchRegion("1", "Москва"),),
            desired_salary=120000,
        ),
    )

    assert result.accepted
    assert {component.name for component in result.components} >= {
        "role",
        "skills",
        "format",
        "salary",
        "region",
        "description",
    }
    assert all(component.score > 0 for component in result.components)


def test_salary_maximum_below_explicit_minimum_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "low-salary",
            "Python backend разработчик",
            "https://hh.ru/vacancy/low-salary",
            description="Разработка API на FastAPI и PostgreSQL.",
            salary_from=Decimal("50000"),
            salary_to=Decimal("100000"),
            salary_currency="RUR",
        ),
        RuleContext(minimum_salary=120000, desired_salary=150000),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("зарплаты" in reason for reason in result.reasons)


def test_salary_maximum_below_desired_salary_is_only_a_soft_score() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "below-desired-salary",
            "Python backend разработчик",
            "https://hh.ru/vacancy/below-desired-salary",
            description="Разработка API на FastAPI и PostgreSQL.",
            salary_from=Decimal("50000"),
            salary_to=Decimal("100000"),
            salary_currency="RUR",
        ),
        RuleContext(desired_salary=120000),
    )

    salary_component = next(
        component for component in result.components if component.name == "salary"
    )
    assert result.category is RuleCategory.MATCH
    assert salary_component.score < 100
    assert not any("ниже установленного порога" in reason for reason in result.reasons)


def test_mandatory_frontend_stack_missing_from_profile_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "mandatory-frontend",
            "Python-разработчик",
            "https://hh.ru/vacancy/mandatory-frontend",
            description="""Разработка API на Python и FastAPI.

Требования:
Уверенное использование JavaScript.
Знание jQuery и Bootstrap.
Уверенные навыки HTML5 и CSS.

Условия:
Удалённая работа.""",
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("клиентский стек" in reason for reason in result.reasons)


def test_mandatory_message_broker_missing_from_profile_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "mandatory-broker",
            "Python backend разработчик",
            "https://hh.ru/vacancy/mandatory-broker",
            description="""Разработка API на Python и FastAPI.

Требования:
Опыт работы с PostgreSQL и брокерами сообщений RabbitMQ или Kafka.

Будет плюсом:
Kubernetes.""",
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL, Docker",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("брокер сообщений" in reason for reason in result.reasons)


def test_mandatory_django_missing_from_profile_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "mandatory-django",
            "Python-разработчик",
            "https://hh.ru/vacancy/mandatory-django",
            description="""Разработка серверной части на Python.

Для нас важно:
Опыт работы с Python и Django.
Знание PostgreSQL и Git.

Мы предлагаем:
Удалённую работу.""",
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL, Git",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("Django" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("vacancy", "reason"),
    [
        (
            VacancyData(
                "closed",
                "Python разработчик",
                "https://hh.ru/vacancy/closed",
                description="Python",
                availability=VacancyAvailability.ARCHIVED,
            ),
            "недоступна",
        ),
        (
            VacancyData(
                "scam",
                "Python разработчик",
                "https://hh.ru/vacancy/scam",
                description="Для начала нужно оплатить обучение и прислать код из СМС.",
            ),
            "подозрительное требование",
        ),
    ],
)
def test_closed_and_suspicious_vacancies_are_rejected(
    vacancy: VacancyData,
    reason: str,
) -> None:
    result = PythonBackendRules().evaluate(vacancy)

    assert result.category is RuleCategory.REJECTED
    assert any(reason in item for item in result.reasons)


def test_mandatory_work_format_conflict_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "office",
            "Python разработчик",
            "https://hh.ru/vacancy/office",
            description="Python backend",
            work_format="Только офис",
        ),
        RuleContext(work_formats=(WorkFormat.REMOTE,)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("формат работы" in reason for reason in result.reasons)


def test_mandatory_relocation_is_rejected_when_candidate_is_not_ready() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "relocation",
            "Python разработчик",
            "https://hh.ru/vacancy/relocation",
            description=(
                "Python backend. Обязательным условием является релокация "
                "в Республику Татарстан, город Елабуга."
            ),
        ),
        RuleContext(
            regions=(SearchRegion("1", "Москва"), SearchRegion("2", "Санкт-Петербург")),
            relocation_allowed=False,
        ),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("переезд" in reason for reason in result.reasons)


def test_office_outside_selected_regions_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "office-other-city",
            "Python backend разработчик",
            "https://hh.ru/vacancy/office-other-city",
            description="Разработка API на Python и FastAPI.",
            work_format=(
                "Формат\N{NO-BREAK SPACE}работы: "
                "на\N{NARROW NO-BREAK SPACE}месте работодателя "
                "или\N{NO-BREAK SPACE}гибрид"
            ),
            region="Москва",
        ),
        RuleContext(
            regions=(SearchRegion("2", "Санкт-Петербург"),),
            candidate_locations=("Учалы",),
            relocation_allowed=False,
        ),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("вне выбранных регионов" in reason for reason in result.reasons)


def test_office_in_selected_region_does_not_depend_on_resume_city() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "office-selected-city",
            "Python backend разработчик",
            "https://hh.ru/vacancy/office-selected-city",
            description="Разработка API на Python и FastAPI.",
            work_format="Формат работы: на месте работодателя",
            region="Москва",
        ),
        RuleContext(
            regions=(SearchRegion("1", "Москва"),),
            candidate_locations=("Учалы",),
            relocation_allowed=False,
        ),
    )

    assert result.category is RuleCategory.MATCH


@pytest.mark.parametrize(
    "office_requirement",
    [
        "Формат работы:\N{NO-BREAK SPACE}офис",
        "РАБОТА В ОФИСЕ",
    ],
)
def test_text_office_requirement_overrides_incorrect_remote_field(
    office_requirement: str,
) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"contradictory-office-{office_requirement}",
            "Backend-разработчик",
            "https://hh.ru/vacancy/contradictory-office",
            description=(
                "Уровень: Middle / Senior. "
                f"{office_requirement}. Разработка backend на Python и FastAPI."
            ),
            work_format="Удалённо",
            region="Москва",
        ),
        RuleContext(
            regions=(SearchRegion("2", "Санкт-Петербург"),),
            candidate_locations=("Учалы",),
            relocation_allowed=False,
        ),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("уровень Middle/Senior" in reason for reason in result.reasons)
    assert any("вне выбранных регионов" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("work_format", "region"),
    [
        ("Формат работы: удалённо", "Москва"),
        ("Формат работы: удалённо или гибрид", "Москва"),
        ("Формат работы: на месте работодателя", "Санкт-Петербург, р-н Центральный"),
    ],
)
def test_remote_or_local_vacancy_does_not_require_relocation(
    work_format: str,
    region: str,
) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"allowed-{region}-{work_format}",
            "Python backend разработчик",
            "https://hh.ru/vacancy/allowed-location",
            description="Разработка API на Python и FastAPI.",
            work_format=work_format,
            region=region,
        ),
        RuleContext(
            regions=(SearchRegion("2", "Санкт-Петербург"),),
            candidate_locations=("Учалы",),
            relocation_allowed=False,
        ),
    )

    assert result.category is RuleCategory.MATCH


def test_mandatory_relocation_is_allowed_when_candidate_confirmed_it() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "relocation-confirmed",
            "Python разработчик",
            "https://hh.ru/vacancy/relocation-confirmed",
            description="Python backend. Обязательная релокация в город Казань.",
        ),
        RuleContext(relocation_allowed=True),
    )

    assert result.category is RuleCategory.MATCH


def test_mandatory_far_east_relocation_is_rejected_even_when_relocation_allowed() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "relocation-far-east-confirmed",
            "Python разработчик",
            "https://hh.ru/vacancy/relocation-far-east-confirmed",
            description="Python backend. Обязательная релокация во Владивосток.",
        ),
        RuleContext(relocation_allowed=True),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("переезд" in reason for reason in result.reasons)


@pytest.mark.parametrize("city", ["Владивосток", "Хабаровск"])
def test_mandatory_relocation_to_far_east_is_rejected(city: str) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"relocation-{city}",
            "Python backend разработчик",
            f"https://hh.ru/vacancy/relocation-{city}",
            description=f"Python backend. Обязательная релокация в город {city}.",
        )
    )

    assert result.category is RuleCategory.REJECTED
    assert any("переезд" in reason for reason in result.reasons)


def test_fullstack_is_owned_by_it_even_with_python_backend() -> None:
    vacancy = VacancyData(
        "fullstack",
        "Middle Full-stack разработчик",
        "https://hh.ru/vacancy/fullstack",
        description="Python, FastAPI, TypeScript и React",
    )

    routed = PythonBackendRules().evaluate(vacancy)
    accepted = AdjacentItRules().evaluate(vacancy)

    assert routed.category is RuleCategory.ROUTED
    assert routed.target_scope is DirectionScope.IT_ADJACENT
    assert accepted.category is RuleCategory.MATCH


def test_qtrader_with_serious_pet_or_open_source_work_remains_eligible() -> None:
    vacancy = VacancyData(
        "qtrader",
        "Fullstack / Backend-разработчик (AI-assisted, vibe coding)",
        "https://hh.ru/vacancy/qtrader",
        description=(
            "Разрабатывать backend/fullstack-функции продуктов на Python, "
            "интегрироваться с API, писать тесты и поддерживать CI/CD. "
            "Требуется опыт коммерческой или серьёзной pet/open-source разработки."
        ),
        experience="3–6 лет",
        key_skills=("Python", "PostgreSQL", "Docker", "JavaScript"),
    )

    backend_result = PythonBackendRules().evaluate(vacancy)
    adjacent_result = AdjacentItRules().evaluate(vacancy)

    assert backend_result.category is RuleCategory.ROUTED
    assert adjacent_result.category is RuleCategory.MATCH


def test_machine_learning_role_is_a_stretch_in_it() -> None:
    vacancy = VacancyData(
        "ml",
        "Middle ML-инженер",
        "https://hh.ru/vacancy/ml",
        description="Разработка служб на Python и обучение моделей",
    )

    routed = PythonBackendRules().evaluate(vacancy)
    accepted = AdjacentItRules().evaluate(vacancy)

    assert routed.category is RuleCategory.ROUTED
    assert accepted.category is RuleCategory.STRETCH


def test_unrelated_mandatory_dotnet_stack_is_rejected_in_it() -> None:
    result = AdjacentItRules().evaluate(
        VacancyData(
            "dotnet",
            "Backend C# Developer",
            "https://hh.ru/vacancy/dotnet",
            required_qualifications="Обязательный стек: C# и .NET",
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("другой основной стек" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "title",
    [
        "Senior Java Developer",
        "Backend C# Developer",
        "Golang разработчик",
        "Node.js Backend Developer / NestJS",
        "Embedded C++ разработчик",
    ],
)
def test_other_primary_stack_is_rejected_even_with_generic_skill_overlap(title: str) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"other-{title}",
            title,
            "https://hh.ru/vacancy/other-stack",
            description="Backend, REST API, PostgreSQL, Docker и Linux.",
            required_qualifications="Обязателен основной стек из названия.",
            key_skills=("PostgreSQL", "Docker", "Linux"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL, Docker, Linux",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("другой основной стек" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("title", "description", "stack"),
    [
        (
            "Стажер-backend developer",
            (
                "Основной стек — NodeJS, TypeScript, NestJS и Rust. "
                "Python указан лишь как пример базового языка."
            ),
            "Node.js/TypeScript",
        ),
        (
            "Senior Backend (Rust/C++/Python)",
            ("Основной язык — Rust; Python используется для legacy и вспомогательных сервисов."),
            "Rust",
        ),
    ],
)
def test_python_as_secondary_language_does_not_hide_primary_stack(
    title: str,
    description: str,
    stack: str,
) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"secondary-{stack}",
            title,
            "https://hh.ru/vacancy/secondary",
            description=description,
            key_skills=("Python", "Docker"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL, Docker",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any(stack in reason for reason in result.reasons)


def test_not_only_python_phrase_is_not_treated_as_secondary_language() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "python-not-only",
            "Python-разработчик",
            "https://hh.ru/vacancy/python-not-only",
            description=(
                "Ищем Python-разработчика, который умеет не только писать код, "
                "но и проектировать backend-сервисы на FastAPI."
            ),
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL",)),
    )

    assert result.category is RuleCategory.MATCH


def test_mandatory_primary_1c_stack_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "primary-1c",
            "Архитектор ERP системы",
            "https://hh.ru/vacancy/primary-1c",
            description=(
                "Глубокая разработка в 1С (основной стек). "
                "Python применяется только для внешних расчётных модулей."
            ),
            key_skills=("1С: Предприятие 8", "Python"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("1С" in reason for reason in result.reasons)


def test_mixed_go_and_python_backend_title_is_not_rejected_as_other_stack() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "mixed-stack",
            "Backend Go / Python разработчик",
            "https://hh.ru/vacancy/mixed-stack",
            description="Разработка сервисов на Python и Go.",
            key_skills=("Python", "Go", "PostgreSQL"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL",)),
    )

    assert result.category is RuleCategory.MATCH
