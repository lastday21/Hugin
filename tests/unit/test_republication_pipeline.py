from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import CareerDirectionModel, CoverLetterModel, VacancyModel
from hugin.domain import HhApplyResult, HhApplyStatus, SystemState, VacancyAvailability
from hugin.repositories import DirectionRepository, SystemStateRepository, VacancyRepository
from hugin.services.application_automation import ApplicationAutomationService, ApplyJob
from hugin.services.cover_letter import CoverLetterService
from hugin.services.semantic_processing import SemanticSelectionProcessor
from hugin.services.vacancy_analysis import VacancyAnalysisService
from tests.unit.test_cover_letter import FakeModel, _letter, _prepare_data, _quality_response
from tests.unit.test_semantic_processing import Client

pytestmark = pytest.mark.integration


def _submission_allowed(service: ApplicationAutomationService, job: ApplyJob) -> bool:
    assert job.cover_letter_id is not None and job.cover_letter_sha256 is not None
    return service.background_submission_is_allowed(
        job.task.id,
        letter_id=job.cover_letter_id,
        letter_sha256=job.cover_letter_sha256,
        resume_hh_id=job.resume.hh_id,
        resume_title=job.resume.title,
    )


@pytest.mark.parametrize("blank_lines", [False, True])
def test_republication_keeps_models_cached_and_submits_each_number_once(
    settings: Settings, blank_lines: bool
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, direction_id, _, vacancy_ids = _prepare_data(session)
            direction = session.get(CareerDirectionModel, direction_id)
            assert direction is not None
            direction.scoring_config = {"semantic_selection": {"enabled": True}}
            vacancy = session.get(VacancyModel, vacancy_ids[0])
            assert vacancy is not None
            vacancy.published_at = datetime.now(UTC)
            vacancy.details_fetched_at = datetime.now(UTC)
        client = Client()
        processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
        original_id = vacancy_ids[0]
        first = processor.process(account_id, direction_id, original_id)
        assert first.applied and first.model_calls == 1
        writer = FakeModel([_letter()])
        judge = FakeModel([_quality_response()])
        recorded_numbers = []
        with database.sessions.begin() as session:
            service = ApplicationAutomationService(session)
            service.prepare_for_account_id(
                account_id=account_id, direction_name="Python backend", include_stretch=True
            )
            letters = CoverLetterService(session, writer, quality_model=judge)
            assert (
                letters.prepare(account_id=account_id, direction_name="Python backend").generated
                == 1
            )
            SystemStateRepository(session).transition(SystemState.RUNNING)
            job = service.claim_next(
                direction_id, require_cover_letter=True, require_cover_letter_quality=True
            )
            assert job is not None and job.vacancy.id == original_id
            assert _submission_allowed(service, job)
            recorded_numbers.append(job.vacancy.hh_id)
            assert service.record_result(
                job,
                HhApplyResult(
                    HhApplyStatus.APPLIED, job.vacancy.source_url, "Контрольный результат"
                ),
                apply_delay=timedelta(0),
            ).sent
            original = session.get(VacancyModel, original_id)
            assert original is not None
            original.availability = VacancyAvailability.ARCHIVED
            vacancies = VacancyRepository(session)
            data = VacancyAnalysisService._data(vacancies.get(original_id))
            fresh = vacancies.upsert(
                replace(
                    data,
                    hh_id="republication-pipeline",
                    source_url="https://hh.ru/vacancy/republication-pipeline",
                    published_at=datetime.now(UTC),
                    details_fetched_at=datetime.now(UTC),
                    description=("\n\n" + (data.description or ""))
                    if blank_lines
                    else data.description,
                    availability=VacancyAvailability.ACTIVE,
                )
            )
            DirectionRepository(session).track_vacancy(direction_id, fresh.id)
            fresh_id = fresh.id
        second = processor.process(account_id, direction_id, fresh_id)
        assert second.applied and second.model_calls == 0
        assert client.calls == 1
        with database.sessions.begin() as session:
            assert VacancyRepository(session).get(fresh_id).duplicate_of_id == original_id
            service = ApplicationAutomationService(session)
            service.prepare_for_account_id(
                account_id=account_id, direction_name="Python backend", include_stretch=True
            )
            letters = CoverLetterService(session, writer, quality_model=judge)
            result = letters.prepare(account_id=account_id, direction_name="Python backend")
            assert result.reused == 1 and result.generated == 0
            assert len(writer.prompts) == len(judge.prompts) == 1
            job = service.claim_next(
                direction_id, require_cover_letter=True, require_cover_letter_quality=True
            )
            assert job is not None and job.vacancy.id == fresh_id
            assert _submission_allowed(service, job)
            recorded_numbers.append(job.vacancy.hh_id)
            assert service.record_result(
                job,
                HhApplyResult(
                    HhApplyStatus.APPLIED, job.vacancy.source_url, "Контрольный результат"
                ),
                apply_delay=timedelta(0),
            ).sent
            assert not _submission_allowed(service, job)
            saved = session.scalars(select(CoverLetterModel).order_by(CoverLetterModel.id)).all()
            assert len(saved) == 2 and saved[1].text == saved[0].text
            assert saved[1].reused_from_id == saved[0].id
        third = processor.process(account_id, direction_id, fresh_id)
        assert third.model_calls == 0
        with database.sessions.begin() as session:
            service = ApplicationAutomationService(session)
            repeated = service.prepare_for_account_id(
                account_id=account_id, direction_name="Python backend", include_stretch=True
            )
            assert repeated.created == 0
            assert (
                service.claim_next(
                    direction_id, require_cover_letter=True, require_cover_letter_quality=True
                )
                is None
            )
        assert recorded_numbers == ["letter-1", "republication-pipeline"]
        assert client.calls == len(writer.prompts) == len(judge.prompts) == 1
    finally:
        database.close()
