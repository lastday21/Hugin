from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from hugin.adapters.codex_cli import configured_codex_cli_client
from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.services.role_analyzer import RoleAnalyzer
from hugin.services.semantic_analyzer import StructuredClient
from hugin.services.semantic_cache import DatabaseStageCache
from hugin.services.semantic_results import final_stage, read_selection
from hugin.services.semantic_snapshot import SelectionSnapshot, selection_snapshot


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    status: str
    model_calls: int
    applied: bool
    key: str | None


class _SelectionStopped(Exception):
    pass


class _ControlledClient:
    def __init__(self, client: StructuredClient, allowed: Callable[[], bool]) -> None:
        self._client = client
        self._allowed = allowed
        self.calls = 0

    @property
    def request_identity(self) -> str:
        return self._client.request_identity

    def complete_json(self, system_prompt: str, user_prompt: str, schema: dict[str, object]) -> str:
        if not self._allowed():
            raise _SelectionStopped
        self.calls += 1
        return self._client.complete_json(system_prompt, user_prompt, schema)


class SemanticSelectionProcessor:
    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[str, SelectionSnapshot], StructuredClient] | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory or self._client

    def _client(self, stage: str, snapshot: SelectionSnapshot) -> StructuredClient:
        config = snapshot.config
        return configured_codex_cli_client(
            self._settings,
            operation=f"semantic_{stage}",
            model=config.model,
            timeout_seconds=config.timeout_seconds,
            reasoning_effort=config.reasoning_effort,
        )

    def process(
        self,
        account_id: int,
        direction_id: int,
        vacancy_id: int,
        *,
        max_calls: int = 1,
        allowed: Callable[[], bool] = lambda: True,
    ) -> ProcessingResult:
        from hugin.repositories.directions import DirectionRepository
        from hugin.repositories.vacancies import VacancyRepository
        from hugin.services.vacancy_analysis import VacancyAnalysisService

        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                direction = DirectionRepository(session).get_for_account(account_id, direction_id)
                vacancy = VacancyRepository(session).get(vacancy_id)
                snapshot = selection_snapshot(session, direction, vacancy)
                if snapshot is None:
                    return ProcessingResult("DISABLED", 0, False, None)
                current = read_selection(session, snapshot)
        finally:
            database.close()
        calls = 0
        if current.due:
            if not allowed():
                return ProcessingResult("STOPPED", 0, False, snapshot.key)
            cache = DatabaseStageCache(self._settings, account_id, vacancy_id)
            client = _ControlledClient(self._client_factory("assess", snapshot), allowed)
            analyzer = RoleAnalyzer(
                client,
                cache,
                max_calls=max_calls,
            )
            try:
                result = analyzer.analyze(snapshot.lines, snapshot.facts)
            except _SelectionStopped:
                return ProcessingResult("STOPPED", client.calls, False, snapshot.key)
            calls = result.model_calls
            if not allowed():
                return ProcessingResult("STOPPED", calls, False, snapshot.key)
            if result.budget_exhausted:
                return ProcessingResult("IN_PROGRESS", calls, False, snapshot.key)
            cache.put(final_stage(snapshot, result))

        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                direction = DirectionRepository(session).get_for_account(account_id, direction_id)
                vacancy = VacancyRepository(session).get(vacancy_id)
                latest = selection_snapshot(session, direction, vacancy)
                if latest is None or latest.key != snapshot.key or not direction.is_active:
                    return ProcessingResult("STALE", calls, False, snapshot.key)
                if not allowed():
                    return ProcessingResult("STOPPED", calls, False, snapshot.key)
                ranked = VacancyAnalysisService(session).reanalyze_one(
                    account_id, direction_id, vacancy_id
                )
                return ProcessingResult(ranked.evaluation.category.value, calls, True, snapshot.key)
        finally:
            database.close()
