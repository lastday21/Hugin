from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal

import pytest

from hugin.domain.directions import DirectionScope, SearchRegion, WorkFormat
from hugin.domain.vacancies import VacancyAvailability, VacancyData
from hugin.services import vacancy_analysis as vacancy_analysis_module
from hugin.services.vacancy_analysis import (
    RULES_VERSION,
    AdjacentItRules,
    PythonBackendRules,
    RuleCategory,
    RuleContext,
)


def test_rules_version_is_python_it_v32() -> None:
    assert RULES_VERSION == "python_it_v32"


@pytest.mark.parametrize(
    ("published_days_ago", "expected_category"),
    [
        (30, RuleCategory.MATCH),
        (31, RuleCategory.REJECTED),
    ],
)
def test_rules_reject_only_vacancies_older_than_thirty_days(
    monkeypatch: pytest.MonkeyPatch,
    published_days_ago: int,
    expected_category: RuleCategory,
) -> None:
    evaluated_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> FixedDateTime:
            fixed = cls.fromtimestamp(evaluated_at.timestamp(), UTC)
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(vacancy_analysis_module, "datetime", FixedDateTime)
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"age-{published_days_ago}",
            "Python backend разработчик",
            f"https://hh.ru/vacancy/age-{published_days_ago}",
            published_at=evaluated_at - timedelta(days=published_days_ago),
            description="Разработка backend-службы на Python и FastAPI",
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        )
    )

    assert result.category is expected_category
    assert ("вакансия опубликована более 30 дней назад" in result.reasons) is (
        published_days_ago > 30
    )
    if published_days_ago == 30:
        freshness = next(
            component for component in result.components if component.name == "freshness"
        )
        assert freshness.score == 55


def test_rules_do_not_reject_vacancy_with_unknown_publication_date() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "age-unknown",
            "Python backend разработчик",
            "https://hh.ru/vacancy/age-unknown",
            published_at=None,
            description="Разработка backend-службы на Python и FastAPI",
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        )
    )

    assert result.category is RuleCategory.MATCH
    assert not any("30 дней" in reason for reason in result.reasons)


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


def test_three_to_six_years_requires_manual_review_for_non_senior_role() -> None:
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
    assert result.category is RuleCategory.STRETCH
    assert any("ручной проверки" in reason for reason in result.reasons)


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
            experience="От 1 года до 3 лет",
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
        "Junior Data-инженер",
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


@pytest.mark.parametrize(
    ("hh_id", "title", "description", "reason"),
    [
        (
            "135439918",
            "Младший разработчик системного ПО (Linux kernel/ C)",
            (
                "Разработка модулей ядра Linux и поиск ошибок в низкоуровневом C-коде. "
                "Требуются системное программирование, POSIX API и Linux kernel."
            ),
            "низкоуровневое системное программирование",
        ),
        (
            "135792660",
            "Веб-мастер",
            (
                "Привлекать B2B-трафик на сервис телефонии через контекстную рекламу, "
                "таргетинг и SEO. Оплата по модели RevShare."
            ),
            "интернет-маркетинг",
        ),
        (
            "134786547",
            "Customer Journey Expert в команду «Инженерно-технические решения»",
            (
                "Проводить A/B-тесты, формализовать задачи для команды разработки, "
                "использовать Python и SQL для анализа данных."
            ),
            "клиентским опытом",
        ),
        (
            "135220221",
            "Инженер по сопровождению",
            (
                "Управлять обновлениями и мониторингом прикладных сервисов, "
                "решать инциденты и иногда применять Python-скрипты."
            ),
            "сопровождение или эксплуатация",
        ),
        (
            "135457711",
            "Сетевой инженер",
            (
                "Настраивать Cisco, LAN/WAN, VPN и межсетевые экраны. "
                "Автоматизировать отдельные рутинные задачи скриптами на Python."
            ),
            "сетевое администрирование",
        ),
    ],
)
def test_live_non_target_roles_are_rejected_in_both_directions(
    hh_id: str,
    title: str,
    description: str,
    reason: str,
) -> None:
    vacancy = VacancyData(
        hh_id,
        title,
        f"https://hh.ru/vacancy/{hh_id}",
        description=description,
        key_skills=("Python", "SQL", "Linux"),
    )

    for rules in (PythonBackendRules(), AdjacentItRules()):
        result = rules.evaluate(vacancy)
        assert result.category is RuleCategory.REJECTED
        assert any(reason in item for item in result.reasons)


