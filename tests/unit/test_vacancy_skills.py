from __future__ import annotations

from dataclasses import replace

from hugin.domain.vacancies import VacancyData
from hugin.services.vacancy_analysis import PythonBackendRules, RuleContext
from hugin.services.vacancy_skills import skill_terms


def test_technology_aliases_and_punctuation_are_normalized_once() -> None:
    assert skill_terms("Postgres, PostgreSQL, PL/SQL, C++17, cpp, C#, .NET, dotnet, K8s") == {
        "postgresql",
        "plsql",
        "c++",
        "c#",
        ".net",
        "kubernetes",
    }
    assert skill_terms("TCP/IP, tcpip, CI/CD, PySpark, Spark, Qt6, Qt") == {
        "tcp/ip",
        "ci/cd",
        "spark",
        "qt",
    }
    assert skill_terms("NoSQL, JavaScript, TypeScript, CodePython, PostgreSQLish") == {
        "nosql",
        "javascript",
        "typescript",
    }


def test_profile_prose_and_company_offer_do_not_inflate_skills_component() -> None:
    data = VacancyData(
        "skills",
        "Python backend разработчик",
        "https://hh.ru/vacancy/skills",
        description=(
            "Обязанности:\nРазрабатывать API на Python.\n"
            "Требования:\nPython и SQL.\n"
            "Мы предлагаем:\nОбучение Go, Kafka, Kubernetes, React и Spark."
        ),
    )
    rules = PythonBackendRules()

    def component(vacancy: VacancyData, context: RuleContext) -> tuple[float, str]:
        result = rules.evaluate(vacancy, context)
        part = next(item for item in result.components if item.name == "skills")
        return part.score, part.reason

    plain = RuleContext(skills=("Python, SQL",))
    verbose = RuleContext(skills=("Python и SQL для обработки через внутренние системы",))
    assert rules._profile_skill_tokens(verbose.skills) == {"python", "sql"}
    assert component(data, plain) == component(data, verbose)
    assert data.description is not None
    assert component(data, plain) == component(
        replace(
            data, description=data.description.replace("Обучение Go", "Для обработки через Go")
        ),
        verbose,
    )
    extended_profile = replace(plain, skills=("Python, SQL, Go, Kafka, Kubernetes, React, Spark",))
    assert component(data, plain) == component(data, extended_profile)


def test_aliases_match_but_common_words_do_not_hide_missing_skills() -> None:
    data = VacancyData(
        "database",
        "Python backend разработчик",
        "https://hh.ru/vacancy/database",
        description="Обязанности:\nРазрабатывать API.\nТребования:\nPython и Postgres.",
    )
    result = PythonBackendRules().evaluate(data, RuleContext(skills=("Python, PostgreSQL",)))
    part = next(item for item in result.components if item.name == "skills")
    assert "postgresql" in part.reason
    assert skill_terms("Для обработки через студию готовлю проекты команды") == set()
