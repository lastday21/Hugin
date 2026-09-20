from datetime import UTC, datetime, timedelta

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import ApplicationSettingsModel, DirectionSearchQueryModel, VacancyModel
from hugin.domain.automation import AutomationJobKind
from hugin.domain.directions import VacancyState
from hugin.domain.vacancies import VacancyAvailability, VacancyData
from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.directions import DirectionRepository
from hugin.repositories.tasks import QueueTaskRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.application_selection_gate import ApplicationSelectionGate
from hugin.services.automation import AutomationSchedulerService
from hugin.services.semantic_processing import SemanticSelectionProcessor
from hugin.services.vacancy_analysis import RULES_VERSION
from tests.unit.test_application_automation_boundaries import queued_jobs
from tests.unit.test_automation_scheduler import seed_search_query
from tests.unit.test_semantic_processing import Client, edit_fact, seed


def test_later_stronger_vacancy_is_evaluated_before_spending_ready_slot(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            old = queued_jobs(session, count=1)[0]
            account_id = old.application.account_id
            direction_id = old.application.direction_id
            assert direction_id is not None
            directions = DirectionRepository(session)
            late = VacancyRepository(session).upsert(
                VacancyData("late-strong", "Python developer", "https://hh.ru/vacancy/late-strong")
            )
            directions.track_vacancy(direction_id, late.id)
            service = ApplicationAutomationService(session)
            assert service.claim_next(account_id=account_id) is None
            assert QueueTaskRepository(session).get(old.task.id).attempts == 0
            row = session.get(VacancyModel, late.id)
            assert row is not None
            row.details_fetched_at = datetime.now(UTC)
            session.flush()
            assert service.claim_next(account_id=account_id) is None
            directions.apply_rules(
                direction_id,
                late.id,
                state=VacancyState.QUEUED,
                score=99,
                details={"category": "MATCH", "accepted": True, "fit_tier": 1},
                rules_version=RULES_VERSION,
            )
            application = ApplicationRepository(session).create_apply_intent(
                account_id, late.id, old.resume.id, direction_id
            )
            QueueTaskRepository(session).enqueue(application.id, 99)
            chosen = service.claim_next(account_id=account_id)
            assert chosen is not None and chosen.vacancy.id == late.id
            assert QueueTaskRepository(session).get(old.task.id).attempts == 0
    finally:
        database.close()


def test_search_round_must_complete_today_with_current_query(settings: Settings) -> None:
    account_id, query_id = seed_search_query(settings)
    database = create_database(settings)
    now = datetime(2026, 9, 20, 9, tzinfo=UTC)
    try:
        with database.sessions.begin() as session:
            options = session.get(ApplicationSettingsModel, 1)
            assert options is not None
            options.resource_saving_mode = False
            session.flush()
            gate = ApplicationSelectionGate(session)
            scheduler = AutomationSchedulerService(session)
            scheduler.ensure_search_job(
                account_id=account_id, search_query_id=query_id, interval_minutes=120, now=now
            )
            assert gate.blocking_reason(account_id, now) is not None
            job = scheduler.claim_due(now, allowed_kinds=(AutomationJobKind.SEARCH,))
            assert job is not None
            scheduler.complete(job.key, {"continuation": True}, now)
            assert gate.blocking_reason(account_id, now) is not None
            later = now + timedelta(seconds=16)
            assert scheduler.claim_due(later, allowed_kinds=(AutomationJobKind.SEARCH,)) is not None
            scheduler.complete(job.key, {"continuation": False}, later)
            assert gate.blocking_reason(account_id, later) is None
            later += timedelta(hours=3)
            assert scheduler.claim_due(later, allowed_kinds=(AutomationJobKind.SEARCH,)) is not None
            scheduler.complete(job.key, {"continuation": True}, later)
            assert gate.blocking_reason(account_id, later) is None
            query = session.get(DirectionSearchQueryModel, query_id)
            assert query is not None
            query.query = "Другой запрос"
            session.flush()
            assert gate.blocking_reason(account_id, later) is not None
            later += timedelta(seconds=16)
            assert scheduler.claim_due(later, allowed_kinds=(AutomationJobKind.SEARCH,)) is not None
            query.query = "Запрос изменён во время поиска"
            session.flush()
            scheduler.complete(job.key, {"continuation": False}, later)
            assert gate.blocking_reason(account_id, later) is not None
            later += timedelta(hours=3)
            assert scheduler.claim_due(later, allowed_kinds=(AutomationJobKind.SEARCH,)) is not None
            scheduler.complete(job.key, {"continuation": False}, later)
            assert gate.blocking_reason(account_id, later) is None
            assert gate.blocking_reason(account_id, later + timedelta(days=1)) is not None
            query.is_active = False
            session.flush()
            assert gate.blocking_reason(account_id, later + timedelta(days=1)) is None
    finally:
        database.close()


def test_all_fresh_vacancies_need_details_and_current_evaluation(settings: Settings) -> None:
    account, direction, vacancy, _, fact = seed(settings)
    database = create_database(settings)
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: Client())
    try:
        with database.sessions() as session:
            assert ApplicationSelectionGate(session).blocking_reason(account) is not None
        processor.process(account, direction, vacancy)
        with database.sessions.begin() as session:
            assert ApplicationSelectionGate(session).blocking_reason(account) is None
            pending = VacancyRepository(session).upsert(
                VacancyData("late", "Разработчик", "https://hh.ru/vacancy/late")
            )
            DirectionRepository(session).track_vacancy(direction, pending.id)
        with database.sessions.begin() as session:
            gate = ApplicationSelectionGate(session)
            assert "загрузки" in (gate.blocking_reason(account) or "")
            row = session.get(VacancyModel, pending.id)
            assert row is not None
            row.availability = VacancyAvailability.ARCHIVED
            session.flush()
            assert gate.blocking_reason(account) is None
            row.availability = VacancyAvailability.ACTIVE
            row.description = "Создавать API на Python"
            row.details_fetched_at = datetime.now(UTC)
            session.flush()
            assert "оценки" in (gate.blocking_reason(account) or "")
        processor.process(account, direction, pending.id)
        with database.sessions() as session:
            assert ApplicationSelectionGate(session).blocking_reason(account) is None
        edit_fact(settings, fact)
        with database.sessions() as session:
            assert "оценки" in (ApplicationSelectionGate(session).blocking_reason(account) or "")
    finally:
        database.close()
