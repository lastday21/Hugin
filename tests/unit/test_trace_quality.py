from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hugin import diagnostic_cli
from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ScreeningQuestionModel,
    VacancyChangeModel,
    VacancyDiscoveryModel,
)
from hugin.diagnostics import OperationJournal
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.decision_evidence import fingerprint
from hugin.services.operation_trace import OperationTraceService
from hugin.services.screening_forms import ScreeningDraftService
from hugin.services.trace_quality import attempt_quality, model_metrics, stage_quality
from tests.unit.test_application_result_independent_review import create_claim
from tests.unit.test_communications import create_application


def test_form_questions_are_available_in_trace_and_frozen_for_next_attempt(
    settings: Settings,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            job = create_claim(session)
            draft = ScreeningDraftService(session).capture_questions(
                job.application.id, ("Какие сферы работы вы не рассматриваете?",)
            )
            ApplicationAutomationService(session)._save_attempt(job)
            report = OperationTraceService(session, data_dir=settings.data_dir).application(
                job.application.id, private=True
            )
            forms = report["sections"]["screening_forms"]
            assert forms[0]["id"] == draft.form_id
            assert forms[0]["questions"][0]["question_text"] == draft.questions[0].question
            snapshot = report["sections"]["events"][-1]["payload"]["selection_snapshot"]
            assert snapshot["screening_forms"] == forms
            assert snapshot["sha256"] == fingerprint(
                {k: v for k, v in snapshot.items() if k != "sha256"}
            )
            question = session.get(ScreeningQuestionModel, forms[0]["questions"][0]["id"])
            assert question is not None
            question.question_text = "Работодатель изменил вопрос"
            session.flush()
            changed = OperationTraceService(session, data_dir=settings.data_dir).application(
                job.application.id, private=True
            )
            frozen = changed["sections"]["events"][-1]["payload"]["selection_snapshot"]
            assert frozen["screening_forms"][0]["questions"][0]["question_text"] == (
                draft.questions[0].question
            )
    finally:
        database.close()


@pytest.mark.parametrize("tokens", [None, True, -1])
def test_missing_or_invalid_model_usage_is_unknown(settings: Settings, tokens: object) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            _, app_id = create_application(session, account_label="Usage", vacancy_hh_id="usage")
            journal = OperationJournal(settings.data_dir)
            run = journal.start("model", "model.complete", application_id=app_id)
            run.succeed(total_tokens=tokens, token_usage_available=True)
            report = OperationTraceService(session, data_dir=settings.data_dir).application(app_id)
            assert report["metrics"]["known_total_tokens"] is None
            assert report["metrics"]["token_usage_complete"] is False
    finally:
        database.close()


def test_broken_journal_is_visible_without_exporting_its_text(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            _, app_id = create_application(
                session, account_label="Journal", vacancy_hh_id="journal"
            )
            journal = OperationJournal(settings.data_dir)
            journal.start("applications", "apply", application_id=app_id)
            path = next(journal.log_dir.glob("*.jsonl"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write('{"private-person-broken\n')
            report = OperationTraceService(session, data_dir=settings.data_dir).application(app_id)
            assert "journal_read_incomplete" in report["gaps"]
            assert report["journal_read_issues"][0]["line"] == 2
            assert "private-person-broken" not in json.dumps(report)
            assert report["quality"]["stages"]["incomplete"] == 1
    finally:
        database.close()


def test_one_old_snapshot_does_not_cover_another_attempt(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            _, app_id = create_application(
                session, account_label="Attempts", vacancy_hh_id="attempts"
            )
            for attempt, snapshot in ((1, {"rules_version": "old"}), (2, None)):
                session.add(
                    ApplicationEventModel(
                        application_id=app_id,
                        event_type="APPLY_INTENT",
                        payload={
                            "task_id": 7,
                            "attempt_number": attempt,
                            "selection_snapshot": snapshot,
                        },
                    )
                )
            session.flush()
            report = OperationTraceService(session, data_dir=settings.data_dir).application(app_id)
            assert "attempt_inputs_missing" in report["gaps"]
            assert report["quality"]["attempts"]["total"] == 2
            assert report["quality"]["attempts"]["complete"] == 0
    finally:
        database.close()


def test_stage_records_source_version_and_reports_unreadable_file(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path)
    run = journal.start("applications", "apply", application_id=1)
    run.succeed()
    entries = list(journal.entries())
    assert len(entries[0]["source_sha256"]) == 64
    assert entries[0]["source_sha256"] == entries[1]["source_sha256"]


def test_actual_claim_freezes_the_code_and_checksum_before_external_action(
    settings: Settings,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            job = create_claim(session)
            snapshot = ApplicationAutomationService._selection_snapshot(job)
            assert isinstance(snapshot["source_sha256"], str)
            assert len(snapshot["source_sha256"]) == 64
            assert snapshot["sha256"] == fingerprint(
                {k: v for k, v in snapshot.items() if k != "sha256"}
            )
    finally:
        database.close()


def test_failed_model_calls_with_reported_usage_are_included(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path)
    completed = journal.start("model", "model.complete")
    completed.succeed(total_tokens=12, token_usage_available=True)
    failed = journal.start("model", "model.complete")
    failed.fail(RuntimeError("bad response"), total_tokens=4, token_usage_available=True)
    journal.start("model", "model.complete")
    result = model_metrics(list(journal.entries()))
    assert result["known_total_tokens"] == 16
    assert result["model_calls_failed"] == 1
    assert result["model_calls_with_unknown_tokens"] == 1
    assert result["token_usage_complete"] is False
    assert model_metrics([])["known_total_tokens"] is None


def test_stage_quality_separates_complete_interrupted_and_duplicate_results(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path)
    run = journal.start("worker", "apply")
    run.succeed()
    rows = list(journal.entries())
    assert stage_quality(rows)["complete_percent"] == 100
    assert stage_quality(rows[:1])["incomplete"] == 1
    assert stage_quality(rows[1:])["incomplete"] == 1
    assert stage_quality(rows + rows[1:])["incomplete"] == 1
    assert stage_quality([{}])["events_without_run_id"] == 1
    assert stage_quality([])["complete_percent"] is None


@pytest.mark.parametrize("duration", [None, True, -1, float("inf"), "1"])
def test_stage_duration_requires_a_finite_nonnegative_number(
    tmp_path: Path, duration: object
) -> None:
    journal = OperationJournal(tmp_path)
    run = journal.start("worker", "apply")
    run.succeed()
    rows = list(journal.entries())
    rows[-1]["details"]["duration_ms"] = duration
    assert "duration_missing_or_invalid" in stage_quality(rows)["issues"][0]["reasons"]


@pytest.mark.parametrize(
    "timestamp", [None, "bad", "2026-01-01T00:00:00", "2000-01-01T00:00:00+00:00"]
)
def test_stage_time_must_be_zoned_and_in_order(tmp_path: Path, timestamp: object) -> None:
    journal = OperationJournal(tmp_path)
    run = journal.start("worker", "apply")
    run.succeed()
    rows = list(journal.entries())
    rows[-1]["timestamp"] = timestamp
    assert stage_quality(rows)["complete"] == 0


def test_snapshot_integrity_detects_changes_to_actual_attempt(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            job = create_claim(session)
            snapshot = ApplicationAutomationService._selection_snapshot(job)
            event = {
                "id": 1,
                "event_type": "APPLY_INTENT",
                "payload": {
                    "task_id": job.task.id,
                    "attempt_number": job.task.attempts,
                    "selection_snapshot": snapshot,
                },
            }
            before = attempt_quality([event])
            assert all(
                "snapshot_checksum_missing_or_invalid" not in row["reasons"]
                for row in before["issues"]
            )
            snapshot["rules_version"] = "modified"
            assert (
                "snapshot_checksum_missing_or_invalid"
                in attempt_quality([event])["issues"][0]["reasons"]
            )
    finally:
        database.close()


def test_exception_source_text_is_kept_out_of_technical_journal(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path)
    run = journal.start("applications", "apply", application_id=3)
    run.fail(RuntimeError("Candidate private-person-8844 answered private-answer-7755"))
    raw = next(journal.log_dir.glob("*.jsonl")).read_text(encoding="utf-8")
    assert "private-person-8844" not in raw
    assert "private-answer-7755" not in raw
    evidence = json.loads(
        (tmp_path / f"evidence/models/{run.run_id}-failure.json").read_text(encoding="utf-8")
    )
    assert "private-person-8844" in evidence["payload"]["error_message"]


def test_audit_reports_a_defined_sample_and_empty_period_without_false_success(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            create_application(session, account_label="Audit private-8844", vacancy_hh_id="audit")
        monkeypatch.setattr(diagnostic_cli, "get_settings", lambda: settings)
        path = tmp_path / "audit.json"
        assert diagnostic_cli.main(["audit", "--limit", "1", "--output", str(path)]) == 0
        report = json.loads(path.read_text(encoding="utf-8"))
        assert report["sample_size"] == 1
        assert report["applications_with_complete_recorded_inputs"] == 0
        assert "private-8844" not in json.dumps(report)
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        path = tmp_path / "empty.json"
        assert diagnostic_cli.main(["audit", "--since", future, "--output", str(path)]) == 0
        report = json.loads(path.read_text(encoding="utf-8"))
        assert report["sample_size"] == 0
        assert report["complete_recorded_inputs_percent"] is None
        assert report["external_state_checked"] is False
    finally:
        database.close()


def test_complete_saved_attempt_and_individual_content_tampering(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            job = create_claim(session)
            profile: dict[str, object] = {
                "captured_at": datetime.now(UTC).isoformat(),
                "resume_content": "Python experience",
                "resume_content_sha256": fingerprint("Python experience"),
                "profile_facts": [{"content": "Confirmed experience"}],
                "profile_facts_sha256": fingerprint([{"content": "Confirmed experience"}]),
            }
            job = replace(
                job,
                profile_snapshot=profile,
                cover_letter="Application letter",
                cover_letter_sha256=hashlib.sha256(b"Application letter").hexdigest(),
                vacancy=replace(job.vacancy, description="Original vacancy"),
            )
            snapshot = ApplicationAutomationService._selection_snapshot(job)
            event = {
                "id": 1,
                "event_type": "APPLY_INTENT",
                "payload": {
                    "task_id": job.task.id,
                    "attempt_number": job.task.attempts,
                    "selection_snapshot": snapshot,
                },
            }
            assert attempt_quality([event])["complete_percent"] == 100
            for group, key in (
                ("profile", "resume_content"),
                ("profile", "profile_facts"),
                ("vacancy", "description"),
            ):
                changed = json.loads(json.dumps(event))
                changed["payload"]["selection_snapshot"]["outcome_context"][group][key] = "altered"
                assert attempt_quality([changed])["complete"] == 0
    finally:
        database.close()


@pytest.mark.parametrize("since", ["bad", "2026-09-05T00:00:00"])
def test_audit_rejects_ambiguous_period_before_database_access(since: str, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        diagnostic_cli.main(["audit", "--since", since, "--output", str(tmp_path / "bad.json")])
    assert error.value.code == 2


def test_reader_reports_non_objects_and_unreadable_files_without_raw_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = OperationJournal(tmp_path)
    journal.record("worker", "operation", status="completed")
    path = next(journal.log_dir.glob("*.jsonl"))
    with path.open("a", encoding="utf-8") as stream:
        stream.write('["private-text"]\n')
    issues: list[dict[str, object]] = []
    assert len(list(journal.entries(issues=issues))) == 1
    assert issues[0]["reason"] == "invalid_record"

    def fail(*args: object, **kwargs: object) -> str:
        raise OSError("private-path")

    monkeypatch.setattr(Path, "read_text", fail)
    unreadable_issues: list[dict[str, object]] = []
    assert list(journal.entries(issues=unreadable_issues)) == []
    assert unreadable_issues[0]["reason"] == "unreadable"
    assert "private" not in json.dumps(unreadable_issues)


def test_unrelated_discovery_and_unsigned_decision_do_not_make_trace_complete(
    settings: Settings,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            job = create_claim(session)
            app = session.get(ApplicationModel, job.application.id)
            assert app is not None
            session.add(
                VacancyChangeModel(
                    vacancy_id=app.vacancy_id,
                    event_type="RULES_EVALUATED",
                    changes={"account_id": app.account_id, "direction_id": app.direction_id},
                )
            )
            session.add(
                VacancyDiscoveryModel(
                    vacancy_id=app.vacancy_id, direction_id=None, query_text="Unrelated source"
                )
            )
            session.flush()
            report = OperationTraceService(session, data_dir=settings.data_dir).application(app.id)
            assert "ranking_inputs_invalid" in report["gaps"]
            assert "discovery_missing" in report["gaps"]
            assert "attempt_stage_links_missing" in report["gaps"]
    finally:
        database.close()
