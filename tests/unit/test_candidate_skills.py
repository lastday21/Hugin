from __future__ import annotations

import pytest

from hugin.services.candidate_skills import confirmed_skill_texts


def test_skills_include_explicit_project_stack_but_not_incidental_mentions() -> None:
    facts = (
        ("skills", "Python, SQL"),
        ("technology", "YandexGPT; SpeechKit"),
        ("project", "Hugin: помогает искать работу.\nТехнологии: FastAPI, PostgreSQL, React."),
        ("work_experience", "Заказчик использует Java.\nСтек: Python, SQL\nОклад: 100000."),
        ("experience", "С PyQt и QML не работал, готов изучить."),
        ("project", "Планируется интеграция с Java и Spring."),
    )
    assert confirmed_skill_texts(iter(facts)) == (
        "Python, SQL",
        "YandexGPT",
        "SpeechKit",
        "FastAPI, PostgreSQL, React.",
    )


@pytest.mark.parametrize(
    "content",
    [
        "Java не использовал",
        "Нет опыта с PyQt",
        "Rust пока изучаю",
        "Хочу освоить C++",
        "Готов изучить Qt",
        "No experience with QML",
        "Want to learn Spring",
        "Java: без опыта",
    ],
)
@pytest.mark.parametrize("category", ["skills", "technology", "project", "work_experience"])
def test_negative_or_future_experience_is_not_a_confirmed_skill(
    category: str, content: str
) -> None:
    if category in {"project", "work_experience"}:
        content = f"Технологии: {content}"
    assert confirmed_skill_texts([(category, content)]) == ()
