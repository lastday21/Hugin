# ruff: noqa: RUF001

from __future__ import annotations

import pytest

from hugin.services.autonomy import DEFAULT_AUTONOMY_POLICY, parse_autonomy_policy


def test_default_autonomy_policy_enables_safe_automatic_actions() -> None:
    policy = parse_autonomy_policy(DEFAULT_AUTONOMY_POLICY, revision=1)

    assert policy.auto_apply_stretch is True
    assert policy.auto_submit_simple_forms is True
    assert policy.auto_prepare_replies is True
    assert policy.auto_send_approved_replies is True
    assert policy.auto_reconcile_unknown is True
    assert policy.reuse_confirmed_profile_facts is True
    assert policy.mark_opened_invitations_seen is True
    assert policy.mutable_fact_validity_days == 30
    assert policy.reply_templates == ()


def test_legacy_autonomy_policy_gets_conservative_fact_validity() -> None:
    legacy = dict(DEFAULT_AUTONOMY_POLICY)
    legacy.pop("mutable_fact_validity_days")

    policy = parse_autonomy_policy(legacy, revision=4)

    assert policy.revision == 4
    assert policy.mutable_fact_validity_days == 30


def test_approved_reply_requires_an_exact_normalized_match() -> None:
    policy = parse_autonomy_policy(
        {
            **DEFAULT_AUTONOMY_POLICY,
            "reply_templates": [
                {
                    "key": "interest",
                    "incoming_text": "Предложение ещё актуально?",
                    "response_text": "Здравствуйте! Да, готов обсудить детали.",
                    "enabled": True,
                }
            ],
        },
        revision=3,
    )

    matched = policy.matching_reply_template("  ПРЕДЛОЖЕНИЕ\u00a0ещё актуально? ")

    assert matched is not None
    assert matched.key == "interest"
    assert policy.matching_reply_template("Предложение актуально?") is None


def test_approved_reply_rejects_two_answers_for_the_same_incoming_text() -> None:
    with pytest.raises(ValueError, match="Один текст работодателя"):
        parse_autonomy_policy(
            {
                **DEFAULT_AUTONOMY_POLICY,
                "reply_templates": [
                    {
                        "key": "first",
                        "incoming_text": "Вам интересно?",
                        "response_text": "Да.",
                        "enabled": True,
                    },
                    {
                        "key": "second",
                        "incoming_text": " вам   интересно? ",
                        "response_text": "Интересно.",
                        "enabled": True,
                    },
                ],
            },
            revision=1,
        )


@pytest.mark.parametrize("value", [0, 366, True, 30.5, "30"])
def test_mutable_fact_validity_days_is_bounded(value: object) -> None:
    with pytest.raises(ValueError, match="mutable_fact_validity_days"):
        parse_autonomy_policy(
            {
                **DEFAULT_AUTONOMY_POLICY,
                "mutable_fact_validity_days": value,
            },
            revision=1,
        )


def test_mutable_fact_validity_days_is_part_of_versioned_policy() -> None:
    policy = parse_autonomy_policy(
        {
            **DEFAULT_AUTONOMY_POLICY,
            "mutable_fact_validity_days": 45,
        },
        revision=7,
    )

    assert policy.revision == 7
    assert policy.mutable_fact_validity_days == 45
    assert policy.as_payload()["mutable_fact_validity_days"] == 45
