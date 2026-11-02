from datetime import UTC, datetime, timedelta

from hugin.domain.applications import ApplicationState
from hugin.services.reapplication_policy import PreviousApplication, decide_reapplication


def test_rolling_limit_and_interval_survive_new_publication_numbers() -> None:
    now = datetime(2026, 10, 10, tzinfo=UTC)
    first = PreviousApplication(1, "old-hh", now - timedelta(days=69), ApplicationState.VIEWED)
    second = PreviousApplication(2, "new-hh", now - timedelta(days=3), ApplicationState.VIEWED)
    decision = decide_reapplication((first, second), now=now)
    assert not decision.allowed
    assert decision.code == "REPOST_LIMIT"
    assert decision.recheck_at == second.confirmed_at + timedelta(days=30)
    assert decide_reapplication((first, second), now=decision.recheck_at).allowed
    assert not decide_reapplication((second,), now=now).allowed
    assert decide_reapplication((), now=now).allowed


def test_rejection_conversation_and_changed_profile_require_review() -> None:
    now = datetime(2026, 10, 10, tzinfo=UTC)
    rejected = PreviousApplication(1, "hh", now - timedelta(days=100), ApplicationState.REJECTED)
    assert decide_reapplication((rejected,), now=now).code == "REPOST_REJECTION"
    invited = PreviousApplication(2, "hh2", now - timedelta(days=100), ApplicationState.INVITED)
    assert decide_reapplication((invited,), now=now).code == "REPOST_CONVERSATION"
    reply = PreviousApplication(
        3,
        "hh3",
        now - timedelta(days=50),
        ApplicationState.VIEWED,
        last_reply_at=now - timedelta(days=1),
    )
    assert decide_reapplication((reply,), now=now).code == "REPOST_CONVERSATION"
    assert (
        decide_reapplication((rejected,), now=now, profile_changed=True).code
        == "REPOST_PROFILE_CHANGED"
    )
