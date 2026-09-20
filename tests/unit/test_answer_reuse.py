# ruff: noqa: RUF001

import pytest

from hugin.domain.answer_reuse import reusable_question_key


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "Какие у тебя зарплатные ожидания? (укажи вилку до вычета налогов — gross)",
            "Укажите ваши зарплатные ожидания в гросс (до вычета НДФЛ), пожалуйста.",
        ),
        ("Укажите вашу электронную почту", "Ваш email?"),
        ("Какой ваш номер телефона?", "Укажите телефон"),
        ("Ваш город проживания?", "Укажите место жительства"),
    ],
)
def test_simple_rephrased_questions_share_key(first: str, second: str) -> None:
    assert reusable_question_key(first) is not None
    assert reusable_question_key(first) == reusable_question_key(second)


@pytest.mark.parametrize(
    "changed",
    [
        "Ваши зарплатные ожидания на руки?",
        "Ваши зарплатные ожидания gross за час?",
        "Ваши минимальные зарплатные ожидания gross?",
        "Ваши зарплатные ожидания gross в долларах?",
        "Ваши зарплатные ожидания gross при занятости 20 часов?",
        "Ваша текущая зарплата gross?",
        "Какая зарплата gross вас не устроит?",
        "Ваши зарплатные ожидания gross и когда готовы выйти?",
        "Ваши зарплатные ожидания gross на испытательный срок?",
        "Ваши зарплатные ожидания gross или net?",
        "Пришлите документ о зарплате gross на почту до собеседования",
    ],
)
def test_changed_salary_conditions_do_not_share_key(changed: str) -> None:
    assert reusable_question_key(changed) != reusable_question_key(
        "Ваши зарплатные ожидания gross?"
    )


def test_contact_of_another_person_is_not_candidate_contact() -> None:
    assert reusable_question_key("Укажите телефон вашего руководителя") is None
    assert reusable_question_key("Готовы отправить выписку до собеседования?") is None
