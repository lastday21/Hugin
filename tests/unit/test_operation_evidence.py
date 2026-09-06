from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from hugin import diagnostic_cli
from hugin.api.app import create_app
from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationTaskModel,
    VacancyChangeModel,
)
from hugin.diagnostics import OperationJournal, operation_context
from hugin.domain.applications import ApplicationEventType
from hugin.domain.tasks import TaskState
from hugin.repositories import AccountRepository, DirectionRepository
from hugin.services.communications import CommunicationService, RecordingMessageSender
from hugin.services.decision_evidence import (
    canonical_json,
    decision_now,
    decision_time,
    fingerprint,
    replay_ranking,
)
from hugin.services.operation_trace import OperationTraceService, safe_snapshot
from hugin.services.recruiter_reply import RecruiterReplyService
from hugin.services.vacancy_analysis import VacancyAnalysisService
from tests.unit.test_communications import create_application
from tests.unit.test_vacancy_collection import detailed_vacancy


def test_replay_uses_saved_inputs_time_and_preserves_history(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Evidence", "evidence")
            direction = DirectionRepository(session).create(account.id, "Python backend")
            service = VacancyAnalysisService(session)
            now = datetime(2026, 1, 1, tzinfo=UTC)
            vacancy = replace(
                detailed_vacancy("601", "Python developer"), published_at=now - timedelta(days=2)
            )
            with decision_time(now):
                result = service.synchronize(
                    account_external_id="evidence",
                    direction_name=direction.name,
                    vacancies=(vacancy,),
                )[0]
            first = session.scalar(
                select(VacancyChangeModel).where(VacancyChangeModel.event_type == "RULES_EVALUATED")
            )
            assert first is not None
            frozen = canonical_json(first.changes)
            assert replay_ranking(first.changes)["matches"] is True
            with decision_time(now):
                service.reanalyze(account_external_id="evidence", direction_name=direction.name)
            assert (
                len(
                    list(
                        session.scalars(
                            select(VacancyChangeModel).where(
                                VacancyChangeModel.event_type == "RULES_EVALUATED"
                            )
                        )
                    )
                )
                == 1
            )
            changed = replace(vacancy, required_qualifications="Java and Spring are required")
            with decision_time(now + timedelta(days=40)):
                service.synchronize(
                    account_external_id="evidence",
                    direction_name=direction.name,
                    vacancies=(changed,),
                )
            assert canonical_json(first.changes) == frozen
            assert replay_ranking(first.changes)["matches"] is True
            rows = list(
                session.scalars(
                    select(VacancyChangeModel)
                    .where(VacancyChangeModel.event_type == "RULES_EVALUATED")
                    .order_by(VacancyChangeModel.id)
                )
            )
            output = rows[-1].changes["output"]
            assert isinstance(output, dict)
            assert len(rows) == 2 and output["category"] == "REJECTED"
            assert rows[-1].vacancy_id == result.vacancy.id
            assert replay_ranking(rows[-1].changes)["matches"] is True
            altered = json.loads(frozen)
            altered["inputs"]["vacancy"]["title"] = "tampered"
            with pytest.raises(ValueError, match="checksum"):
                replay_ranking(altered)
            with pytest.raises(ValueError, match="Unsupported"):
                replay_ranking({"schema_version": 100})
            evidence_id = first.id
        monkeypatch.setattr(diagnostic_cli, "get_settings", lambda: settings)
        output_path = tmp_path / "database-replay.json"
        assert (
            diagnostic_cli.main(
                ["replay", "--evidence-id", str(evidence_id), "--output", str(output_path)]
            )
            == 0
        )
        assert json.loads(output_path.read_text(encoding="utf-8"))["matches"] is True
        source_path = tmp_path / "saved-evidence.json"
        source_path.write_text(frozen, encoding="utf-8")

        def no_database(*args: object, **kwargs: object) -> None:
            raise AssertionError("Offline replay must not connect to a database")

        monkeypatch.setattr(diagnostic_cli, "create_database", no_database)
        assert (
            diagnostic_cli.main(
                ["replay", "--file", str(source_path), "--output", str(tmp_path / "offline.json")]
            )
            == 0
        )
        expected_change = json.loads(frozen)
        expected_change["output"]["category"] = "REJECTED"
        expected_change["source_sha256"] = "0" * 64
        expected_change["sha256"] = fingerprint(
            {key: value for key, value in expected_change.items() if key != "sha256"}
        )
        source_path.write_text(json.dumps(expected_change), encoding="utf-8")
        assert (
            diagnostic_cli.main(
                ["replay", "--file", str(source_path), "--output", str(tmp_path / "changed.json")]
            )
            == 1
        )
        assert (
            json.loads((tmp_path / "changed.json").read_text(encoding="utf-8"))["same_source"]
            is False
        )
    finally:
        database.close()


def test_evidence_clock_is_nested_and_thread_local() -> None:
    now = datetime(2001, 1, 1, tzinfo=UTC)
    with decision_time(now):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(decision_now).result().year != 2001
        with pytest.raises(RuntimeError), decision_time(now + timedelta(days=1)):
            raise RuntimeError("stop")
        assert decision_now() == now
    assert decision_now() != now
    with pytest.raises(ValueError, match="timezone"), decision_time(datetime(2001, 1, 1)):
        pass
    with pytest.raises(TypeError, match="Unsupported"):
        canonical_json(object())


def test_manual_reply_model_can_be_found_by_application_number(settings: Settings) -> None:
    database = create_database(settings)
    journal = OperationJournal(settings.data_dir)

    class Model:
        model_name = "recorded-reply"

        def complete(self, system_prompt: str, user_prompt: str) -> str:
            run = journal.start("model", "model.complete")
            run.save_evidence("request", system_prompt=system_prompt, user_prompt=user_prompt)
            run.save_evidence("response", text="Thank you, available tomorrow.")
            run.succeed()
            return "Thank you, available tomorrow."

    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session, account_label="Reply trace", vacancy_hh_id="reply-trace"
            )
            CommunicationService(session, RecordingMessageSender()).save_incoming(
                application_id=application_id, body="When are you available?", hh_id="reply-trace"
            )
            RecruiterReplyService(session, Model()).generate(
                account_id=account_id, application_id=application_id
            )
            report = OperationTraceService(session, data_dir=settings.data_dir).application(
                application_id
            )
            assert report["counts"]["journal"] == 2
            assert report["counts"]["model_evidence"] == 2
    finally:
        database.close()


