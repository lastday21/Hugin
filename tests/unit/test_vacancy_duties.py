from __future__ import annotations

from dataclasses import replace

import pytest

from hugin.domain.directions import DirectionScope
from hugin.domain.vacancies import VacancyData
from hugin.services.vacancy_analysis import AdjacentItRules, RuleContext, VacancyRoleRouter
from hugin.services.vacancy_duties import technical_duty_evidence


def vacancy(title: str, description: str) -> VacancyData:
    return VacancyData(
        hh_id="duties",
        title=title,
        description=description,
        source_url="https://hh.ru/vacancy/duties",
    )


@pytest.mark.parametrize("title", ["Инженер по сопровождению", "Стажер в Департамент рисков"])
def test_general_title_uses_actual_technical_duties(title: str) -> None:
    duty = (
        "Администрирование Linux и анализ логов."
        if "Инженер" in title
        else "Участвовать в разработке ИИ для обработки событий."
    )
    data = vacancy(title, f"Обязанности:\n{duty}\nТребования:\nPython, SQL и Excel.")
    evidence = technical_duty_evidence(data)
    assert data.description is not None
    assert evidence and all(item.text in data.description for item in evidence)
    assert VacancyRoleRouter.classify(data) is DirectionScope.IT_ADJACENT
    assert AdjacentItRules().evaluate(data, RuleContext(skills=("Python", "SQL", "Excel"))).accepted


def test_company_work_and_training_offer_do_not_turn_non_it_duties_into_it() -> None:
    data = vacancy(
        "Стажер отдела",
        "Наши разработчики создают API и поддерживают серверы.\n"
        "Обязанности:\nДоставлять документы клиентам.\n"
        "Мы предлагаем:\nОбучение: разрабатывать API и администрировать Linux.",
    )
    assert technical_duty_evidence(data) == ()
    assert VacancyRoleRouter.classify(data) is None
    assert not AdjacentItRules().evaluate(data).accepted
    assert not technical_duty_evidence(replace(data, responsibilities="Администрировать Linux."))
    assert not technical_duty_evidence(
        vacancy("Стажер отдела", "Работа с командой, которая создаёт сервисы на Python.")
    )


def test_cluster_administration_is_distinguished_from_junior_and_developer_use() -> None:
    context = RuleContext(skills=("Python", "Docker", "PostgreSQL"))
    data = vacancy(
        "DevOps-инженер",
        "Ты нам подходишь, если:\n"
        "Имеешь коммерческий опыт с Kubernetes (администрирование, troubleshooting).\n"
        "Умеешь писать скрипты Python.\nБудет плюсом:\nGo и Java.",
    )
    result = AdjacentItRules().evaluate(data, context)
    assert not result.accepted
    assert any("администрирование кластеров Kubernetes" in reason for reason in result.reasons)
    assert not any(
        "основной стек: Go" in reason or "основной стек: Java" in reason
        for reason in result.reasons
    )
    confirmed = replace(context, skills=(*context.skills, "Kubernetes"))
    assert AdjacentItRules().evaluate(data, confirmed).accepted
    for text in (
        "Требования:\nPython и Docker.\nБудет плюсом:\nАдминистрирование Kubernetes.",
        "Требования:\nPython. Знакомство с Kubernetes на уровне разработчика.",
    ):
        assert AdjacentItRules().evaluate(replace(data, description=text), context).accepted


def test_recognized_development_duties_do_not_bypass_mandatory_model_training() -> None:
    data = vacancy(
        "Инженер по машинному обучению",
        "Обязанности:\nРазработка и обучение современных языковых моделей.\n"
        "Требования:\nПрактический опыт дообучения предобученных языковых моделей.",
    )
    result = AdjacentItRules().evaluate(
        data, RuleContext(skills=("Python", "FastAPI", "YandexGPT"))
    )
    assert not result.accepted
    assert any("обучение моделей" in reason for reason in result.reasons)


def test_manual_functional_testing_keeps_third_priority() -> None:
    data = vacancy(
        "Стажер по направлению Ручное функциональное тестирование",
        "Обязанности:\nФункциональное тестирование программ.\nТребования:\nОсновы Linux и SQL.",
    )
    result = AdjacentItRules().evaluate(data, RuleContext(skills=("Python", "SQL", "Linux")))
    assert result.accepted and result.fit is not None
    assert result.fit.tier == 3


@pytest.mark.parametrize(
    "duty",
    [
        "Проектировать и наполнять аналитические витрины данных.",
        "Разрабатывать алгоритмы преобразования больших объёмов данных.",
        "Проектировать и развивать аналитические пайплайны и ETL-процессы.",
        "Подготовка скриптов для загрузки и обработки данных.",
        "Участие в разработке прототипов хранилищ данных.",
        "Автоматизировать рутинные отчёты через скрипты на Python.",
        "Выполнять задачи команды и развивать ETL-процессы.",
        "Самостоятельно проектировать витрины данных, а сверку выполняет другая команда.",
        "В команде разрабатывать витрины данных.",
        "Не только разрабатывать витрины данных, но и проверять их качество.",
    ],
)
def test_data_analyst_with_technical_duties_is_a_related_it_role(duty: str) -> None:
    data = vacancy("Аналитик данных", f"Обязанности:\n{duty}\nТребования:\nPython и SQL.")
    context = RuleContext(skills=("Python", "SQL", "pandas"))

    evidence = technical_duty_evidence(data)
    result = AdjacentItRules().evaluate(data, context)

    assert evidence and all(item.text == duty for item in evidence)
    assert VacancyRoleRouter.classify(data) is DirectionScope.IT_ADJACENT
    assert result.accepted and result.fit is not None
    assert result.fit.tier == 2


