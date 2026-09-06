from __future__ import annotations

from itertools import permutations

import pytest

from hugin.domain.vacancies import VacancyData
from hugin.services.requirement_sections import (
    RequirementKind,
    description_sections,
    mandatory_text,
    primary_duties,
    primary_requirements,
    requirement_sections,
)
from hugin.services.vacancy_analysis import (
    AdjacentItRules,
    PythonBackendRules,
    RuleCategory,
    RuleContext,
)


@pytest.mark.parametrize("separator", ["\n", "\r\n", " "])
@pytest.mark.parametrize("structured", [False, True])
def test_required_stack_survives_section_reordering(separator: str, structured: bool) -> None:
    sections = (
        "Требования: Python, FastAPI. Обязателен опыт разработки на Java.",
        "Будет плюсом: Redis.",
        "Мы предлагаем: обучение Go и PHP.",
    )
    rules = PythonBackendRules()
    context = RuleContext(skills=("Python", "FastAPI", "PostgreSQL"))
    for ordered in permutations(sections):
        text = separator.join(ordered)
        vacancy = VacancyData(
            hh_id="sections",
            title="Python backend-разработчик",
            source_url="https://hh.ru/vacancy/1",
            description="Разработка API на Python." if structured else text,
            responsibilities="Разработка серверных API на Python.",
            required_qualifications=text if structured else None,
        )
        evaluation = rules.evaluate(vacancy, context)
        assert evaluation.category is RuleCategory.REJECTED
        required = rules._mandatory_requirements(vacancy)
        assert "java" in required
        assert "redis" not in required
        assert "php" not in required


def test_sections_preserve_original_evidence_and_offsets() -> None:
    text = "О команде.\nБудет плюсом: Redis.\nТребования: Python и SQL."
    sections = requirement_sections(text)
    assert [part.kind for part in sections] == [
        RequirementKind.UNLABELLED,
        RequirementKind.OPTIONAL,
        RequirementKind.REQUIRED,
    ]
    assert all(text[part.start : part.end] == part.text for part in sections)
    assert mandatory_text(text, include_unlabelled=False) == "python и sql."


@pytest.mark.parametrize(
    "text",
    [
        "Желательно Redis, но обязателен опыт Java.",
        "Желательно Redis; обязателен опыт Java.",
        "Желательно Redis. Обязателен опыт Java.",
    ],
)
def test_optional_clause_does_not_hide_required_clause(text: str) -> None:
    required = mandatory_text(text, include_unlabelled=True)
    assert "java" in required
    assert "redis" not in required


def test_company_offer_does_not_replace_actual_duties_in_any_section_order() -> None:
    sections = (
        "Чем ты будешь заниматься:\nРазрабатывать интерфейсы на C++ и Qt5.",
        "Что ждём:\nЗнание C++ и Python.",
        "Преимуществом будет:\nJava.",
        "Мы предлагаем:\nЗадачи по развитию сервисов компании.\nКонкурентная зарплата.",
    )
    for ordered in permutations(sections):
        text = "\n".join(ordered)
        assert description_sections(text) == (
            "Разрабатывать интерфейсы на C++ и Qt5.",
            "Знание C++ и Python.",
            "Java.",
        )
        assert primary_duties(text, "Конкурентная зарплата.") == (
            "Разрабатывать интерфейсы на C++ и Qt5."
        )
        assert "java" not in mandatory_text(text, include_unlabelled=False)
        assert all(text[part.start : part.end] == part.text for part in requirement_sections(text))


def test_offer_only_fallback_is_not_evidence_of_duties() -> None:
    assert primary_duties("Мы предлагаем:\nКонкурентная зарплата.", "Конкурентная зарплата.") == ""
    assert primary_duties(None, "Разрабатывать API.") == "Разрабатывать API."


def test_requirements_for_future_team_member_end_the_duties_section() -> None:
    text = (
        "Задачи:\nРазрабатывать API на Python.\n"
        "Мы ожидаем от будущего члена команды:\nЗнание Python и PostgreSQL.\n"
        "Опыт C++ будет плюсом."
    )
    assert primary_duties(text, None) == "Разрабатывать API на Python."
    assert mandatory_text(text, include_unlabelled=False) == "знание python и postgresql."


