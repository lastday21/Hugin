from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import SemanticStageModel, VacancyChangeModel
from hugin.services.decision_evidence import replay_ranking
from hugin.services.semantic_processing import SemanticSelectionProcessor
from tests.unit.test_semantic_processing import edit_fact, seed

pytestmark = pytest.mark.integration


class RoleClient:
    request_identity = "role-test:medium"

    def __init__(
        self, callback: Callable[[], None] | None = None, *, invalid: bool = False
    ) -> None:
        self.calls = 0
        self.callback = callback
        self.invalid = invalid

    def complete_json(self, system: str, user: str, schema: dict[str, object]) -> str:
        self.calls += 1
        payload = json.loads(user)
        assert "profile" in payload
        assert "extraction" not in payload
        if self.callback is not None:
            self.callback()
        answer: dict[str, Any] = {
            "profession_basis": {
                "kind": "technical_it",
                "source_line_ids": [1],
                "reason": "Основная работа — создание программного API",
            },
            "core_duties": [
                {
                    "task": "Создание API на Python",
                    "source_line_ids": [1],
                    "support": "confirmed",
                    "profile_fact_ids": [payload["profile"]["facts"][0]["id"]],
                    "reason": "Подтверждено проектом",
                }
            ],
            "fit": "direct",
            "profession": "applied_python",
            "role": "Создание API на Python",
            "reason": "Основные задачи подтверждены проектом",
            "source_line_ids": [999 if self.invalid else 1],
            "profile_fact_ids": [payload["profile"]["facts"][0]["id"]],
            "gaps": [],
            "blocker": None,
        }
        return json.dumps(answer)


def test_processor_uses_only_whole_role_client_and_saved_result_replays(settings: Settings) -> None:
    account, direction, vacancy, _resume, fact = seed(settings)
    stages: list[str] = []
    client = RoleClient()

    def factory(stage: str, _snapshot: object) -> RoleClient:
        stages.append(stage)
        assert stage == "assess"
        return client

    processor = SemanticSelectionProcessor(settings, client_factory=factory)
    first = processor.process(account, direction, vacancy)
    assert first.applied and first.status == "MATCH" and first.model_calls == 1
    assert processor.process(account, direction, vacancy).model_calls == 0
    assert client.calls == 1 and stages == ["assess"]
    database = create_database(settings)
    try:
        with database.sessions() as session:
            saved = list(session.scalars(select(SemanticStageModel)))
            assert sorted(stage.stage for stage in saved) == ["assess", "selection"]
            final = next(stage for stage in saved if stage.stage == "selection")
            assert json.loads(final.response_text)["assessment"]["fit"] == "direct"
            for event in session.scalars(select(VacancyChangeModel)):
                if event.event_type == "RULES_EVALUATED":
                    assert replay_ranking(event.changes)["matches"]
        edit_fact(settings, fact)
        changed = processor.process(account, direction, vacancy)
        assert changed.applied and changed.model_calls == 1 and changed.key != first.key
    finally:
        database.close()


@pytest.mark.parametrize("instruction", ["ROLE_REVIEW_INSTRUCTIONS", "ROLE_REPAIR_TASK"])
def test_changed_review_instructions_invalidate_the_saved_selection(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, instruction: str
) -> None:
    from hugin.services import semantic_role, semantic_snapshot

    account, direction, vacancy, _resume, _fact = seed(settings)
    client = RoleClient()
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    first = processor.process(account, direction, vacancy)
    assert first.applied
    monkeypatch.setattr(
        semantic_snapshot,
        instruction,
        getattr(semantic_role, instruction) + " Обновление.",
        raising=False,
    )
    repeated = processor.process(account, direction, vacancy)
    assert repeated.applied and repeated.key != first.key
    assert repeated.model_calls == 0 and client.calls == 1


def test_invalid_whole_role_result_resumes_one_repair_then_keeps_review(settings: Settings) -> None:
    account, direction, vacancy, _resume, _fact = seed(settings)
    client = RoleClient(invalid=True)
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    result = processor.process(account, direction, vacancy)
    assert result.status == "IN_PROGRESS" and not result.applied and result.model_calls == 1
    result = processor.process(account, direction, vacancy)
    assert result.status == "REVIEW" and result.model_calls == 1
    assert processor.process(account, direction, vacancy).model_calls == 0
    assert client.calls == 2
    with create_database(settings).sessions.begin() as session:
        for stage in session.scalars(select(SemanticStageModel)):
            stage.created_at = datetime.now(UTC) - timedelta(hours=1)
    assert processor.process(account, direction, vacancy).model_calls == 0
    assert client.calls == 2