def test_trace_joins_stage_completion_model_inputs_and_keeps_private_text_local(
    settings: Settings,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session, account_label="Private-person-6622", vacancy_hh_id="602"
            )
            journal = OperationJournal(settings.data_dir)
            with operation_context(application_id=application_id, account_id=account_id):
                run = journal.start("model", "model.complete", model="recorded-model")
                assert run.save_evidence(
                    "request", user_prompt="private-text-7733", system_prompt="rules"
                )
                assert run.save_evidence("response", text="recorded-output")
            run.succeed(total_tokens=17, token_usage_available=True)
            foreign = journal.start(
                "model",
                "model.complete",
                application_id=999999,
                account_id=account_id,
                vacancy_id="602",
            )
            foreign.succeed(total_tokens=999)
            application = session.get(ApplicationModel, application_id)
            assert application is not None
            session.add(
                VacancyChangeModel(
                    vacancy_id=application.vacancy_id,
                    event_type="RULES_EVALUATED",
                    changes={"account_id": account_id, "direction_id": 999999},
                )
            )
            session.flush()
            assert (
                next(journal.entries(status="completed"))["details"]["application_id"]
                == application_id
            )
            service = OperationTraceService(session, data_dir=settings.data_dir)
            public = service.application(application_id)
            private = service.application(application_id, private=True)
            assert public["counts"]["journal"] == 2
            assert public["counts"]["model_evidence"] == 2
            assert public["metrics"]["known_total_tokens"] == 17
            assert public["metrics"]["cost"] is None
            assert public["journal_scope"] == "local_data_directory_only"
            assert public["other_process_data_directories_checked"] is False
            assert "private-text-7733" not in json.dumps(public)
            assert "Private-person-6622" not in json.dumps(public)
            assert "private-text-7733" in json.dumps(private)
            assert "attempt_inputs_missing" in public["gaps"]
            assert "ranking_inputs_missing" in public["gaps"]
            assert "private-text-7733" not in "".join(
                path.read_text(encoding="utf-8") for path in journal.log_dir.glob("*.jsonl")
            )
            source = settings.data_dir / "evidence" / "models" / f"{run.run_id}-response.json"
            source.write_text('{"sha256":"broken"}', encoding="utf-8")
            assert (
                "model_response_missing_or_invalid" in service.application(application_id)["gaps"]
            )
            source.write_text("[]", encoding="utf-8")
            assert (
                "model_response_missing_or_invalid" in service.application(application_id)["gaps"]
            )
            assert journal.save_evidence("foreign-run", "response", {"text": "wrong-call"})
            source.write_bytes((source.parent / "foreign-run-response.json").read_bytes())
            assert (
                "model_response_missing_or_invalid" in service.application(application_id)["gaps"]
            )
        with TestClient(create_app(settings)) as client:
            response = client.get(f"/api/diagnostics/applications/{application_id}?private=true")
            assert response.status_code == 200 and response.json()["private"] is False
            assert "private-text-7733" not in response.text
            assert client.get("/api/diagnostics/applications/9999999").status_code == 404
            assert client.get("/api/diagnostics/check").json()["ok"] is True
    finally:
        database.close()


