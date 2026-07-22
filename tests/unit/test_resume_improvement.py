from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from docx import Document
from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import CandidateProfileModel, ResumeModel
from hugin.repositories import AccountRepository, ResumeRepository
from hugin.services.resume_improvement import (
    ImprovedResumeBlock,
    ResumeBlockExtractor,
    ResumeImprovementService,
    ResumeNarrativeBlock,
)
from hugin.services.resume_prompts import ResumeBlockKind, ResumePromptContext

SOURCE_TEXT = """Иван Иванов
ivan@example.com
Опыт работы — 4 года
Январь 2024 —
настоящее время
2 года
Компания Альфа
Информационные технологии
• Разработка программного обеспечения
Разработчик
Разрабатывал сервис обработки заказов и отвечал за серверную часть приложения на Python.
- Реализовал создание и изменение заказов.
- Настроил журналирование ошибок.
Проекты:
- Сервис уведомлений - отправляет сообщения пользователям после изменения заказа.
Реализовал очередь задач и повторную отправку сообщений.
- Панель отчетов - показывает состояние заказов и ошибки обработки.
Подготовил API для получения сводных данных.
Стек: Python, PostgreSQL, Redis.
Август 2021 —
Декабрь 2023
2 года 5 месяцев
Компания Бета
Аналитик
- Анализировал данные и готовил расчеты.
Проект прогноза:
- Разработал алгоритм прогноза.
- Проверил результат на двух выборках с точностью 92%.
Стек: Python, pandas.
Образование
Университет
"""


def question_response() -> str:
    return json.dumps(
        [
            {
                "topic": "personal_contribution",
                "known": True,
                "evidence": "Разрабатывал сервис.",
                "question": None,
            },
            {
                "topic": "result",
                "known": True,
                "evidence": "Настроил журналирование ошибок.",
                "question": None,
            },
            {
                "topic": "project_status",
                "known": False,
                "evidence": None,
                "question": "На какой стадии находилось решение?",
            },
            {
                "topic": "scale",
                "known": False,
                "evidence": None,
                "question": "Какой объем данных обрабатывался?",
            },
            {
                "topic": "collaboration",
                "known": True,
                "evidence": "Отвечал за серверную часть.",
                "question": None,
            },
        ],
        ensure_ascii=False,
    )


