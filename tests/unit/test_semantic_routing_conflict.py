from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    CareerDirectionModel,
    SemanticStageModel,
    VacancyChangeModel,
    VerifiedFactModel,
)
from hugin.domain.directions import DirectionScope, VacancyState
from hugin.repositories.directions import DirectionRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.decision_evidence import fingerprint, replay_ranking
from hugin.services.semantic_analyzer import MemoryStageCache, SemanticAnalyzer
from hugin.services.semantic_cache import DatabaseStageCache
from hugin.services.semantic_processing import SemanticSelectionProcessor
from hugin.services.semantic_results import final_stage, read_selection
from hugin.services.semantic_snapshot import selection_snapshot
from hugin.services.vacancy_analysis import RULES_VERSION, RuleCategory, VacancyAnalysisService
from tests.unit.test_semantic_processing import Client, seed

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class RoutingCase:
    account_id: int
    direction_ids: tuple[int, int]
    vacancy_id: int
    target_fact_id: int


class StoredResponseClient(Client):
    def __init__(self, profession: str) -> None:
        super().__init__()
        self.profession = profession

    def complete_json(self, system: str, user: str, schema: dict[str, object]) -> str:
        answer = json.loads(super().complete_json(system, user, schema))
        if "paths" in answer:
            answer["paths"][0]["profession"] = self.profession
        return json.dumps(answer)


class NoCallsClient:
    request_identity = Client.request_identity

    def complete_json(self, system: str, user: str, schema: dict[str, object]) -> str:
        pytest.fail("Сохранённые ступени не должны повторно обращаться к модели")


def routing_case(settings: Settings) -> RoutingCase:
    account, python_direction, vacancy, resume, fact_id = seed(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            directions = DirectionRepository(session)
            adjacent = directions.create(
                account, "ИТ", scoring_config={"semantic_selection": {"enabled": True}}
            )
            directions.attach_resume(adjacent.id, resume)
            directions.track_vacancy(adjacent.id, vacancy)
            fact = session.get(VerifiedFactModel, fact_id)
            assert fact is not None
            extra = VerifiedFactModel(
                profile_id=fact.profile_id,
                category=fact.category,
                source_type=fact.source_type,
                content="Настраивал приложения и базы данных",
                resume_id=resume,
                direction_id=adjacent.id,
                state=fact.state,
            )
            session.add(extra)
            session.flush()
            return RoutingCase(account, (python_direction, adjacent.id), vacancy, extra.id)
    finally:
        database.close()


def save_response(settings: Settings, case: RoutingCase, index: int, profession: str) -> str:
    database = create_database(settings)
    try:
        with database.sessions() as session:
            direction = DirectionRepository(session).get_for_account(
                case.account_id, case.direction_ids[index]
            )
            snapshot = selection_snapshot(
                session, direction, VacancyRepository(session).get(case.vacancy_id)
            )
            assert snapshot is not None
    finally:
        database.close()
    client = StoredResponseClient(profession)
    result = SemanticAnalyzer(client, client, MemoryStageCache()).analyze(
        snapshot.lines, snapshot.facts
    )
    assert result.decision.status == "ALLOW"
    cache = DatabaseStageCache(settings, case.account_id, case.vacancy_id)
    for stage in (*result.stages, final_stage(snapshot, result)):
        cache.put(stage)
    return snapshot.key


def raw_records(
    settings: Settings, *, only_model_stages: bool = False
) -> tuple[tuple[object, ...], ...]:
    database = create_database(settings)
    try:
        with database.sessions() as session:
            query = select(SemanticStageModel).order_by(SemanticStageModel.id)
            if only_model_stages:
                query = query.where(SemanticStageModel.stage != "selection")
            return tuple(
                (
                    row.id,
                    row.cache_key,
                    fingerprint(row.request),
                    row.response_text,
                    row.response_sha256,
                    tuple(row.errors),
                )
                for row in session.scalars(query)
            )
    finally:
        database.close()


def apply_direction(settings: Settings, case: RoutingCase, index: int) -> RuleCategory:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            return (
                VacancyAnalysisService(session)
                .reanalyze_one(case.account_id, case.direction_ids[index], case.vacancy_id)
                .evaluation.category
            )
    finally:
        database.close()


def assert_conflict(settings: Settings, case: RoutingCase, keys: dict[int, str]) -> None:
    database = create_database(settings)
    try:
        with database.sessions() as session:
            directions = DirectionRepository(session)
            vacancy = VacancyRepository(session).get(case.vacancy_id)
            for index, direction_id in enumerate(case.direction_ids):
                tracked = directions.get_tracked_vacancy(direction_id, case.vacancy_id)
                assert tracked.rules_details["category"] == "REVIEW"
                assert tracked.rules_details["accepted"] is False
                assert tracked.rules_details["target_scope"] is None
                semantic = tracked.rules_details["semantic_selection"]
                assert isinstance(semantic, dict)
                assert semantic["status"] == "ALLOW"
                assert semantic["key"] == keys[index]
                assert semantic["routing_conflict"]
                reasons = tracked.rules_details["reasons"]
                assert isinstance(reasons, list)
                assert any("противоречат" in str(reason) for reason in reasons)
                snapshot = selection_snapshot(
                    session, directions.get_for_account(case.account_id, direction_id), vacancy
                )
                assert snapshot is not None
                selection = read_selection(session, snapshot)
                assert selection.decision.status == "ALLOW"
                assert selection.target_scope == (
                    DirectionScope.IT_ADJACENT if index == 0 else DirectionScope.PYTHON_BACKEND
                )
                evidence_id = tracked.rules_details["evidence_id"]
                assert isinstance(evidence_id, int)
                evidence = session.get(VacancyChangeModel, evidence_id)
                assert evidence is not None
                assert replay_ranking(evidence.changes)["matches"] is True
                applied = evidence.changes["applied"]
                assert isinstance(applied, dict)
                evaluation = applied["evaluation"]
                assert isinstance(evaluation, dict) and evaluation["category"] == "REVIEW"
    finally:
        database.close()


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_mutual_routes_need_review_in_both_orders_without_changing_saved_responses(
    settings: Settings, order: tuple[int, int]
) -> None:
    case = routing_case(settings)
    professions = ("adjacent_it", "applied_python")
    first, second = order
    keys = {first: save_response(settings, case, first, professions[first])}
    assert apply_direction(settings, case, first) is RuleCategory.ROUTED
    keys[second] = save_response(settings, case, second, professions[second])
    saved = raw_records(settings)
    assert apply_direction(settings, case, second) is RuleCategory.REVIEW
    assert_conflict(settings, case, keys)
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: NoCallsClient())
    for index in order:
        assert (
            processor.process(
                case.account_id, case.direction_ids[index], case.vacancy_id
            ).model_calls
            == 0
        )
    assert_conflict(settings, case, keys)
    assert raw_records(settings) == saved