@pytest.mark.parametrize(
    "heading", ["Будет являться существенным плюсом", "Будет большим преимуществом"]
)
def test_full_source_separates_legacy_stack_from_optional_experience(heading: str) -> None:
    text = (
        "Переводим прежнюю платформу с PHP на Python.\n"
        "Обязанности:\nРазрабатывать серверные API на Python.\n"
        "Требования:\nPython, FastAPI, SQL.\n"
        f"{heading}:\nЗнание PHP.\n"
        "Мы предлагаем:\nОбучение Java."
    )
    data = VacancyData(
        hh_id="legacy",
        title="Python backend-разработчик",
        source_url="https://hh.ru/vacancy/legacy",
        description=text,
        required_qualifications="Python, FastAPI, SQL. Знание PHP.",
    )
    result = PythonBackendRules().evaluate(data, RuleContext(skills=("Python", "FastAPI", "SQL")))
    assert result.accepted
    assert "php" not in PythonBackendRules._mandatory_requirements(data)
    assert "java" not in PythonBackendRules._mandatory_requirements(data)


def test_full_requirements_restore_a_mandatory_stack_missing_from_stored_fields() -> None:
    data = VacancyData(
        hh_id="required",
        title="Python backend-разработчик",
        source_url="https://hh.ru/vacancy/required",
        description="Требования:\nPython, FastAPI. Обязательна разработка на Java.",
        required_qualifications="Python, FastAPI.",
    )
    result = PythonBackendRules().evaluate(data, RuleContext(skills=("Python", "FastAPI")))
    assert result.category is RuleCategory.REJECTED
    assert any("Java" in reason for reason in result.reasons)


def test_fallback_is_used_only_when_full_description_has_no_requirements() -> None:
    assert primary_requirements("Разрабатываем API.", "Python и SQL.") == "python и sql."
    assert primary_requirements("Будет плюсом:\nЗнание Java.", "Знание Java.") == ""
    assert (
        primary_requirements("Требования:\nPython. Java будет большим плюсом.", "Java.")
        == "python."
    )


def test_duties_do_not_replace_unlabelled_mandatory_requirements() -> None:
    data = VacancyData(
        hh_id="network",
        title="Сетевой инженер",
        source_url="https://hh.ru/vacancy/network",
        description=(
            "Глубокое понимание маршрутизации OSPF, BGP и сетевого стека VMware.\n"
            "Опыт администрирования Windows Server и Linux.\n"
            "Обязанности:\nВести документацию и устранять сетевые сбои."
        ),
        responsibilities="Вести документацию и устранять сетевые сбои.",
    )
    result = AdjacentItRules().evaluate(data, RuleContext(skills=("Python", "Docker", "SQL")))
    assert not result.accepted
    assert "вести документацию" not in PythonBackendRules._mandatory_requirements(data)


def test_duty_system_names_are_not_mandatory_candidate_experience() -> None:
    data = VacancyData(
        hh_id="etl",
        title="Python-разработчик (ETL)",
        source_url="https://hh.ru/vacancy/etl",
        description=(
            "Обязанности:\nРазработка ETL на Python. Подключать источники к DWH.\n"
            "Интегрировать ETL в Airflow."
        ),
        responsibilities="Разработка ETL на Python. Подключать источники к DWH и Airflow.",
    )
    result = AdjacentItRules().evaluate(
        data, RuleContext(skills=("Python", "SQL", "Docker", "pandas"))
    )
    assert result.accepted
    assert PythonBackendRules._mandatory_requirements(data) == ""


def test_inline_optional_experience_does_not_end_required_section() -> None:
    text = (
        "Требования\nОпыт Python.\nОпыт работы с Go будет преимуществом\n"
        "Опыт проектирования архитектуры распределённых систем.\n"
        "Условия\nРабота в офисе."
    )
    required = primary_requirements(text, None)
    assert "go" not in required
    assert "python" in required
    assert "распределённых систем" in required


def test_phrase_inside_offer_does_not_create_candidate_requirements() -> None:
    text = (
        "Требования:\nPython и SQL.\nУсловия:\n"
        "Полная удалёнка (будем рады видеть в офисе)\n"
        "Стек разработки другой команды: PHP, Go."
    )
    assert primary_requirements(text, None) == "python и sql."


@pytest.mark.parametrize("ending", [":", ""])
def test_heading_annotation_preserves_required_and_optional_boundaries(ending: str) -> None:
    text = (
        f"Наши ожидания (Hard Skills){ending}\nPython, SQL.\n"
        f"Будет большим плюсом (специфика){ending}\nGo и Java.\n"
        f"Что нужно будет делать{ending}\nРазрабатывать API на Python."
    )
    assert primary_requirements(text, None) == "python, sql."
    assert primary_duties(text, None) == "Разрабатывать API на Python."


def test_stack_heading_is_candidate_requirement_without_using_company_preamble() -> None:
    text = (
        "Наши Java-разработчики помогают новой команде.\n"
        "Основные технологии:\nPython, TypeScript, React.\n"
        "Будет плюсом:\nC++ и Go."
    )
    assert primary_requirements(text, None) == "python, typescript, react."