def test_live_sql_only_role_is_routed_to_it_and_rejected_there() -> None:
    vacancy = VacancyData(
        "135422937",
        "Младший Разработчик SQL (PIX BI)",
        "https://hh.ru/vacancy/135422937",
        description=(
            "Разработка PL/SQL скриптов, скриптов на Python и дашбордов на PIX BI. "
            "Обязателен опыт с BI-системами и данными."
        ),
        experience="1–3 года",
        key_skills=("Python", "Apache Airflow", "Apache Spark"),
    )

    backend = PythonBackendRules().evaluate(vacancy)
    adjacent = AdjacentItRules().evaluate(vacancy)

    assert backend.category is RuleCategory.ROUTED
    assert backend.target_scope is DirectionScope.IT_ADJACENT
    assert adjacent.category is RuleCategory.REJECTED
    assert any("разработка баз данных" in reason for reason in adjacent.reasons)


def test_live_data_engineer_requires_confirmed_big_data_stack() -> None:
    vacancy = VacancyData(
        "135686059",
        "Data Engineer (Риски розничного бизнеса)",
        "https://hh.ru/vacancy/135686059",
        description=(
            "Разработка и сопровождение витрин данных, создание ETL-процессов "
            "и проверка качества данных."
        ),
        required_qualifications=(
            "Уверенное владение Python и Spark SQL. Опыт работы с Hadoop, "
            "Greenplum и инкрементальной загрузкой данных CDC."
        ),
        experience="1–3 года",
        key_skills=("Python", "SQL", "ETL", "Git"),
    )

    result = AdjacentItRules().evaluate(
        vacancy,
        RuleContext(skills=("Python, SQL, ETL, Git",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any(
        "несколько обязательных профильных технологий не подтверждены" in reason
        for reason in result.reasons
    )
    assert any("Apache Spark" in reason for reason in result.reasons)
    assert any("Hadoop" in reason for reason in result.reasons)
    assert any("Greenplum" in reason for reason in result.reasons)


def test_live_python_role_with_large_unconfirmed_stack_is_rejected() -> None:
    vacancy = VacancyData(
        "135596795",
        "Python - разработчик",
        "https://hh.ru/vacancy/135596795",
        description=(
            "Работа с библиотеками Python и участие в тестировании. "
            "Ждем от тебя: опыт со всеми библиотеками: Pandas, Requests, "
            "Scrapy, NumPy, SciPy и PySpark. Опыт с Keras и TensorFlow."
        ),
        experience="1–3 года",
        key_skills=("Python", "Flask", "PostgreSQL", "REST"),
    )

    result = PythonBackendRules().evaluate(
        vacancy,
        RuleContext(skills=("Python, Pandas, NumPy, PostgreSQL, REST",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("Apache Spark" in reason for reason in result.reasons)
    assert any("Scrapy" in reason for reason in result.reasons)
    assert any("TensorFlow/Keras" in reason for reason in result.reasons)


def test_inline_mandatory_heading_detects_siem_and_message_broker() -> None:
    vacancy = VacancyData(
        "135202636",
        "Python-разработчик",
        "https://hh.ru/vacancy/135202636",
        description=(
            "Разработка SIEM-платформы и обработчиков событий ИБ. "
            "Требования: опыт с Flask и FastAPI. Знание Redis, Kafka, Docker "
            "и опыт работы с SIEM-платформами. Условия: ДМС и обучение."
        ),
        key_skills=("Python", "FastAPI", "Redis"),
    )

    result = PythonBackendRules().evaluate(
        vacancy,
        RuleContext(skills=("Python, FastAPI, Redis, Docker",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("брокер сообщений" in reason for reason in result.reasons)
    assert any("SIEM" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("hh_id", "heading", "requirements", "expected_reasons"),
    [
        (
            "135470448",
            "Мы ожидаем от тебя",
            "Опыт интеграции с RabbitMQ.",
            ("брокер сообщений",),
        ),
        (
            "135027656",
            "Что ожидаем от кандидата",
            "Опыт работы с ELK / Elasticsearch.",
            ("Elasticsearch/ELK",),
        ),
        (
            "132284785",
            "Что для этого необходимо",
            "Опыт разработки на фреймворке Django от 1 года.",
            ("Django",),
        ),
        (
            "135430663",
            "Для нас важно",
            "Уверенное использование pandas и понимание принципов DWH.",
            ("Pandas", "DWH"),
        ),
        (
            "135662860",
            "Опыт и навыки",
            "Разработка дагов в Airflow и обработка данных на PySpark.",
            ("Airflow", "Apache Spark"),
        ),
        (
            "135723184",
            "Ожидания",
            "Практический опыт работы с Kubernetes.",
            ("Kubernetes",),
        ),
        (
            "135266443",
            "Наши пожелания к кандидатам",
            "Опыт с pandas, Greenplum, Airflow и PySpark.",
            ("Pandas", "Greenplum", "Airflow", "Apache Spark"),
        ),
    ],
)
def test_live_inline_requirement_headings_detect_specialist_gaps(
    hh_id: str,
    heading: str,
    requirements: str,
    expected_reasons: tuple[str, ...],
) -> None:
    vacancy = VacancyData(
        hh_id,
        "Python backend-разработчик",
        f"https://hh.ru/vacancy/{hh_id}",
        description=f"Разработка на Python. {heading}: {requirements} Условия: ДМС.",
        key_skills=("Python",),
    )

    result = PythonBackendRules().evaluate(vacancy, RuleContext(skills=("Python",)))

    expected_category = RuleCategory.REJECTED if len(expected_reasons) > 1 else RuleCategory.STRETCH
    assert result.category is expected_category
    for reason in expected_reasons:
        assert any(reason in item for item in result.reasons)


def test_optional_skill_line_does_not_hide_later_mandatory_requirements() -> None:
    vacancy = VacancyData(
        "135027656",
        "Разработчик python (junior +)",
        "https://hh.ru/vacancy/135027656",
        description=(
            "Разработка backend-сервисов на Python. Что ожидаем от кандидата:\n"
            "Знание Python и ООП.\n"
            "Базовые навыки Flask, Django - будет плюсом.\n"
            "Опыт работы с ELK / Elasticsearch.\n"
            "Знание основ тестирования.\n"
            "Что мы предлагаем: ДМС."
        ),
        key_skills=("Python", "REST API"),
    )

    result = PythonBackendRules().evaluate(
        vacancy,
        RuleContext(skills=("Python, FastAPI, REST, pytest",)),
    )

    assert result.category is RuleCategory.STRETCH
    assert any("Elasticsearch/ELK" in reason for reason in result.reasons)
    assert not any("Django" in reason for reason in result.reasons)


def test_welcome_section_stops_mandatory_requirements() -> None:
    vacancy = VacancyData(
        "135723184",
        "Middle Backend разработчик",
        "https://hh.ru/vacancy/135723184",
        description=(
            "Разработка сервисов на Python. Ожидания:\n"
            "Опыт промышленной разработки на Python от года.\n"
            "Практический опыт работы с Kubernetes.\n"
            "Приветствуется:\n"
            "Опыт работы с LangGraph и Kafka."
        ),
        key_skills=("Python", "FastAPI", "PostgreSQL"),
    )

    result = PythonBackendRules().evaluate(
        vacancy,
        RuleContext(skills=("Python, FastAPI, PostgreSQL",)),
    )

    assert result.category is RuleCategory.STRETCH
    assert any("Kubernetes" in reason for reason in result.reasons)
    assert not any("LangGraph" in reason for reason in result.reasons)
    assert not any("брокер сообщений" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("description", "has_test_assignment", "expected_reason"),
    [
        (
            (
                "Разработка backend на Python и FastAPI. "
                "Просим в сопроводительном письме коротко ответить на три вопроса."
            ),
            False,
            "ответы в сопроводительном письме",
        ),
        (
            (
                "Разработка backend на Python и FastAPI. "
                "Для отклика заполните форму https://forms.gle/example."
            ),
            False,
            "внешнюю форму",
        ),
        (
            "Разработка backend на Python и FastAPI.",
            True,
            "испытательное задание",
        ),
    ],
)
def test_manual_application_actions_are_not_exact_matches(
    description: str,
    has_test_assignment: bool,
    expected_reason: str,
) -> None:
    vacancy = VacancyData(
        "manual-application-action",
        "Python backend разработчик",
        "https://hh.ru/vacancy/manual-application-action",
        description=description,
        key_skills=("Python", "FastAPI", "PostgreSQL"),
        has_test_assignment=has_test_assignment,
    )

    result = PythonBackendRules().evaluate(
        vacancy,
        RuleContext(skills=("Python, FastAPI, PostgreSQL",)),
    )

    assert result.category is RuleCategory.STRETCH
    assert any(expected_reason in reason for reason in result.reasons)


def test_multilanguage_backend_requirements_are_not_treated_as_python_only() -> None:
    vacancy = VacancyData(
        "135721455",
        "Middle Backend Developer / Backend-разработчик (Go / Python)",
        "https://hh.ru/vacancy/135721455",
        description=(
            "Разрабатывать backend и интеграции. Что мы ожидаем: "
            "коммерческий опыт с Go, Python и Node.js. "
            "Понимание микросервисной архитектуры. "
            "Будет плюсом: опыт с CMS."
        ),
        key_skills=("Python", "REST API"),
    )

    result = PythonBackendRules().evaluate(vacancy, RuleContext(skills=("Python",)))

    assert result.category is RuleCategory.REJECTED
    assert any("Go" in reason for reason in result.reasons)
    assert any("Node.js" in reason for reason in result.reasons)
    assert any("микросервисная архитектура" in reason for reason in result.reasons)


def test_colleague_expectation_heading_detects_required_message_broker() -> None:
    vacancy = VacancyData(
        "135127347",
        "Программист-разработчик (Python)",
        "https://hh.ru/vacancy/135127347",
        description=(
            "Разработка backend-сервисов. "
            "Будем рады видеть в новом коллеге следующее: "
            "знакомство с брокерами сообщений RabbitMQ или Kafka. "
            "Будет плюсом: опыт с Celery."
        ),
        key_skills=("Python", "FastAPI"),
    )

    result = PythonBackendRules().evaluate(
        vacancy,
        RuleContext(skills=("Python, FastAPI, Celery",)),
    )

    assert result.category is RuleCategory.STRETCH
    assert any("брокер сообщений" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "title",
    ("Инженер-технолог", "Инженер -технолог", "Инженер‑технолог"),
)
def test_engineer_technologist_title_spacing_is_rejected(title: str) -> None:
    vacancy = VacancyData(
        "technologist",
        title,
        "https://hh.ru/vacancy/technologist",
        description="Автоматизация и роботизация производства.",
        key_skills=("Python", "Автоматизация"),
    )

    result = AdjacentItRules().evaluate(vacancy, RuleContext(skills=("Python",)))

    assert result.category is RuleCategory.REJECTED
    assert any("производственная технология" in reason for reason in result.reasons)


def test_backend_and_frontend_title_is_routed_to_it() -> None:
    vacancy = VacancyData(
        "full-product",
        "Web-программист (бэк и фронт)",
        "https://hh.ru/vacancy/full-product",
        description="Разработка CRM на Python, React и Vue.",
        required_qualifications="Технические требования: Python, React, Vue, Angular.",
        key_skills=("Python", "REST API"),
    )

    backend = PythonBackendRules().evaluate(vacancy, RuleContext(skills=("Python",)))
    adjacent = AdjacentItRules().evaluate(vacancy, RuleContext(skills=("Python",)))

    assert backend.category is RuleCategory.ROUTED
    assert backend.target_scope is DirectionScope.IT_ADJACENT
    assert adjacent.category is RuleCategory.STRETCH
    assert any("клиентский стек" in reason for reason in adjacent.reasons)


def test_optional_python_example_does_not_count_as_python_backend() -> None:
    vacancy = VacancyData(
        "researcher",
        "Программист-исследователь",
        "https://hh.ru/vacancy/researcher",
        description=(
            "Исследование технологий и подготовка материалов. "
            "Требования: базовое знание какого-либо языка программирования, "
            "например Python (для макетирования и экспериментов)."
        ),
        key_skills=("Python",),
    )

    result = PythonBackendRules().evaluate(vacancy, RuleContext(skills=("Python",)))

    assert result.category is RuleCategory.REJECTED
    assert any("необязательных языков" in reason for reason in result.reasons)


def test_live_industrial_automation_rejects_python_false_positive() -> None:
    vacancy = VacancyData(
        "135529627",
        "Инженер-программист АСУ ТП",
        "https://hh.ru/vacancy/135529627",
        description=(
            "Разработка программ ПЛК, SCADA и HMI. "
            "Просьба не откликаться разработчикам Python, FastAPI и Django."
        ),
        required_qualifications="ПЛК, SCADA, МЭК 61131-3 и Modbus.",
        key_skills=("Python", "Интеграция", "Автоматизация"),
    )

    for rules in (PythonBackendRules(), AdjacentItRules()):
        result = rules.evaluate(vacancy, RuleContext(skills=("Python, FastAPI",)))
        assert result.category is RuleCategory.REJECTED
        assert any("промышленная автоматизация" in reason for reason in result.reasons)
        assert any("прямо исключил кандидатов" in reason for reason in result.reasons)


def test_live_computer_vision_role_requires_confirmed_specialist_stack() -> None:
    vacancy = VacancyData(
        "135500790",
        "Разработчик компьютерного зрения / Computer vision research engineer",
        "https://hh.ru/vacancy/135500790",
        description=(
            "Разработка и внедрение моделей компьютерного зрения. "
            "Мы ждём от Вас: опыт с PyTorch, OpenCV и глубоким обучением. "
            "Будет преимуществом: опыт развёртывания моделей."
        ),
        key_skills=("Python", "PyTorch", "OpenCV"),
    )

    backend = PythonBackendRules().evaluate(vacancy, RuleContext(skills=("Python",)))
    adjacent = AdjacentItRules().evaluate(vacancy, RuleContext(skills=("Python",)))

    assert backend.category is RuleCategory.ROUTED
    assert backend.target_scope is DirectionScope.IT_ADJACENT
    assert adjacent.category is RuleCategory.REJECTED
    assert any("PyTorch" in reason for reason in adjacent.reasons)
    assert any("компьютерное зрение" in reason for reason in adjacent.reasons)


def test_live_support_duties_do_not_count_as_python_development() -> None:
    vacancy = VacancyData(
        "134945058",
        "Программист Python",
        "https://hh.ru/vacancy/134945058",
        description=(
            "Техническая поддержка и консультации клиентов. "
            "Настройка оборудования и программного обеспечения."
        ),
        required_qualifications="Знание Python, PHP, API и SQL.",
        key_skills=("Python", "API", "SQL"),
    )

    result = AdjacentItRules().evaluate(
        vacancy,
        RuleContext(skills=("Python, API, SQL",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("поддержкой и настройкой" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("hh_id", "title", "description"),
    [
        (
            "134754431",
            "ML-разработчик",
            (
                "Разработка модулей рекомендательной системы ценообразования, "
                "построение ML-моделей и написание production-ready кода на Python."
            ),
        ),
        (
            "135273328",
            "Junior/Junior+ DevOps Engineer",
            (
                "Поддержка Kubernetes, CI/CD и мониторинга. "
                "Автоматизация инфраструктуры на Bash или Python."
            ),
        ),
    ],
)
def test_live_adjacent_roles_are_routed_and_require_manual_review(
    hh_id: str,
    title: str,
    description: str,
) -> None:
    vacancy = VacancyData(
        hh_id,
        title,
        f"https://hh.ru/vacancy/{hh_id}",
        description=description,
        experience="1–3 года",
        key_skills=("Python", "Docker", "Linux"),
    )

    backend = PythonBackendRules().evaluate(vacancy)
    adjacent = AdjacentItRules().evaluate(vacancy)

    assert backend.category is RuleCategory.ROUTED
    assert backend.target_scope is DirectionScope.IT_ADJACENT
    assert adjacent.category is RuleCategory.STRETCH


def test_live_lowcode_role_with_real_python_coding_requires_manual_review() -> None:
    vacancy = VacancyData(
        "135705178",
        "LowCode разработчик",
        "https://hh.ru/vacancy/135705178",
        description=(
            "Создавать схемы процессов в BPMN. Писать код для AI-агентов. "
            "Проводить функциональное и интеграционное тестирование."
        ),
        required_qualifications=(
            "Опыт разработки на Python; знание JSON и REST API; опыт работы с Git."
        ),
        experience="1–3 года",
        key_skills=("Python", "REST API", "Git"),
    )

    for rules in (PythonBackendRules(), AdjacentItRules()):
        result = rules.evaluate(vacancy)
        assert result.category is RuleCategory.STRETCH
        assert any("существенное программирование" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "title",
    [
        "AI Developer",
        "ИИ-разработчик",
        "Инженер ИИ",
        "Generative AI Engineer",
        "AI/ML Engineer",
        "Инженер по искусственному интеллекту",
        "DWH Developer",
        "Data Warehouse Developer",
        "Разработчик хранилища данных",
    ],
)
def test_ai_and_dwh_roles_are_routed_to_it_as_stretch(title: str) -> None:
    vacancy = VacancyData(
        f"adjacent-{title}",
        title,
        "https://hh.ru/vacancy/adjacent-role",
        description="Разработка технических решений и программного кода на Python.",
        key_skills=("Python", "SQL", "Git"),
    )

    backend = PythonBackendRules().evaluate(vacancy)
    adjacent = AdjacentItRules().evaluate(vacancy)

    assert backend.category is RuleCategory.ROUTED
    assert backend.target_scope is DirectionScope.IT_ADJACENT
    assert adjacent.category is RuleCategory.STRETCH


def test_live_unpaid_equity_only_role_is_rejected() -> None:
    vacancy = VacancyData(
        "135160771",
        "Разработчик-партнер в международный AI/e-commerce стартап",
        "https://hh.ru/vacancy/135160771",
        description=(
            "Backend, интеграции и автоматизация на Python, FastAPI и PostgreSQL. "
            "На текущем этапе без зарплаты. "
            "Формат — партнерское участие за долю в проекте."
        ),
        key_skills=("Python", "FastAPI", "PostgreSQL"),
    )

    for rules in (PythonBackendRules(), AdjacentItRules()):
        result = rules.evaluate(vacancy)
        assert result.category is RuleCategory.REJECTED
        assert any("не предусматривает денежную оплату" in reason for reason in result.reasons)


def test_salary_with_additional_option_is_not_treated_as_unpaid() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "salary-and-option",
            "Python backend разработчик",
            "https://hh.ru/vacancy/salary-and-option",
            description=(
                "Разработка API на Python и FastAPI. "
                "Официальный оклад 180 000 рублей и дополнительный опцион "
                "после испытательного срока."
            ),
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        )
    )

    assert result.category is RuleCategory.MATCH
    assert not any("денежную оплату" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("hh_id", "title", "description"),
    [
        (
            "135597074",
            "Инженер по ИИ",
            (
                "Проектирование и разработка AI-пайплайнов, работа с данными, "
                "MLOps и интеграция языковых моделей в продукты на Python."
            ),
        ),
        (
            "135786924",
            "Стажер-разработчик AI-агентов в юридическое управление",
            (
                "Разрабатывать и поддерживать AI-агентов для автоматизации "
                "бизнес-процессов банка на Python."
            ),
        ),
    ],
)
def test_live_russian_ai_roles_are_routed_to_it_as_stretch(
    hh_id: str,
    title: str,
    description: str,
) -> None:
    vacancy = VacancyData(
        hh_id,
        title,
        f"https://hh.ru/vacancy/{hh_id}",
        description=description,
        key_skills=("Python", "SQL", "LLM"),
    )

    backend = PythonBackendRules().evaluate(vacancy)
    adjacent = AdjacentItRules().evaluate(vacancy)

    assert backend.category is RuleCategory.ROUTED
    assert backend.target_scope is DirectionScope.IT_ADJACENT
    assert adjacent.category is RuleCategory.STRETCH


@pytest.mark.parametrize(
    ("hh_id", "title", "description"),
    [
        (
            "135082692",
            "Junior Software Development Engineer in Test (Quality & AI focus)",
            (
                "Писать автоматизированные тесты и скрипты на Python, Java или C#, "
                "поддерживать инфраструктуру качества и CI/CD."
            ),
        ),
        (
            "134855429",
            "Тестировщик Web-приложения HypeScribe",
            (
                "Выполнять ручное тестирование всех функций. "
                "Писать код тестирования и поддерживать автотесты ключевых функций."
            ),
        ),
    ],
)
def test_live_automated_or_mixed_testing_roles_are_stretch_in_it(
    hh_id: str,
    title: str,
    description: str,
) -> None:
    vacancy = VacancyData(
        hh_id,
        title,
        f"https://hh.ru/vacancy/{hh_id}",
        description=description,
        key_skills=("Python", "Автоматизированное тестирование", "QA"),
    )

    backend = PythonBackendRules().evaluate(vacancy)
    adjacent = AdjacentItRules().evaluate(vacancy)

    assert backend.category is RuleCategory.ROUTED
    assert backend.target_scope is DirectionScope.IT_ADJACENT
    assert adjacent.category is RuleCategory.STRETCH


def test_pure_manual_testing_role_remains_rejected() -> None:
    vacancy = VacancyData(
        "manual-only",
        "Тестировщик Web-приложения",
        "https://hh.ru/vacancy/manual-only",
        description=(
            "Только ручное тестирование интерфейса, составление сценариев "
            "и оформление отчетов об ошибках."
        ),
        key_skills=("Ручное тестирование", "Составление баг-репортов"),
    )

    assert PythonBackendRules().evaluate(vacancy).category is RuleCategory.REJECTED
    assert AdjacentItRules().evaluate(vacancy).category is RuleCategory.REJECTED


def test_live_pyqt_qml_role_is_stretch_in_it() -> None:
    vacancy = VacancyData(
        "134577494",
        "Программист (Python / PyQt/QML)",
        "https://hh.ru/vacancy/134577494",
        description=(
            "Разработка прикладного программного обеспечения на Python, PyQt и QML "
            "для взаимодействия с промышленным оборудованием."
        ),
        key_skills=("Python", "PyQt", "QML"),
    )

    backend = PythonBackendRules().evaluate(vacancy)
    adjacent = AdjacentItRules().evaluate(vacancy)

    assert backend.category is RuleCategory.ROUTED
    assert backend.target_scope is DirectionScope.IT_ADJACENT
    assert adjacent.category is RuleCategory.STRETCH


def test_live_build_and_infrastructure_role_is_stretch_in_it() -> None:
    vacancy = VacancyData(
        "135558523",
        "Младший инженер-программист по безопасной разработке",
        "https://hh.ru/vacancy/135558523",
        description=(
            "Доработка и поддержка системы сборки, внедрение процессов CI/CD, "
            "обеспечение доступности сервисов и инфраструктуры."
        ),
        required_qualifications=(
            "Опыт работы с CMake, Makefile и Autotools. Написание скриптов на Python и Bash."
        ),
        key_skills=("Linux", "CI/CD", "Python", "Bash", "CMake"),
    )

    backend = PythonBackendRules().evaluate(vacancy)
    adjacent = AdjacentItRules().evaluate(vacancy)

    assert backend.category is RuleCategory.ROUTED
    assert backend.target_scope is DirectionScope.IT_ADJACENT
    assert adjacent.category is RuleCategory.STRETCH
    assert any("сборкой, CI/CD и инфраструктурой" in reason for reason in adjacent.reasons)


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
            experience="От 1 года до 3 лет",
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
    assert any("обязательный стаж от трёх лет" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "required_qualifications",
    [
        "Опыт коммерческой разработки от 2 лет.",
        "Коммерческий опыт разработки от 2,5 лет на Python/Django.",
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


@pytest.mark.parametrize(
    "required_qualifications",
    [
        "Опыт разработки на Python от 3 лет.",
        "Опыт промышленной разработки на Python от трех лет.",
        "Опыт разработки backend-сервисов минимум 5 лет.",
        "Python development experience: 3+ years.",
    ],
)
def test_mandatory_three_plus_years_of_development_requires_manual_review(
    required_qualifications: str,
) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"hard-experience-{required_qualifications}",
            "Python backend разработчик",
            "https://hh.ru/vacancy/hard-experience",
            description="Разработка API на Python и FastAPI.",
            required_qualifications=required_qualifications,
        )
    )

    assert result.category is RuleCategory.STRETCH
    assert any("обязательный стаж от трёх лет" in reason for reason in result.reasons)


def test_mandatory_fullstack_experience_is_stretch_in_it_direction() -> None:
    result = AdjacentItRules().evaluate(
        VacancyData(
            "fullstack-experience",
            "Fullstack-разработчик",
            "https://hh.ru/vacancy/fullstack-experience",
            description="Разработка API на Python и интерфейса на React.",
            required_qualifications=(
                "От 2 лет коммерческого опыта в fullstack- или backend-разработке."
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


def test_mandatory_frontend_stack_missing_from_profile_is_stretch() -> None:
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

    assert result.category is RuleCategory.STRETCH
    assert any("клиентский стек" in reason for reason in result.reasons)


def test_mandatory_message_broker_missing_from_profile_is_stretch() -> None:
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

    assert result.category is RuleCategory.STRETCH
    assert any("брокер сообщений" in reason for reason in result.reasons)


def test_mandatory_django_missing_from_profile_is_stretch() -> None:
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

    assert result.category is RuleCategory.STRETCH
    assert any("Django" in reason for reason in result.reasons)


def test_bell_integrator_pattern_is_rejected_for_hard_experience_and_skill_gaps() -> None:
    result = AdjacentItRules().evaluate(
        VacancyData(
            "bell-integrator-live-pattern",
            "Python-разработчик RAG-систем",
            "https://hh.ru/vacancy/133722627",
            description="Разработка серверных сервисов на Python для ИИ-агентов.",
            required_qualifications=(
                "Обязателен коммерческий опыт разработки от 3,5 лет. "
                "Практический опыт RAG / pgvector / ReAct."
            ),
            key_skills=("Python", "RAG", "pgvector"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL, RAG",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("обязательный стаж от трёх лет" in reason for reason in result.reasons)
    assert any(
        "несколько обязательных профильных технологий" in reason for reason in result.reasons
    )


def test_founding_ai_engineer_with_multiple_unverified_requirements_is_rejected() -> None:
    result = AdjacentItRules().evaluate(
        VacancyData(
            "founding-ai-live-pattern",
            "Founding AI Engineer",
            "https://hh.ru/vacancy/135597368",
            description="Разработка агентной платформы и Python API с нуля.",
            required_qualifications=(
                "Обязателен опыт с LangGraph и PydanticAI, "
                "а также проектирование архитектуры AI-агентов."
            ),
            key_skills=("Python", "LLM"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL, LLM, RAG",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any(
        "несколько обязательных профильных технологий" in reason for reason in result.reasons
    )
    assert any("роль первого инженера" in reason for reason in result.reasons)


def test_sbermed_qa_with_three_to_six_years_requires_manual_review() -> None:
    result = AdjacentItRules().evaluate(
        VacancyData(
            "sbermed-qa-live-pattern",
            "Инженер по автоматизированному тестированию Python",
            "https://hh.ru/vacancy/sbermed-qa",
            description="Разработка автотестов на Python и pytest.",
            experience="От 3 до 6 лет",
            key_skills=("Python", "pytest"),
        ),
        RuleContext(skills=("Python, pytest",)),
    )

    assert result.category is RuleCategory.STRETCH
    assert any("диапазон опыта hh.ru" in reason for reason in result.reasons)


def test_one_unverified_specialist_requirement_is_stretch() -> None:
    result = AdjacentItRules().evaluate(
        VacancyData(
            "one-specialist-gap",
            "LLM Python-разработчик",
            "https://hh.ru/vacancy/one-specialist-gap",
            description="Разработка серверной части ИИ-помощника.",
            required_qualifications="Обязателен практический опыт с LangGraph.",
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL, LLM, RAG",)),
    )

    assert result.category is RuleCategory.STRETCH
    assert any("LangGraph" in reason and "ручной проверки" in reason for reason in result.reasons)


def test_junior_role_ignores_optional_experience_and_specialist_technologies() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "junior-with-optional-ai",
            "Junior Python backend разработчик",
            "https://hh.ru/vacancy/junior-with-optional-ai",
            description="Разработка API на Python и FastAPI.",
            required_qualifications=(
                "Знание Python и основ REST. Будет плюсом: опыт от 3 лет с LangGraph и pgvector."
            ),
            experience="От 1 года до 3 лет",
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL",)),
    )

    assert result.category is RuleCategory.MATCH
    assert not any("обязательный стаж" in reason for reason in result.reasons)
    assert not any("обязательных профильных технологий" in reason for reason in result.reasons)


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
    assert adjacent_result.category is RuleCategory.STRETCH


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


def test_optional_ruby_stack_requires_manual_review() -> None:
    result = AdjacentItRules().evaluate(
        VacancyData(
            "adjacent-ruby",
            "Стажер Backend-разработчик",
            "https://hh.ru/vacancy/ruby-stack",
            description=(
                "Наш стек: Ruby, Ruby on Rails, PostgreSQL и Redis. "
                "Знание стека будет преимуществом, но не обязательно."
            ),
            key_skills=("Ruby", "Ruby on Rails", "PostgreSQL", "Git"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL, Git",)),
    )

    assert result.category is RuleCategory.STRETCH
    assert any("Ruby/Rails" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("title", "stack"),
    [
        ("Ruby on Rails разработчик", "Ruby/Rails"),
        ("Стажер-разработчик ABAP", "ABAP/SAP"),
    ],
)
def test_adjacent_it_rejects_other_primary_stack(
    title: str,
    stack: str,
) -> None:
    result = AdjacentItRules().evaluate(
        VacancyData(
            f"adjacent-other-{stack}",
            title,
            "https://hh.ru/vacancy/other-stack",
            description="Разработка серверных приложений и работа с PostgreSQL.",
            key_skills=("PostgreSQL", "Git"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL, Git",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any(stack in reason for reason in result.reasons)


def test_n8n_role_requires_manual_review() -> None:
    result = AdjacentItRules().evaluate(
        VacancyData(
            "n8n-integrations",
            "Инженер интеграций / n8n-разработчик",
            "https://hh.ru/vacancy/n8n-integrations",
            description=(
                "Вы — первый и единственный инженер проекта. "
                "3+ года: интеграции, автоматизация процессов, ETL/RPA. "
                "n8n / Make или Python — на уровне промышленной эксплуатации."
            ),
            key_skills=("Python", "API", "SQL"),
        ),
        RuleContext(skills=("Python, API, SQL",)),
    )

    assert result.category is RuleCategory.STRETCH
    assert any("от трёх лет" in reason for reason in result.reasons)
    assert any("первого инженера" in reason for reason in result.reasons)


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