@pytest.mark.parametrize("change", ["stale", "inactive", "one_way"])
def test_stale_inactive_and_one_way_routes_are_not_conflicts(
    settings: Settings, change: str
) -> None:
    case = routing_case(settings)
    save_response(settings, case, 0, "adjacent_it")
    save_response(settings, case, 1, "adjacent_it" if change == "one_way" else "applied_python")
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            if change == "stale":
                fact = session.get(VerifiedFactModel, case.target_fact_id)
                assert fact is not None
                fact.content = "Новый подтверждённый проект"
            if change == "inactive":
                direction = session.get(CareerDirectionModel, case.direction_ids[1])
                assert direction is not None
                direction.is_active = False
        expected = RuleCategory.STRETCH if change == "inactive" else RuleCategory.ROUTED
        assert apply_direction(settings, case, 0) is expected
        with database.sessions() as session:
            tracked = DirectionRepository(session).get_tracked_vacancy(
                case.direction_ids[0], case.vacancy_id
            )
            semantic = tracked.rules_details["semantic_selection"]
            assert isinstance(semantic, dict) and "routing_conflict" not in semantic
            if change == "one_way":
                target = DirectionRepository(session).get_tracked_vacancy(
                    case.direction_ids[1], case.vacancy_id
                )
                assert target.rules_details["category"] == "STRETCH"
    finally:
        database.close()


def test_new_rules_reapply_old_routes_from_saved_model_stages(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hugin.services.vacancy_analysis as analysis

    assert RULES_VERSION == "python_it_v71"
    case = routing_case(settings)
    with monkeypatch.context() as previous_version:
        previous_version.setattr(analysis, "RULES_VERSION", "python_it_v70")
        old_keys = {
            0: save_response(settings, case, 0, "adjacent_it"),
            1: save_response(settings, case, 1, "applied_python"),
        }
        database = create_database(settings)
        try:
            with database.sessions.begin() as session:
                for index, direction_id in enumerate(case.direction_ids):
                    DirectionRepository(session).apply_rules(
                        direction_id,
                        case.vacancy_id,
                        state=VacancyState.SKIPPED,
                        score=80,
                        details={
                            "category": "ROUTED",
                            "semantic_selection": {
                                "key": old_keys[index],
                                "status": "ALLOW",
                            },
                        },
                        rules_version="python_it_v70",
                    )
        finally:
            database.close()
    saved_model_stages = raw_records(settings, only_model_stages=True)
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: NoCallsClient())
    keys = {}
    for index, direction_id in enumerate(case.direction_ids):
        result = processor.process(case.account_id, direction_id, case.vacancy_id, max_calls=1)
        assert result.model_calls == 0 and result.applied
        assert result.key is not None and result.key != old_keys[index]
        keys[index] = result.key
    assert_conflict(settings, case, keys)
    assert raw_records(settings, only_model_stages=True) == saved_model_stages
