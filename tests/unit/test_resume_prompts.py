from __future__ import annotations

import json

import pytest

from hugin.services.resume_prompts import (
    QUESTION_PROMPT_VERSION,
    REWRITE_PROMPT_VERSION,
    ResumeBlockKind,
    ResumePromptContext,
    ResumeQuestionAnswer,
    ResumeQuestionTopic,
    build_questions_prompt,
    build_rewrite_prompt,
    parse_question_assessments,
    select_missing_questions,
)


def context() -> ResumePromptContext:
    return ResumePromptContext(
        kind=ResumeBlockKind.PROJECT,
        source_block=(
            "Проект анализа данных: разработал алгоритм, проверил на двух выборках, "
            "точность 92%. Стек: Python, pandas, numpy."
        ),
        target_role="Python backend-разработчик",
        vacancy_context="Python, API, PostgreSQL, Docker",
    )


def test_question_prompt_requires_evidence_for_five_fixed_topics() -> None:
    prompt = build_questions_prompt(context())

    assert QUESTION_PROMPT_VERSION == "resume_questions_v3"
    assert "Проверь ровно пять" in prompt
    assert "personal_contribution" in prompt
    assert "короткая точная выдержка" in prompt
    assert "не требуй другую метрику" in prompt
    assert "сами по себе не подтверждают project_status" in prompt
    assert "не спрашивай о событиях после ухода" in prompt
    assert "слова «участвовал»" in prompt
    assert "интеграция с явно названным сервисом" in prompt
    assert "подтверждать именно проверяемую тему" in prompt
    assert "которых нет в исходном блоке" in prompt
    assert "точность 92%" in prompt


def test_question_analysis_selects_only_missing_topics() -> None:
    payload = [
        {
            "topic": "personal_contribution",
            "known": True,
            "evidence": "Разработал алгоритм.",
            "question": None,
        },
        {"topic": "result", "known": True, "evidence": "Точность 92%.", "question": None},
        {
            "topic": "project_status",
            "known": True,
            "evidence": "Передал в разработку.",
            "question": None,
        },
        {
            "topic": "scale",
            "known": False,
            "evidence": None,
            "question": "Какой объем данных обрабатывался?",
        },
        {
            "topic": "collaboration",
            "known": False,
            "evidence": None,
            "question": "С какими командами вы работали?",
        },
    ]
    response = f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"

    assessments = parse_question_assessments(response)

    assert assessments[0].topic is ResumeQuestionTopic.PERSONAL_CONTRIBUTION
    assert assessments[0].known
    assert select_missing_questions(assessments) == (
        "Какой объем данных обрабатывался?",
        "С какими командами вы работали?",
    )


def test_question_analysis_rejects_missing_evidence() -> None:
    response = """[
      {"topic":"personal_contribution","known":true,"evidence":null,"question":null},
      {"topic":"result","known":true,"evidence":"Результат.","question":null},
      {"topic":"project_status","known":true,"evidence":"Стадия.","question":null},
      {"topic":"scale","known":false,"evidence":null,"question":"Какой масштаб?"},
      {"topic":"collaboration","known":false,"evidence":null,"question":"Какая команда?"}
    ]"""

    with pytest.raises(ValueError, match="нужна выдержка"):
        parse_question_assessments(response)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ("not-json", "некорректный JSON"),
        ("[]", "пяти тем"),
        (
            """[
            {"topic":"result","known":true,"evidence":"Есть.","question":null},
            {"topic":"personal_contribution","known":true,"evidence":"Есть.","question":null},
            {"topic":"project_status","known":true,"evidence":"Есть.","question":null},
            {"topic":"scale","known":true,"evidence":"Есть.","question":null},
            {"topic":"collaboration","known":true,"evidence":"Есть.","question":null}
            ]""",
            "заданном порядке",
        ),
    ],
)
def test_question_analysis_rejects_invalid_structure(response: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_question_assessments(response)


def test_missing_question_limit_is_bounded() -> None:
    assessments = parse_question_assessments(
        json.dumps(
            [
                {
                    "topic": topic.value,
                    "known": False,
                    "evidence": None,
                    "question": f"Вопрос {index}?",
                }
                for index, topic in enumerate(ResumeQuestionTopic, start=1)
            ],
            ensure_ascii=False,
        )
    )

    assert select_missing_questions(assessments) == ("Вопрос 1?", "Вопрос 2?", "Вопрос 3?")
    with pytest.raises(ValueError, match="от одного до трех"):
        select_missing_questions(assessments, limit=4)


def test_rewrite_prompt_preserves_facts_and_requires_active_style() -> None:
    prompt = build_rewrite_prompt(
        context(),
        (
            ResumeQuestionAnswer(
                "Было ли решение внедрено?",
                "Подтверждена только передача в дальнейшую разработку.",
            ),
            ResumeQuestionAnswer(
                "Каков объем данных?",
                "Точный объем данных не фиксировался.",
            ),
        ),
    )

    assert REWRITE_PROMPT_VERSION == "resume_rewrite_v4"
    assert "используй только явно указанные факты" in prompt
    assert "не подменяй содержание прошлой работы" in prompt
    assert "полностью исключай из ответа" in prompt
    assert "не пропускай отдельный результат" in prompt
    assert "а не смешивай их с задачами" in prompt
    assert "не придумывай практический эффект" in prompt
    assert "помещай в раздел результатов" in prompt
    assert "не повторяй один факт" in prompt
    assert "[результат]" in prompt
    assert "не объявляй решение работающим" in prompt
    assert "включая пояснение после" in prompt
    assert "Обязательные факты из источника" in prompt
    assert "точность 92%" in prompt
    assert "нельзя терять отдельный смысл, число, точность" in prompt
    assert "начинай каждый пункт с действия кандидата" in prompt
    assert "проверь орфографию, падежи и согласование слов" in prompt
    assert "Подтверждена только передача" in prompt
    assert "Название проекта" in prompt
    assert "Вклад:" in prompt
    assert "Результат:" in prompt
    assert "Компания, должность, даты, контакты" in prompt


def test_prompt_rejects_empty_context_and_accepts_no_questions() -> None:
    empty = ResumePromptContext(
        kind=ResumeBlockKind.WORK_EXPERIENCE,
        source_block=" ",
        target_role="Python-разработчик",
    )

    with pytest.raises(ValueError, match="Исходный блок"):
        build_questions_prompt(empty)
    assert "Уточняющих вопросов не потребовалось" in build_rewrite_prompt(context())


def test_project_prompt_uses_project_format() -> None:
    prompt = build_rewrite_prompt(context())

    assert "Название проекта" in prompt
    assert "Назначение:" in prompt
    assert "Вклад:" in prompt
    assert "Результат:" in prompt


def test_work_prompt_marks_confirmed_achievements_but_not_future_plans_as_required() -> None:
    work = ResumePromptContext(
        kind=ResumeBlockKind.WORK_EXPERIENCE,
        source_block="""Обязанности
- Разработал сервис.
Достижения
1) Ускорил доступ к данным за счет кэша.
2) Заложил возможность будущей интеграции с Kafka как опциональную функцию.
Стек
Python, Redis.""",
        target_role="Python-разработчик",
    )

    prompt = build_rewrite_prompt(work)
    required_section = prompt.split("Обязательные факты из источника:", 1)[1].split(
        "Каждый обязательный факт", 1
    )[0]

    assert "Ускорил доступ к данным" in required_section
    assert "Kafka" not in required_section
