from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import ApplicationModel, ApplicationTaskModel, IncidentModel
from hugin.domain.content import IncidentSeverity, IncidentState
from hugin.domain.hh import HhApplyResult, HhApplyStatus
from hugin.domain.hh_sync import HhNegotiationData, HhNegotiationStatus
from hugin.domain.tasks import TaskState
from hugin.domain.vacancies import VacancyAvailability
from hugin.repositories import VacancyRepository
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.hh_sync import HhSynchronizationService
from hugin.services.incidents import IncidentService
from tests.unit.test_communications import create_application


@pytest.mark.parametrize("unknown", [False, True])
def test_preparation_recovery_requires_verified_form_and_preserves_unknowns(
    settings: Settings,
    unknown: bool,
) -> None:
    db = create_database(settings)
    try:
        with db.sessions.begin() as session:
            account_id, app_id = create_application(
                session,
                account_label="Восстановление формы",
                vacancy_hh_id="101",
            )
            task = ApplicationTaskModel(
                application_id=app_id,
                state=(TaskState.UNKNOWN_RESULT if unknown else TaskState.REVIEW_REQUIRED),
                priority_score=1,
                scheduled_at=datetime.now(UTC),
                attempts=3,
                last_error_code="RETRY_LIMIT_REACHED",
            )
            session.add(task)
            session.flush()
            incident = IncidentService(session).report(
                code="APPLICATION_RETRY_EXHAUSTED",
                severity=IncidentSeverity.ERROR,
                scope_type="application_task",
                scope_id=task.id,
                message="Форма не открылась",
            )
            service = ApplicationAutomationService(session)
            with pytest.raises(ValueError):
                service.recover_preparation_failure(
                    account_id=account_id,
                    task_id=task.id,
                    preflight=HhApplyResult(
                        HhApplyStatus.MANUAL_REVIEW_REQUIRED,
                        "https://hh.ru/applicant/vacancy_response?vacancyId=999",
                    ),
                )
            verified = HhApplyResult(
                HhApplyStatus.MANUAL_REVIEW_REQUIRED,
                "https://hh.ru/applicant/vacancy_response?vacancyId=101",
                "Форма проверена",
            )
            if unknown:
                with pytest.raises(ValueError):
                    service.recover_preparation_failure(
                        account_id=account_id, task_id=task.id, preflight=verified
                    )
                assert task.state is TaskState.UNKNOWN_RESULT
                assert incident.state is IncidentState.OPEN
            else:
                assert service.recover_preparation_failure(
                    account_id=account_id, task_id=task.id, preflight=verified
                )
                assert task.state is TaskState.RETRY_SCHEDULED and task.attempts == 3
                assert incident.state is IncidentState.RESOLVED
                assert not service.recover_preparation_failure(
                    account_id=account_id,
                    task_id=task.id,
                    preflight=verified,
                )
                assert len(tuple(session.scalars(select(IncidentModel)))) == 1
    finally:
        db.close()


@pytest.mark.parametrize("via_vacancy", [False, True])
def test_verified_vacancy_closure_resolves_its_preparation_error(
    settings: Settings, via_vacancy: bool
) -> None:
    db = create_database(settings)
    try:
        with db.sessions.begin() as session:
            account_id, app_id = create_application(
                session,
                account_label="Закрытая вакансия",
                vacancy_hh_id="101",
            )
            task = ApplicationTaskModel(
                application_id=app_id,
                state=TaskState.REVIEW_REQUIRED,
                priority_score=1,
                scheduled_at=datetime.now(UTC),
                attempts=2,
                last_error_code="RETRY_LIMIT_REACHED",
            )
            session.add(task)
            session.flush()
            incident = IncidentService(session).report(
                code="APPLICATION_RETRY_EXHAUSTED",
                severity=IncidentSeverity.ERROR,
                scope_type="application_task",
                scope_id=task.id,
                message="Форма не открылась",
            )
            if via_vacancy:
                app = session.get(ApplicationModel, app_id)
                assert app is not None
                VacancyRepository(session).mark_unavailable(
                    app.vacancy_id, VacancyAvailability.ARCHIVED
                )
            else:
                HhSynchronizationService(session).synchronize_statuses(
                    account_id=account_id,
                    statuses=(HhNegotiationData("101", HhNegotiationStatus.CLOSED, "Закрыта"),),
                )
            assert task.state is TaskState.SKIPPED
            assert incident.state is IncidentState.RESOLVED
    finally:
        db.close()
