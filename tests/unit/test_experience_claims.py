# ruff: noqa: RUF001

import pytest

from hugin.services.experience_claims import unsupported_experience_denial


@pytest.mark.parametrize(
    "claim",
    [
        "Прямого опыта с Flutter у меня пока нет.",
        "У меня нет опыта в fintech.",
        "С Google Sheets API я не работал.",
        "Не использовал PI в проектах.",
        "Коммерческий опыт с Java отсутствует.",
    ],
)
def test_unknown_experience_does_not_support_a_denial(claim: str) -> None:
    assert unsupported_experience_denial(claim, ("Разрабатывал сервисы на Python.",)) == claim


def test_confirmed_denial_can_be_repeated() -> None:
    claim = "Прямого опыта с Airflow у меня пока нет."
    assert unsupported_experience_denial(claim, (claim,)) is None


def test_other_negative_fact_does_not_confirm_denial() -> None:
    claim = "Прямого опыта с Flutter у меня пока нет."
    facts = ("Прямого опыта с Airflow у меня пока нет. Разрабатывал приложение на Flutter.",)
    assert unsupported_experience_denial(claim, facts) == claim


def test_commercial_denial_does_not_mean_no_experience() -> None:
    claim = "Опыта с Java нет."
    assert unsupported_experience_denial(claim, ("Коммерческого опыта с Java нет.",)) == claim


def test_completed_negative_fact_does_not_confirm_current_denial() -> None:
    claim = "Опыта с Java нет."
    assert (
        unsupported_experience_denial(claim, ("Раньше опыта с Java нет, теперь работаю.",)) == claim
    )


def test_unrelated_negation_and_positive_experience_are_allowed() -> None:
    text = "Работал с Python. Не терял данные. Проверял, что ошибок нет."
    assert unsupported_experience_denial(text, ()) is None


def test_denial_is_checked_separately_from_other_sentences() -> None:
    denial = "С Flutter я не работал."
    text = f"Работал с Python. {denial} Готов обсудить задачи."
    assert unsupported_experience_denial(text, ()) == denial
