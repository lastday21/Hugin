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
from hugin.diagnostics import OperationJournal
from hugin.domain.vacancies import VacancyAvailability
from hugin.repositories.directions import DirectionRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.automation import PROTECTIVE_SYSTEM_STATES
from hugin.services.semantic_processing import SemanticSelectionProcessor
from hugin.services.semantic_results import read_selection
from hugin.services.semantic_snapshot import selection_snapshot
from hugin.services.vacancy_analysis import MAX_VACANCY_AGE, RULES_VERSION


class SemanticSelectionWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        account_id: int = 1,
        poll_seconds: float = 10,
        journal: OperationJournal | None = None,
        processor: SemanticSelectionProcessor | None = None,
    ) -> None:
        if account_id < 1 or poll_seconds <= 0:
            raise ValueError("Аккаунт и интервал проверки должны быть положительными")
        self._settings = settings
        self._account_id = account_id
        self._poll_seconds = poll_seconds
        self._journal = journal or OperationJournal(settings.data_dir)
        self._processor = processor or SemanticSelectionProcessor(settings)
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
            and options.search_enabled
            and state is not None
            and state.state not in PROTECTIVE_SYSTEM_STATES
        )

    def _next(self, session: Session) -> tuple[int, int] | None:
        if self._stop.is_set() or not self.enabled(session):
            return None
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
                CareerDirectionModel.scoring_config.has_key("semantic_selection"),
                VacancyModel.availability == VacancyAvailability.ACTIVE,
                VacancyModel.details_fetched_at.is_not(None),
                VacancyModel.duplicate_of_id.is_(None),
                or_(
                    VacancyModel.published_at.is_(None),
                    VacancyModel.published_at >= datetime.now(UTC) - MAX_VACANCY_AGE,
                ),
            )
            .order_by(VacancyModel.published_at.desc().nullslast(), VacancyModel.id.desc())
        )
        directions = DirectionRepository(session)
        vacancies = VacancyRepository(session)
        for direction_id, vacancy_id in rows:
            direction = directions.get_for_account(self._account_id, direction_id)
            try:
                snapshot = selection_snapshot(session, direction, vacancies.get(vacancy_id))
            except ValueError:
                continue
            if snapshot is None:
                continue
            result = read_selection(session, snapshot)
            tracked = directions.get_tracked_vacancy(direction_id, vacancy_id)
            evidence = tracked.rules_details.get("semantic_selection")
            if (
                result.due
                or tracked.rules_version != RULES_VERSION
                or not isinstance(evidence, dict)
                or evidence.get("key") != snapshot.key
            ):
                return direction_id, vacancy_id
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
            result = self._processor.process(self._account_id, direction_id, vacancy_id)
            if result.applied and not self._stop.is_set():
                database = create_database(self._settings)
                try:
                    with database.sessions.begin() as session:
                        if self.enabled(session):
                            direction = DirectionRepository(session).get_for_account(
                                self._account_id, direction_id
                            )
                            automation = ApplicationAutomationService(session)
                            automation.prepare_for_account_id(
                                account_id=self._account_id,
                                direction_name=direction.name,
                                include_stretch=automation.stretch_automation_enabled(),
                            )
                finally:
                    database.close()
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

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                worked = False
            self._stop.wait(1 if worked else self._poll_seconds)