def test_extractor_splits_workplaces_and_projects_and_assembles() -> None:
    extractor = ResumeBlockExtractor()
    structure = extractor.extract(SOURCE_TEXT)

    assert [(block.kind.value, block.label) for block in structure.blocks] == [
        ("WORK_EXPERIENCE", "Компания Альфа — Разработчик"),
        ("PROJECT", "Сервис уведомлений"),
        ("PROJECT", "Панель отчетов"),
        ("WORK_EXPERIENCE", "Компания Бета — Аналитик"),
        ("PROJECT", "прогноза"),
    ]
    assert "Стек: Python, PostgreSQL, Redis." not in structure.blocks[2].source_text

    improved = tuple(
        _improved(block, f"Улучшенный блок {block.index}") for block in structure.blocks
    )
    assembled = extractor.assemble(structure, improved)

    assert "ivan@example.com" in assembled
    assert "Компания Альфа" in assembled
    assert "Проекты:" in assembled
    assert "Стек: Python, PostgreSQL, Redis." in assembled
    assert "Образование\nУниверситет" in assembled
    assert "Реализовал очередь задач" not in assembled
    assert all(f"Улучшенный блок {index}" in assembled for index in range(1, 6))


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "пуст"),
        ("Навыки\nPython", "раздел опыта"),
        ("Опыт работы\nКомпания\nОбразование", "места работы"),
        ("Опыт работы\nЯнварь 2024 —\nКомпания\nРазработчик\nОбразование", "описание"),
    ],
)
def test_extractor_rejects_incomplete_work_history(source: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ResumeBlockExtractor().extract(source)


def test_assembler_requires_every_improved_block() -> None:
    extractor = ResumeBlockExtractor()
    structure = extractor.extract(SOURCE_TEXT)

    with pytest.raises(ValueError, match="все блоки"):
        extractor.assemble(structure, ())


def test_model_text_normalization_and_limits() -> None:
    normalize = ResumeImprovementService._normalize_model_text

    assert normalize("```markdown\n**Вклад:**\n* Сделал сервис\n```") == ("Вклад:\n- Сделал сервис")
    with pytest.raises(ValueError, match="пустой"):
        normalize("  ")
    with pytest.raises(ValueError, match="слишком длинный"):
        normalize("x" * 20_001)


def test_answer_and_vacancy_limit_validation(tmp_path: Path) -> None:
    block = ResumeBlockExtractor().extract(SOURCE_TEXT).blocks[0]

    with pytest.raises(ValueError, match="Не получен ответ"):
        ResumeImprovementService._answer(block, "Вопрос?", lambda *_args: " ")
    with pytest.raises(ValueError, match="слишком длинный"):
        ResumeImprovementService._answer(block, "Вопрос?", lambda *_args: "x" * 4001)

    service = ResumeImprovementService(cast(Session, object()), tmp_path, FakeModel())
    with pytest.raises(ValueError, match="выборки"):
        service.improve(1, lambda *_args: "ответ", vacancy_limit=0)


def test_question_assessment_retries_once_after_invalid_json(tmp_path: Path) -> None:
    class RetryModel:
        model_name = "retry-model"

        def __init__(self) -> None:
            self.responses = iter(("не json", question_response()))
            self.prompts: list[str] = []

        def complete(self, _system_prompt: str, user_prompt: str) -> str:
            self.prompts.append(user_prompt)
            return next(self.responses)

    model = RetryModel()
    service = ResumeImprovementService(cast(Session, object()), tmp_path, model)
    context = ResumePromptContext(
        kind=ResumeBlockKind.PROJECT,
        source_block="Разработал алгоритм и проверил точность.",
        target_role="Python-разработчик",
    )

    assessments = service._assess_questions(context)

    assert len(assessments) == 5
    assert len(model.prompts) == 2
    assert "Предыдущий ответ имел неверный формат" in model.prompts[1]


@pytest.mark.integration
def test_service_creates_separate_draft_without_changing_source(
    settings: Settings,
    tmp_path: Path,
) -> None:
    local_settings = settings.model_copy(update={"data_dir": tmp_path / "data"})
    upgrade_database(local_settings)
    database = create_database(local_settings)
    model = FakeModel()
    answers: list[tuple[int, str]] = []

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "resume-improvement")
            record = ResumeRepository(session).upsert(
                account.id,
                "resume-improvement-source",
                "Python-разработчик",
            )
            resume = session.get(ResumeModel, record.id)
            assert resume is not None
            resume.content_text = _single_workplace_source()
            resume.source_sha256 = "a" * 64
            session.add(
                CandidateProfileModel(
                    account_id=account.id,
                    active_resume_id=resume.id,
                    display_name="Иван Иванов",
                )
            )
            session.flush()

            def answer(block: ResumeNarrativeBlock, question: str) -> str:
                answers.append((block.index, question))
                return "Работало в опытной эксплуатации, точный объем не фиксировался."

            result = ResumeImprovementService(
                session,
                local_settings.data_dir,
                model,
            ).improve(account.id, answer)

            assert result.source_unchanged
            assert result.target_role == "Python-разработчик"
            assert result.model_name == "fake-yandexgpt"
            assert result.draft_path.is_file()
            assert result.report_path.is_file()
            assert len(result.blocks) == 1
            assert len(answers) == 2
            assert session.scalar(select(ResumeModel.content_text)) == _single_workplace_source()

            document_text = "\n".join(
                paragraph.text for paragraph in Document(str(result.draft_path)).paragraphs
            )
            assert "Реализовал API обработки заказов" in document_text
            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            assert report["source_resume_id"] == resume.id
            assert report["question_prompt_version"] == "resume_questions_v3"
            assert report["rewrite_prompt_version"] == "resume_rewrite_v4"
            assert report["blocks"][0]["answers"] == [
                "Работало в опытной эксплуатации, точный объем не фиксировался.",
                "Работало в опытной эксплуатации, точный объем не фиксировался.",
            ]
            assert all("ivan@example.com" not in prompt for prompt in model.prompts)
    finally:
        database.close()


class FakeModel:
    model_name = "fake-yandexgpt"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, _system_prompt: str, user_prompt: str) -> str:
        self.prompts.append(user_prompt)
        if "JSON-массив" in user_prompt:
            return question_response()
        return """Задачи и вклад:
- Реализовал API обработки заказов на Python.
- Настроил хранение данных в PostgreSQL.
Результаты:
- Подготовил решение к опытной эксплуатации.
Технологии: Python, PostgreSQL."""


def _single_workplace_source() -> str:
    return """Иван Иванов
ivan@example.com
Опыт работы — 2 года
Январь 2024 —
настоящее время
2 года
Компания Альфа
Разработчик
Разрабатывал серверную часть сервиса обработки заказов и отвечал за работу API на Python.
- Реализовал создание и изменение заказов.
Образование
Университет
"""


def _improved(block: ResumeNarrativeBlock, text: str) -> ImprovedResumeBlock:
    return ImprovedResumeBlock(
        index=block.index,
        kind=block.kind,
        label=block.label,
        source_text=block.source_text,
        improved_text=text,
        questions=(),
        answers=(),
    )
