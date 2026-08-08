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
        "Когда вам удобно созвониться?",
        "Пришлите выполненное тестовое задание по ссылке.",
    ),
)
def test_risky_question_is_left_for_explicit_user_action(text: str) -> None:
    assert (
        classify_recruiter_reply(ApplicationState.APPLIED, text)
        is RecruiterReplyDisposition.MANUAL
    )
