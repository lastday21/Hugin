# ruff: noqa: RUF001

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from hugin.database.models import VacancyModel
from hugin.services.cover_letter_quality import (
    CoverLetterQuality,
    CoverLetterQualityResponseError,
    QualityFact,
    assess_cover_letter_quality,
    build_quality_correction_prompt,
    build_quality_prompt,
    parse_quality_response,
)


class FakeModel:
    model_name = "quality-test"

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.prompts.append((system_prompt, user_prompt))
        return self.response


def _vacancy() -> VacancyModel:
    return VacancyModel(
        hh_id="quality-1",
        title="Python-разработчик",
        source_url="https://hh.ru/vacancy/quality-1",
        responsibilities="Развивать серверные интеграции.",
        required_qualifications="Python, FastAPI и PostgreSQL.",
        key_skills=["Python", "FastAPI", "PostgreSQL"],
    )


def _response(**changes: object) -> str:
    payload: dict[str, object] = {
        "structure": 3,
        "clarity": 3,
        "individuality": 2,
        "naturalness": 2,
        "hard_failure": None,
        "reasons": ["Есть конкретный пример под основную задачу."],
        "revision_instruction": "Сохранить конкретный пример.",
    }
    payload.update(changes)
    return json.dumps(payload, ensure_ascii=False)


def test_quality_response_passes_only_with_required_score_and_dimensions() -> None:
    passed = parse_quality_response(_response(naturalness=1))
    weak_clarity = parse_quality_response(_response(clarity=1))
    hard_failure = parse_quality_response(_response(hard_failure="Нет нужного примера"))

    assert passed.score == 9
    assert passed.passed
    assert weak_clarity.score == 8
    assert not weak_clarity.passed
    assert not hard_failure.passed


@pytest.mark.parametrize(
    "response",
    [
        "не json",
        _response(structure=4),
        _response(reasons=[]),
        _response(revision_instruction=""),
    ],
)
def test_quality_response_rejects_invalid_contract(response: str) -> None:
    with pytest.raises(CoverLetterQualityResponseError):
        parse_quality_response(response)


def test_quality_assessment_contains_vacancy_facts_and_letter() -> None:
    model = FakeModel(_response())
    facts = (QualityFact(7, "project", "Реализовал интеграцию на FastAPI."),)

    quality = assess_cover_letter_quality(model, _vacancy(), facts, "Текст письма")

    assert quality.score == 10
    assert len(model.prompts) == 1
    system_prompt = model.prompts[0][0]
    assert "Не оценивай пригодность кандидата" in system_prompt
    assert "ответило на прямые вопросы вакансии" in system_prompt
    assert "Отсутствие сведений не означает отсутствие опыта" in system_prompt
    assert "подменяет основную роль несвязанным опытом" in system_prompt
    prompt = model.prompts[0][1]
    assert "Развивать серверные интеграции" in prompt
    assert "Реализовал интеграцию на FastAPI" in prompt
    assert "Текст письма" in prompt


def test_perfect_model_score_cannot_approve_an_unsupported_denial() -> None:
    quality = assess_cover_letter_quality(
        FakeModel(_response()), _vacancy(), (), "Прямого опыта с Flutter у меня пока нет."
    )
    assert not quality.passed
    assert quality.hard_failure is not None
    assert "Отрицание опыта" in quality.hard_failure


def test_confirmed_denial_does_not_override_model_quality() -> None:
    denial = "Прямого опыта с Flutter у меня пока нет."
    quality = assess_cover_letter_quality(
        FakeModel(_response()), _vacancy(), (QualityFact(1, "experience", denial),), denial
    )
    assert quality.passed


def test_quality_prompt_is_valid_json_payload() -> None:
    vacancy = _vacancy()
    vacancy.description = (
        "При отклике приложите 1–2 проекта, где вы работали с Django и PostgreSQL."
    )
    vacancy.employer_name = "Компания"
    vacancy.employment = "Частичная занятость"
    vacancy.work_format = "Удалённо"
    vacancy.salary_from = Decimal(120000)
    vacancy.salary_currency = "RUR"
    vacancy.salary_gross = False
    prompt = build_quality_prompt(
        vacancy,
        (QualityFact(1, "work_experience", "Работал с PostgreSQL."),),
        "Проверяемое письмо",
    )

    payload = json.loads(prompt.split("\n", maxsplit=1)[1])
    assert payload["vacancy"]["title"] == "Python-разработчик"
    assert payload["vacancy"]["description"] == vacancy.description
    assert payload["vacancy"]["employer_name"] == "Компания"
    assert payload["vacancy"]["employment"] == "Частичная занятость"
    assert payload["vacancy"]["work_format"] == "Удалённо"
    assert payload["vacancy"]["salary_from"] == "120000"
    assert payload["vacancy"]["salary_currency"] == "RUR"
    assert payload["vacancy"]["salary_gross"] is False
    assert payload["confirmed_facts"][0]["id"] == 1
    assert payload["letter"] == "Проверяемое письмо"


def test_correction_prompt_keeps_rejected_text_out_of_facts() -> None:
    quality = CoverLetterQuality(
        structure=2,
        clarity=2,
        individuality=1,
        naturalness=2,
        hard_failure=None,
        reasons=("Мало конкретики",),
        revision_instruction="Показать действие и результат",
    )

    prompt = build_quality_correction_prompt("Исходные факты", "Старое письмо", quality)

    assert "7 из 10" in prompt
    assert "Показать действие и результат" in prompt
    assert "Отклонённый текст ниже не является источником фактов" in prompt
    assert "<rejected_letter>\nСтарое письмо\n</rejected_letter>" in prompt