@pytest.mark.parametrize(
    "duty",
    [
        "Готовить отчёты о продажах и проводить опросы покупателей.",
        "Анализировать цены поставщиков и согласовывать закупки.",
        "Готовить документацию для отдела, который разрабатывает витрины данных.",
        "Подготовка документации для отдела, который разрабатывает витрины данных.",
        "Разработка инструкций для команды, которая создаёт хранилища данных.",
        "Работать с аналитиками: они разрабатывают витрины данных.",
        "Помогать отделу разработки; разработка витрин данных выполняется другой командой.",
        "Не требуется разрабатывать витрины данных.",
        "Не автоматизировать отчёты через Python, а собирать сведения вручную.",
        "Взаимодействовать с командой разработки витрин данных.",
        "Готовить документацию для команды, разрабатывающей витрины данных.",
        "Не придётся разрабатывать витрины данных, требуется согласовывать документы.",
        "Разрабатывать витрины данных не требуется; нужно вручную сверять отчёты.",
        "Разрабатывать витрины данных будут программисты. Согласовывать документы.",
    ],
)
def test_analyst_title_and_company_data_work_do_not_establish_it_duties(duty: str) -> None:
    data = vacancy(
        "Аналитик",
        "Компания разрабатывает витрины данных на Python.\n"
        f"Обязанности:\n{duty}\n"
        "Будет плюсом:\nРазработка ETL и знание SQL.\n"
        "Мы предлагаем:\nОбучение: создавать хранилища данных.",
    )
    assert technical_duty_evidence(data) == ()
    assert not AdjacentItRules().evaluate(data, RuleContext(skills=("Python", "SQL"))).accepted


def test_data_duties_do_not_bypass_an_unsupported_mandatory_language() -> None:
    data = vacancy(
        "Аналитик данных",
        "Обязанности:\nРазрабатывать витрины данных.\n"
        "Требования:\nОбязателен опыт разработки на Java и Spring.",
    )
    result = AdjacentItRules().evaluate(data, RuleContext(skills=("Python", "SQL")))
    assert not result.accepted
    assert any("основной стек: Java" in reason for reason in result.reasons)


def test_data_duty_after_another_teams_work_is_still_recognized() -> None:
    data = vacancy(
        "Аналитик данных",
        "Обязанности:\nРаботать с командой, которая создаёт API; "
        "самостоятельно проектировать витрины данных.\nТребования:\nPython и SQL.",
    )
    assert [item.text for item in technical_duty_evidence(data)] == [
        "самостоятельно проектировать витрины данных."
    ]
    assert AdjacentItRules().evaluate(data, RuleContext(skills=("Python", "SQL"))).accepted


@pytest.mark.parametrize(
    ("title", "requirements", "reason"),
    [
        ("Бухгалтер-аналитик", "Бухгалтерский учёт, отчётность и 1С.", "бухгалтерия"),
        ("Менеджер по продажам", "Опыт продаж.", "работа не связана с разработкой"),
        ("Аналитик данных", "Обязательны Scala и Spark.", "основной стек: Scala"),
        (
            "Аналитик данных",
            "Практический опыт обучения нейронных сетей на PyTorch.",
            "обучение моделей",
        ),
        (
            "Аналитик данных",
            "Обязателен промышленный опыт Hadoop, Spark, Kafka.",
            "промышленный стек обработки данных",
        ),
    ],
)
def test_analytical_duties_preserve_profession_and_specialization_boundaries(
    title: str, requirements: str, reason: str
) -> None:
    data = vacancy(
        title,
        f"Обязанности:\nАвтоматизировать отчёты через скрипты Python.\nТребования:\n{requirements}",
    )
    result = AdjacentItRules().evaluate(data, RuleContext(skills=("Python", "SQL", "Excel")))
    assert not result.accepted
    assert any(reason in item for item in result.reasons)


def test_analyst_can_prepare_data_for_another_teams_neural_network_training() -> None:
    data = vacancy(
        "Аналитик данных",
        "Обязанности:\nПодготовка данных для обучения нейронных сетей другой командой.\n"
        "Разрабатывать витрины данных.\nТребования:\nPython и SQL.\n"
        "Будет плюсом:\nПромышленный опыт Hadoop, Spark, Kafka.",
    )
    result = AdjacentItRules().evaluate(data, RuleContext(skills=("Python", "SQL")))
    assert result.accepted


def test_analyst_neural_network_awareness_is_not_model_training_experience() -> None:
    data = vacancy(
        "Аналитик данных",
        "Обязанности:\nРазрабатывать витрины данных.\nТребования:\nPython и SQL.\n"
        "Базовое понимание принципов обучения нейронных сетей.",
    )
    assert AdjacentItRules().evaluate(data, RuleContext(skills=("Python", "SQL"))).accepted