def test_repaired_whole_role_result_does_not_expire_with_initial_validation_error(
    settings: Settings,
) -> None:
    account, direction, vacancy, _resume, _fact = seed(settings)
    client = RoleClient(invalid=True)
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    assert processor.process(account, direction, vacancy).status == "IN_PROGRESS"
    client.invalid = False
    repaired = processor.process(account, direction, vacancy)
    assert repaired.status == "MATCH" and repaired.applied and repaired.model_calls == 1
    with create_database(settings).sessions.begin() as session:
        for stage in session.scalars(select(SemanticStageModel)):
            stage.created_at = datetime.now(UTC) - timedelta(hours=1)
    repeated = processor.process(account, direction, vacancy)
    assert repeated.status == "MATCH" and repeated.model_calls == 0
    assert client.calls == 2


def test_rejection_review_resumes_without_reapplying_the_first_decision(settings: Settings) -> None:
    account, direction, vacancy, _resume, _fact = seed(settings)

    class ReviewClient(RoleClient):
        def complete_json(self, system: str, user: str, schema: dict[str, object]) -> str:
            answer = json.loads(super().complete_json(system, user, schema))
            if "proposed_assessment" not in json.loads(user):
                answer.update(
                    fit="reject",
                    blocker={
                        "source_line_ids": [1],
                        "reason": "Готовый новый артефакт отсутствует",
                    },
                )
            return json.dumps(answer)

    client = ReviewClient()
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    first = processor.process(account, direction, vacancy)
    assert first.status == "IN_PROGRESS" and not first.applied and first.model_calls == 1
    reviewed = processor.process(account, direction, vacancy)
    assert reviewed.status == "MATCH" and reviewed.applied and reviewed.model_calls == 1
    with create_database(settings).sessions.begin() as session:
        saved = list(session.scalars(select(SemanticStageModel)))
        assert sorted(stage.stage for stage in saved) == ["assess", "assess_review", "selection"]
        final = next(stage for stage in saved if stage.stage == "selection")
        assert json.loads(final.response_text)["retryable"] is False
        for stage in saved:
            stage.created_at = datetime.now(UTC) - timedelta(hours=1)
    assert processor.process(account, direction, vacancy).model_calls == 0
    assert client.calls == 2


def test_stop_after_whole_role_call_preserves_cached_answer_for_resume(settings: Settings) -> None:
    account, direction, vacancy, _resume, _fact = seed(settings)
    allowed = True

    def stop() -> None:
        nonlocal allowed
        allowed = False

    client = RoleClient(stop)
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    stopped = processor.process(account, direction, vacancy, allowed=lambda: allowed)
    assert stopped.status == "STOPPED" and not stopped.applied and stopped.model_calls == 1
    allowed = True
    resumed = processor.process(account, direction, vacancy, allowed=lambda: allowed)
    assert resumed.status == "MATCH" and resumed.applied and resumed.model_calls == 0
    assert client.calls == 1


def test_profile_changed_during_whole_role_call_cannot_apply_old_result(settings: Settings) -> None:
    account, direction, vacancy, _resume, fact = seed(settings)
    client = RoleClient(lambda: edit_fact(settings, fact))
    result = SemanticSelectionProcessor(settings, client_factory=lambda *_: client).process(
        account, direction, vacancy
    )
    assert result.status == "STALE" and not result.applied and result.model_calls == 1


def test_stopped_processor_cannot_apply_an_already_saved_assessment(settings: Settings) -> None:
    account, direction, vacancy, _resume, _fact = seed(settings)
    client = RoleClient()
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    assert processor.process(account, direction, vacancy).applied
    stopped = processor.process(account, direction, vacancy, allowed=lambda: False)
    assert stopped.status == "STOPPED" and not stopped.applied and stopped.model_calls == 0
    assert client.calls == 1
