# ruff: noqa: RUF001

from __future__ import annotations

import json

import pytest

from hugin.services.cover_letter_routing import (
    RoutingDecisionKind,
    RoutingResponseError,
    parse_routing_decision,
)


def test_parse_use_decision() -> None:
    response = json.dumps(
        {
            "decision": "USE",
            "candidate_id": 17,
            "confidence": 0.94,
            "reason": "Письмо раскрывает основную задачу",
            "text": None,
        },
        ensure_ascii=False,
    )

    decision = parse_routing_decision(response, frozenset({17, 18}))

    assert decision.kind is RoutingDecisionKind.USE
    assert decision.candidate_id == 17
    assert decision.confidence == pytest.approx(0.94)
    assert decision.text is None


def test_parse_fenced_edit_decision() -> None:
    payload = json.dumps(
        {
            "decision": "EDIT",
            "candidate_id": 18,
            "confidence": 0.82,
            "reason": "Нужно заменить акцент",
            "text": "Здравствуйте!\n\nИсправленный текст.",
        },
        ensure_ascii=False,
    )

    decision = parse_routing_decision(f"```json\n{payload}\n```", frozenset({18}))

    assert decision.kind is RoutingDecisionKind.EDIT
    assert decision.candidate_id == 18
    assert decision.text == "Здравствуйте!\n\nИсправленный текст."


@pytest.mark.parametrize(
    "response, message",
    (
        ("не json", "JSON"),
        (
            '{"decision":"USE","candidate_id":99,"confidence":0.9,"reason":"причина","text":null}',
            "неизвестное письмо",
        ),
        (
            '{"decision":"EDIT","candidate_id":17,"confidence":0.8,"reason":"причина","text":null}',
            "отсутствует исправленный текст",
        ),
        (
            '{"decision":"NEW","candidate_id":17,"confidence":0.8,"reason":"причина","text":null}',
            "Для NEW",
        ),
    ),
)
def test_invalid_router_response_is_rejected(response: str, message: str) -> None:
    with pytest.raises(RoutingResponseError, match=message):
        parse_routing_decision(response, frozenset({17}))
