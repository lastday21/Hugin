from __future__ import annotations

import threading
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    ApplicationSettingsModel,
    CareerDirectionModel,
    DirectionVacancyModel,
    HhAccountModel,
    SystemStateModel,
    VacancyModel,
)
from hugin.diagnostics import OperationJournal, operation_context
from hugin.domain.vacancies import VacancyAvailability
from hugin.repositories.directions import DirectionRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.automation import PROTECTIVE_SYSTEM_STATES
from hugin.services.background_processes import BackgroundProcessService
from hugin.services.semantic_processing import ProcessingResult, SemanticSelectionProcessor
from hugin.services.semantic_results import read_selection
from hugin.services.semantic_snapshot import selection_config, selection_snapshot
from hugin.services.vacancy_analysis import MAX_VACANCY_AGE, RULES_VERSION, VacancyAnalysisService


class SemanticSelectionWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        account_id: int = 1,
        poll_seconds: float = 10,
        journal: OperationJournal | None = None,
        processor: SemanticSelectionProcessor | None = None,
        max_calls_per_turn: int = 6,
    ) -> None:
        if account_id < 1 or poll_seconds <= 0:
            raise ValueError("Аккаунт и интервал проверки должны быть положительными")
        self._settings = settings
        self._account_id = account_id
        self._poll_seconds = poll_seconds
        self._journal = journal or OperationJournal(settings.data_dir)
        self._processor = processor or SemanticSelectionProcessor(settings)
        self._max_calls_per_turn = max_calls_per_turn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        upgrade_database(self._settings)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="hugin-semantic-selection", daemon=True
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 10) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_seconds)
            if not self._thread.is_alive():
                self._thread = None

    @staticmethod
    def enabled(session: Session) -> bool:
        options = session.get(ApplicationSettingsModel, 1)
        state = session.get(SystemStateModel, 1)
        return bool(
            options is not None
            and options.evaluation_enabled
            and state is not None
            and state.state not in PROTECTIVE_SYSTEM_STATES
        )

    def _next(self, session: Session) -> tuple[int, int] | None:
        if (
            self._stop.is_set()
            or not self.enabled(session)
            or session.get(HhAccountModel, self._account_id) is None
        ):
            return None
        runtime = BackgroundProcessService(session, self._account_id)._runtime("evaluation")
        rows = session.execute(
            select(DirectionVacancyModel.direction_id, VacancyModel.id)
            .join(VacancyModel, VacancyModel.id == DirectionVacancyModel.vacancy_id)
            .join(
                CareerDirectionModel, CareerDirectionModel.id == DirectionVacancyModel.direction_id
            )
            .join(HhAccountModel, HhAccountModel.id == CareerDirectionModel.account_id)
            .where(
                CareerDirectionModel.account_id == self._account_id,
                CareerDirectionModel.is_active.is_(True),
                HhAccountModel.is_active.is_(True),
                VacancyModel.availability == VacancyAvailability.ACTIVE,
                VacancyModel.details_fetched_at.is_not(None),
                VacancyModel.duplicate_of_id.is_(None),
                or_(
                    VacancyModel.published_at.is_(None),
                    VacancyModel.published_at >= datetime.now(UTC) - MAX_VACANCY_AGE,
                ),
            )
            .order_by(
                (VacancyModel.id == runtime.cursor_vacancy_id).desc(),
                DirectionVacancyModel.analyzed_at.asc().nullsfirst(),
                VacancyModel.details_fetched_at,
                VacancyModel.id,
                DirectionVacancyModel.direction_id,
            )
        )
        directions = DirectionRepository(session)
        vacancies = VacancyRepository(session)
        for direction_id, vacancy_id in rows:
            direction = directions.get_for_account(self._account_id, direction_id)
            tracked = directions.get_tracked_vacancy(direction_id, vacancy_id)
            try:
                snapshot = selection_snapshot(session, direction, vacancies.get(vacancy_id))
            except ValueError:
                continue
            if snapshot is None:
                if (
                    tracked.rules_version != RULES_VERSION
                    or "semantic_selection" in tracked.rules_details
                ):
                    runtime.cursor_vacancy_id = vacancy_id
                    return direction_id, vacancy_id
                continue
            result = read_selection(session, snapshot)
            evidence = tracked.rules_details.get("semantic_selection")
            if (
                result.due
                or tracked.rules_version != RULES_VERSION
                or not isinstance(evidence, dict)
                or evidence.get("key") != snapshot.key
            ):
                runtime.cursor_vacancy_id = vacancy_id
                return direction_id, vacancy_id
        runtime.cursor_vacancy_id = None
        return None

    def run_once(self) -> bool:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                selected = self._next(session)
        finally:
            database.close()
        if selected is None or self._stop.is_set():
            return False
        direction_id, vacancy_id = selected
        run = self._journal.start(
            "semantic_selection",
            "analyze",
            account_id=self._account_id,
            direction_id=direction_id,
            vacancy_id=vacancy_id,
        )
        try:
            with operation_context(
                account_id=self._account_id,
                direction_id=direction_id,
                vacancy_id=vacancy_id,
                parent_run_id=run.run_id,
            ):
                result = self._evaluate_by_rules(direction_id, vacancy_id)
                if result is None:
                    result = self._processor.process(
                        self._account_id,
                        direction_id,
                        vacancy_id,
                        max_calls=self._max_calls_per_turn,
                        allowed=self._allowed,
                    )
            run.succeed(
                result_status=result.status,
                model_calls=result.model_calls,
                applied=result.applied,
                selection_key=result.key,
            )
        except Exception as error:
            run.fail(error)
            raise
        return True

    def _evaluate_by_rules(self, direction_id: int, vacancy_id: int) -> ProcessingResult | None:
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                if not self._allowed():
                    return ProcessingResult("STOPPED", 0, False, None)
                direction = DirectionRepository(session).get_for_account(
                    self._account_id, direction_id
                )
                if not direction.is_active:
                    return ProcessingResult("STALE", 0, False, None)
                if selection_config(direction) is not None:
                    return None
                ranked = VacancyAnalysisService(session).reanalyze_one(
                    self._account_id, direction_id, vacancy_id
                )
                return ProcessingResult(ranked.evaluation.category.value, 0, True, None)
        finally:
            database.close()

    def _allowed(self) -> bool:
        if self._stop.is_set():
            return False
        database = create_database(self._settings)
        try:
            with database.sessions() as session:
                return BackgroundProcessService(session, self._account_id).enabled("evaluation")
        finally:
            database.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                worked = False
            self._stop.wait(1 if worked else self._poll_seconds)
