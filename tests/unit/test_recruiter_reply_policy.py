# ruff: noqa: RUF001

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hugin.domain.applications import ApplicationState
from hugin.domain.content import MessageDirection, RecruiterActionKind, RecruiterMessageState
from hugin.services.recruiter_reply_policy import (
    RecruiterReplyDisposition,
    classify_recruiter_reply,
    repeated_incoming_already_answered,
    requested_external_action_kind,
    unresolved_action_position_before_invitation_reminder,
)


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    body: str
    direction: MessageDirection
    state: RecruiterMessageState


@pytest.mark.parametrize(
    "text",
    (
        "К сожалению, сейчас мы не готовы пригласить вас на следующий этап.",
        "Мы остановились на другом кандидате. Спасибо за интерес к вакансии.",
        "Рассмотрим ваше резюме. Если оно подойдёт, мы свяжемся с вами.",
        "Благодарю за ответы! Представитель работодателя ознакомится с ними.",
        "Тимур Фанисович, спасибо, что уделили нам время. Успехов!",
        "Мы продолжаем рассматривать кандидатов и вернёмся к вам с обратной связью.",
        "Мы продолжаем рассмотрение кандидатов, поэтому нам потребуется ещё немного времени.",
        "Ваше резюме передано руководителю. Мы сообщим о результате отбора.",
        "В разговоре с кандидатом я узнал основные сведения об опыте.",
        "Напоминаем: ответьте на приглашение работодателя.",
        "ИИ-помощник завершил работу. Если резюме подойдёт, работодатель напишет вам.",
        "Сейчас я беру время на изучение резюме и вернусь, если оно подойдёт.",
        "Всё зафиксировал, спасибо. Передаю информацию работодателю.",
        "Компания рассмотрит ваше резюме и позже сообщит решение.",
        "Ваше резюме находится на этапе рассмотрения.",
        "Thank you, but we will not be moving forward with your application.",
    ),
)
def test_messages_without_required_reply_do_not_reach_model(text: str) -> None:
    assert (
        classify_recruiter_reply(ApplicationState.APPLIED, text)
        is RecruiterReplyDisposition.NO_REPLY
    )


def test_thanks_for_answered_question_does_not_require_reply() -> None:
    assert (
        classify_recruiter_reply(
            ApplicationState.APPLIED,
            "Спасибо за то, что ответили на вопрос",
        )
        is RecruiterReplyDisposition.NO_REPLY
    )


@pytest.mark.parametrize(
    "text",
    (
        "Когда сможете выйти? Спасибо за ответ.",
        "Напоминаем: ответьте на приглашение. Когда сможете приступить?",
    ),
)
def test_question_before_closing_or_after_hh_reminder_still_requires_reply(
    text: str,
) -> None:
    assert (
        classify_recruiter_reply(ApplicationState.APPLIED, text)
        is RecruiterReplyDisposition.AUTOMATIC_DRAFT
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            "Спасибо за ответ. Уточните, когда сможете выйти?",
            RecruiterReplyDisposition.AUTOMATIC_DRAFT,
        ),
        (
            "Спасибо за ответ. Когда вам удобно созвониться?",
            RecruiterReplyDisposition.REVIEW_DRAFT,
        ),
        (
            "Спасибо за ответ. Пришлите выполненное тестовое задание по ссылке.",
            RecruiterReplyDisposition.MANUAL,
        ),
        (
            "Ваше резюме передано руководителю. Мы сообщим о результате. "
            "Уточните, готовы ли вы продолжить?",
            RecruiterReplyDisposition.AUTOMATIC_DRAFT,
        ),
    ),
)
def test_closing_message_does_not_hide_follow_up_request(
    text: str,
    expected: RecruiterReplyDisposition,
) -> None:
    assert classify_recruiter_reply(ApplicationState.APPLIED, text) is expected


@pytest.mark.parametrize(
    "text",
    (
        "Ответьте на приглашение, даже если оно вам не интересно. "
        "Так мы сможем рекомендовать вам более подходящие вакансии. "
        "Отправить ответ можно одной кнопкой:",
        "Напоминаем: ответьте на приглашение работодателя.",
    ),
)
def test_hh_invitation_prompts_remain_without_reply(text: str) -> None:
    assert (
        classify_recruiter_reply(ApplicationState.APPLIED, text)
        is RecruiterReplyDisposition.NO_REPLY
    )