@pytest.mark.parametrize(
    "state", [TaskState.PENDING, TaskState.RETRY_SCHEDULED, TaskState.COMPLETED]
)
def test_consistency_detects_impossible_task_and_clears_only_after_confirmation(
    settings: Settings, state: TaskState
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            _, app_id = create_application(session, account_label="Check", vacancy_hh_id="603")
            task = ApplicationTaskModel(
                application_id=app_id,
                state=state,
                priority_score=50,
                scheduled_at=datetime.now(UTC),
            )
            session.add(task)
            session.add(
                ApplicationEventModel(
                    application_id=app_id,
                    event_type=ApplicationEventType.UNKNOWN_RESULT,
                    payload={},
                )
            )
            session.flush()
            service = OperationTraceService(session, data_dir=settings.data_dir)
            assert service.check()["ok"] is False
            session.add(
                ApplicationEventModel(
                    application_id=app_id, event_type=ApplicationEventType.APPLIED, payload={}
                )
            )
            session.flush()
            assert service.check()["ok"] is True
            assert (
                service.check()["historical_gaps"]["historical_confirmation_without_snapshot"] == 1
            )
    finally:
        database.close()


def test_snapshot_masks_unknown_keys_and_free_text() -> None:
    result = safe_snapshot(
        {
            "secretKeyPerson6622": "private",
            "payload": {"state": "APPLIED", "text": "secret"},
            "source_sha256": fingerprint("code"),
            "nested": ["private", 2, None],
        }
    )
    assert "secretKeyPerson6622" not in json.dumps(result)
    assert "private" not in json.dumps(result)
    assert result["payload"]["state"] == "APPLIED"
    assert result["source_sha256"] == fingerprint("code")


def test_paused_applications_do_not_hide_enabled_automatic_replies(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            session.execute(text("UPDATE system_state SET state='PAUSED' WHERE id=1"))
            session.execute(
                text(
                    "UPDATE application_settings SET autonomy_policy=jsonb_set(autonomy_policy,"
                    "'{auto_send_approved_replies}','true') WHERE id=1"
                )
            )
            service = OperationTraceService(session, data_dir=settings.data_dir)
            report = service.check()
            assert report["automatic_replies_enabled"] is True
            assert report["automatic_sending_stopped"] is False
            session.execute(
                text(
                    "UPDATE application_settings SET autonomy_policy=jsonb_set(autonomy_policy,"
                    "'{auto_send_approved_replies}','false') WHERE id=1"
                )
            )
            assert service.check()["automatic_sending_stopped"] is True
            session.execute(
                text(
                    "UPDATE system_state SET supervised_lease_token='private-lease-value', "
                    "supervised_lease_expires_at=now()+interval '2 minutes' WHERE id=1"
                )
            )
            report = service.check()
            assert report["automatic_sending_stopped"] is False
            assert report["supervised_submission_active"] is True
            assert "private-lease-value" not in json.dumps(report)
    finally:
        database.close()


def test_cli_stop_guard_fails_when_only_application_queue_is_paused(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = create_database(settings)
    monkeypatch.setattr(diagnostic_cli, "get_settings", lambda: settings)
    try:
        with database.sessions.begin() as session:
            session.execute(text("UPDATE system_state SET state='PAUSED' WHERE id=1"))
            session.execute(
                text(
                    "UPDATE application_settings SET autonomy_policy=jsonb_set(autonomy_policy,"
                    "'{auto_send_approved_replies}','true') WHERE id=1"
                )
            )
        assert diagnostic_cli.main(["check"]) == 0
        assert diagnostic_cli.main(["check", "--require-stopped"]) == 1
        with database.sessions.begin() as session:
            session.execute(
                text(
                    "UPDATE application_settings SET autonomy_policy=jsonb_set(autonomy_policy,"
                    "'{auto_send_approved_replies}','false') WHERE id=1"
                )
            )
        assert diagnostic_cli.main(["check", "--require-stopped"]) == 0
    finally:
        database.close()


def test_evidence_write_failure_does_not_destroy_existing_record(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path)
    assert journal.save_evidence("test-run", "request", {"text": "first"})
    assert journal.save_evidence("test-run", "request", {"text": "second"}) is False
    payload = json.loads(
        (tmp_path / "evidence/models/test-run-request.json").read_text(encoding="utf-8")
    )
    assert payload["payload"]["text"] == "first"
    assert next(journal.entries(status="failed"))["details"]["error_type"] == "FileExistsError"
    with pytest.raises(ValueError, match="identifier"):
        journal.save_evidence("../escape", "request", {})


def test_cli_reads_saved_report_and_refuses_overwrite(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(diagnostic_cli, "get_settings", lambda: settings)
    output = tmp_path / "check.json"
    assert diagnostic_cli.main(["check", "--output", str(output)]) == 0
    assert diagnostic_cli.main(["check"]) == 0
    with pytest.raises(FileExistsError):
        diagnostic_cli.main(["check", "--output", str(output)])
    with pytest.raises(LookupError):
        diagnostic_cli.main(["trace", "999999", "--output", str(tmp_path / "missing.json")])
    with pytest.raises(LookupError):
        diagnostic_cli.main(
            ["replay", "--evidence-id", "999999", "--output", str(tmp_path / "missing.json")]
        )
    invalid: dict[str, Any] = {"schema_version": 100}
    output.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ValueError):
        diagnostic_cli.main(
            ["replay", "--file", str(output), "--output", str(tmp_path / "invalid.json")]
        )
