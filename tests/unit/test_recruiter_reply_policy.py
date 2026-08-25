# ruff: noqa: RUF001

from __future__ import annotations

import pytest

from hugin.domain.applications import ApplicationState
from hugin.services.recruiter_reply_policy import (
    RecruiterReplyDisposition,
    classify_recruiter_reply,
)


@pytest.mark.parametrize(
    "text",
    (
        "К сожалению, сейчас мы не готовы пригласить вас на следующий этап.",
        "Мы остановились на другом кандидате. Спасибо за интерес к вакансии.",
        "Рассмотрим ваше резюме. Если оно подойдёт, мы свяжемся с вами.",
        "Благодарю за ответы! Представитель работодателя ознакомится с ними.",
        "В разговоре с кандидатом я узнал основные сведения об опыте.",
        "Напоминаем: ответьте на приглашение работодателя.",
        "ИИ-помощник завершил работу. Если резюме подойдёт, работодатель напишет вам.",
        "Сейчас я беру время на изучение резюме и вернусь, если оно подойдёт.",
        "Thank you, but we will not be moving forward with your application.",
    ),
)
def test_messages_without_required_reply_do_not_reach_model(text: str) -> None:
    assert (
        classify_recruiter_reply(ApplicationState.APPLIED, text)
        is RecruiterReplyDisposition.NO_REPLY
    )


@pytest.mark.parametrize(
    "state",
    (ApplicationState.REJECTED, ApplicationState.CLOSED),
)
def test_terminal_application_never_requires_reply(state: ApplicationState) -> None:
    assert (
        classify_recruiter_reply(state, "Расскажите, пожалуйста, о своём опыте?")
        is RecruiterReplyDisposition.NO_REPLY
    )


@pytest.mark.parametrize(
    "text",
    (
        "Расскажите, пожалуйста, как вы применяли Celery и RabbitMQ.",
        "Есть ли у вас опыт работы с PostgreSQL?",
        "Could you describe your Python experience?",
    ),
)
def test_substantive_question_can_get_automatic_draft(text: str) -> None:
    assert (
        classify_recruiter_reply(ApplicationState.VIEWED, text)
        is RecruiterReplyDisposition.AUTOMATIC_DRAFT
    )


@pytest.mark.parametrize(
    "text",
    (
        "Какие у вас зарплатные ожидания?",
        "Какая зарплата вас устроит?",
        "Какой уровень дохода вы рассматриваете?",
        "Укажите желаемую зарплату.",
        "What are your salary expectations?",
    ),
)
def test_simple_salary_expectation_question_can_get_exact_automatic_reply(
    text: str,
) -> None:
    assert (
        classify_recruiter_reply(
            ApplicationState.APPLIED,
            text,
            "Мои зарплатные ожидания — 120 000 рублей на руки.",
        )
        is RecruiterReplyDisposition.AUTOMATIC_DRAFT
    )


@pytest.mark.parametrize(
    "text",
    (
        "Напишите желаемый уровень заработной платы: минимум и комфорт.",
        "Напишите желаемый уровень заработной планы: минимум и комфорт.",
        "Мы предлагаем 100 000 рублей. Вас устроит такая зарплата?",
        "Готовы снизить зарплатные ожидания?",
        "Когда вам удобно созвониться?",
    ),
)
def test_risky_question_gets_a_draft_but_stays_for_review(text: str) -> None:
    assert (
        classify_recruiter_reply(ApplicationState.APPLIED, text)
        is RecruiterReplyDisposition.REVIEW_DRAFT
    )


@pytest.mark.parametrize(
    "text",
    (
        "Пришлите выполненное тестовое задание по ссылке.",
        "Заполните анкету: https://example.test/form",
    ),
)
def test_external_action_is_left_for_explicit_user_action(text: str) -> None:
    assert (
        classify_recruiter_reply(ApplicationState.APPLIED, text)
        is RecruiterReplyDisposition.MANUAL
    )


def test_informational_link_alone_does_not_create_external_action() -> None:
    assert (
        classify_recruiter_reply(
            ApplicationState.APPLIED,
            "Подробное описание компании: https://example.test/about",
        )
        is RecruiterReplyDisposition.NO_REPLY
    )