def test_hh_invitation_reminder_does_not_hide_unanswered_question() -> None:
    history = (
        HistoryMessage(
            "Расскажите, пожалуйста, как вы применяли Celery?",
            MessageDirection.INCOMING,
            RecruiterMessageState.RECEIVED,
        ),
        HistoryMessage(
            "Напоминаем: ответьте на приглашение работодателя.",
            MessageDirection.INCOMING,
            RecruiterMessageState.RECEIVED,
        ),
    )

    assert (
        unresolved_action_position_before_invitation_reminder(
            ApplicationState.APPLIED,
            history,
        )
        == 0
    )


def test_hh_invitation_reminder_does_not_reopen_answered_question() -> None:
    history = (
        HistoryMessage(
            "Расскажите, пожалуйста, как вы применяли Celery?",
            MessageDirection.INCOMING,
            RecruiterMessageState.RECEIVED,
        ),
        HistoryMessage(
            "Использовал Celery для фоновых задач.",
            MessageDirection.OUTGOING,
            RecruiterMessageState.SENT,
        ),
        HistoryMessage(
            "Напоминаем: ответьте на приглашение работодателя.",
            MessageDirection.INCOMING,
            RecruiterMessageState.RECEIVED,
        ),
    )

    assert (
        unresolved_action_position_before_invitation_reminder(
            ApplicationState.APPLIED,
            history,
        )
        is None
    )


@pytest.mark.parametrize(
    ("second_answer_state", "latest_question", "expected"),
    (
        (
            RecruiterMessageState.SENT,
            "На каком курсе ты учишься?",
            True,
        ),
        (
            RecruiterMessageState.FAILED,
            "На каком курсе ты учишься?",
            False,
        ),
        (
            RecruiterMessageState.SENT,
            "На каком курсе ты учишься и когда выпуск?",
            False,
        ),
    ),
)
def test_repeated_question_is_closed_only_after_two_sent_answers(
    second_answer_state: RecruiterMessageState,
    latest_question: str,
    expected: bool,
) -> None:
    question = "На каком курсе ты учишься?"
    history = (
        HistoryMessage(question, MessageDirection.INCOMING, RecruiterMessageState.RECEIVED),
        HistoryMessage("На последнем.", MessageDirection.OUTGOING, RecruiterMessageState.SENT),
        HistoryMessage(
            "  НА КАКОМ КУРСЕ ТЫ УЧИШЬСЯ?  ",
            MessageDirection.INCOMING,
            RecruiterMessageState.RECEIVED,
        ),
        HistoryMessage("На последнем.", MessageDirection.OUTGOING, second_answer_state),
        HistoryMessage(
            latest_question,
            MessageDirection.INCOMING,
            RecruiterMessageState.RECEIVED,
        ),
    )

    assert repeated_incoming_already_answered(history) is expected


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
        "Было бы здорово, если бы вы смогли уделить пару минут и ответить на вопросы.",
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
        classify_recruiter_reply(ApplicationState.APPLIED, text) is RecruiterReplyDisposition.MANUAL
    )


def test_short_test_request_is_left_for_explicit_user_action() -> None:
    assert (
        classify_recruiter_reply(
            ApplicationState.APPLIED,
            "Необходимо пройти небольшой тест на нашем сайте.",
        )
        is RecruiterReplyDisposition.MANUAL
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            "Заполните анкету по ссылке https://example.test/form",
            RecruiterActionKind.EXTERNAL_FORM,
        ),
        (
            "Выполните тестовое задание по ссылке https://example.test/task",
            RecruiterActionKind.TEST_ASSIGNMENT,
        ),
        (
            "Пройдите интервью во внешнем помощнике https://example.test/interview",
            RecruiterActionKind.EXTERNAL_ACTION,
        ),
        ("Подробности доступны на https://example.test/info", None),
    ),
)
def test_external_action_kind_is_structured_only_for_explicit_request(
    text: str,
    expected: RecruiterActionKind | None,
) -> None:
    assert requested_external_action_kind(text) is expected


def test_informational_link_alone_does_not_create_external_action() -> None:
    assert (
        classify_recruiter_reply(
            ApplicationState.APPLIED,
            "Подробное описание компании: https://example.test/about",
        )
        is RecruiterReplyDisposition.NO_REPLY
    )


def test_message_without_explicit_request_or_closing_is_ambiguous() -> None:
    assert (
        classify_recruiter_reply(
            ApplicationState.APPLIED,
            "Давайте пока оставим это здесь.",
        )
        is RecruiterReplyDisposition.AMBIGUOUS
    )
