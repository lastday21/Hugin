from __future__ import annotations

import json
from pathlib import Path

import pytest
from docx import Document

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import CandidateProfileModel, ResumeModel
from hugin.repositories import AccountRepository, ResumeRepository
from hugin.services.resume_improvement import ResumeImprovementService


class RewriteModel:
    model_name = "resume-evidence-test"

    def __init__(self, outputs: tuple[str, ...], *, ask: bool = False) -> None:
        self.outputs = iter(outputs)
        self.ask = ask
        self.rewrite_prompts: list[str] = []

    def complete(self, _system_prompt: str, prompt: str) -> str:
        if "JSON-массив" in prompt:
            return json.dumps(
                [
                    {
                        "topic": topic,
                        "known": not self.ask,
                        "evidence": None if self.ask else "Исходный текст",
                        "question": "Удалось обработать 900 заявок?" if self.ask else None,
                    }
                    for topic in (
                        "personal_contribution",
                        "result",
                        "project_status",
                        "scale",
                        "collaboration",
                    )
                ],
                ensure_ascii=False,
            )
        self.rewrite_prompts.append(prompt)
        return next(self.outputs)


def source_text(narrative: str) -> str:
    return (
        "Опыт работы\nЯнварь 2024 — настоящее время\n"
        "Компания\nРазработчик\n- " + narrative + "\nОбразование\nУниверситет\n"
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("source", "rewrite", "ask", "answer"),
    [
        ("Обработал 100 заявок за месяц", "Обработал 100 заявок за день", False, ""),
        ("Во внутренней выборке получил точность 96%", "Получил точность 96%", False, ""),
        ("Сократил время расчёта до 15 минут", "Сократил время расчёта на 15 минут", False, ""),
        ("Разрабатывал сервис на Python", "Обработал 900 заявок", True, "Объём не измерял"),
        ("Разрабатывал сервис на Python", "Обработал 900 заявок", False, ""),
    ],
)
def test_unconfirmed_result_never_becomes_resume_draft(
    settings: Settings,
    tmp_path: Path,
    source: str,
    rewrite: str,
    ask: bool,
    answer: str,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = RewriteModel((rewrite, rewrite), ask=ask)
    original = source_text(source)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Проверка", "numeric-evidence")
            record = ResumeRepository(session).upsert(account.id, "numeric-source", "Python")
            resume = session.get(ResumeModel, record.id)
            assert resume is not None
            resume.content_text = original
            session.add(
                CandidateProfileModel(
                    account_id=account.id, active_resume_id=resume.id, display_name="Проверка"
                )
            )
            session.flush()

            with pytest.raises(ValueError, match="Число не подтверждено"):
                ResumeImprovementService(session, tmp_path, model).improve(
                    account.id,
                    lambda *_: answer,
                    target_role="Python-разработчик: 900 заявок в день",
                )

            assert resume.content_text == original
            assert len(model.rewrite_prompts) == 2
            assert not list(tmp_path.rglob("*.docx"))
            assert not list(tmp_path.rglob("*.json"))
    finally:
        database.close()


@pytest.mark.integration
@pytest.mark.parametrize("retry", [False, True])
def test_confirmed_answer_supports_result_and_repair_preserves_source(
    settings: Settings,
    tmp_path: Path,
    retry: bool,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    valid = "Результат:\n- За месяц сервис обработал сто заявок."
    model = RewriteModel(("Обработал 900 заявок", valid) if retry else (valid,), ask=True)
    original = source_text("Разрабатывал сервис на Python")
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Проверка", "numeric-confirmed")
            record = ResumeRepository(session).upsert(account.id, "numeric-confirmed", "Python")
            resume = session.get(ResumeModel, record.id)
            assert resume is not None
            resume.content_text = original
            session.add(
                CandidateProfileModel(
                    account_id=account.id, active_resume_id=resume.id, display_name="Проверка"
                )
            )
            session.flush()
            result = ResumeImprovementService(session, tmp_path, model).improve(
                account.id,
                lambda *_: "Обработал 100 заявок за месяц",
            )
            rendered = "\n".join(p.text for p in Document(str(result.draft_path)).paragraphs)
            assert "сто заявок" in rendered
            assert "900" not in rendered
            assert resume.content_text == original
            assert result.source_unchanged
            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            assert report["numeric_validation_version"] == "resume_numeric_v1"
            assert len(model.rewrite_prompts) == (2 if retry else 1)
    finally:
        database.close()
