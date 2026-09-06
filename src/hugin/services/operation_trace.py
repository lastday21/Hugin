from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from hugin.database.base import Base
from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationOutcomeModel,
    ApplicationStatusObservationModel,
    ApplicationTaskModel,
    CoverLetterModel,
    DirectionVacancyModel,
    RecruiterMessageModel,
    VacancyChangeModel,
    VacancyDiscoveryModel,
    VacancyModel,
)
from hugin.diagnostics import OperationJournal
from hugin.services.decision_evidence import canonical_json, fingerprint, source_fingerprint
from hugin.services.operation_timing import (
    history_quality,
    history_records,
    task_timing,
    timing_report,
)
from hugin.services.screening_evidence import screening_evidence
from hugin.services.trace_quality import attempt_quality, model_metrics, stage_quality

_SAFE_KEYS = frozenset(
    column.name for table in Base.metadata.tables.values() for column in table.columns
) | frozenset(
    {
        "application",
        "vacancy_current",
        "events",
        "tasks",
        "status_observations",
        "outcomes",
        "letters",
        "screening_forms",
        "questions",
        "answer",
        "messages",
        "discoveries",
        "vacancy_history",
        "selection_current",
        "journal",
        "schema_version",
        "kind",
        "source_sha256",
        "sha256",
        "observed_at",
        "inputs",
        "output",
        "applied",
        "context",
        "vacancy",
        "scope",
        "duration_ms",
        "provenance",
        "details",
        "timestamp",
        "component",
        "event",
        "status",
        "level",
        "process_id",
        "thread",
        "run_id",
        "parent_run_id",
        "attempt_number",
        "result_status",
        "confirmation",
        "selection_snapshot",
        "outcome_context",
        "snapshot_missing",
        "category",
        "accepted",
        "reasons",
        "components",
        "fit",
        "tier",
        "evaluation",
        "manual_accept",
        "model",
        "operation",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "token_usage_available",
        "cost",
        "cost_available",
    }
)
_STATE_CODES = frozenset(
    {
        "APPLY_INTENT",
        "APPLIED",
        "UNKNOWN_RESULT",
        "STATE_CHANGED",
        "RULES_EVALUATED",
        "MATCH",
        "STRETCH",
        "REJECTED",
        "ROUTED",
        "PAUSED",
        "RUNNING",
        "PENDING",
        "COMPLETED",
        "RETRY_SCHEDULED",
        "SKIPPED",
        "REVIEW_REQUIRED",
        "INPUT_REQUIRED",
        "VIEWED",
        "INVITED",
        "CLOSED",
        "started",
        "completed",
        "failed",
        "blocked",
        "skipped",
    }
)


def _row(model: Base) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            canonical_json(
                {column.name: getattr(model, column.name) for column in model.__table__.columns}
            )
        ),
    )


def safe_snapshot(value: Any, *, key: str = "") -> Any:
    """Export structure and checksums; arbitrary source strings never leave the local record."""
    if isinstance(value, dict):
        # Keys in source payloads can contain user text too.
        return {
            name if name in _SAFE_KEYS else fingerprint(name): safe_snapshot(item, key=name)
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [safe_snapshot(item) for item in value]
    if isinstance(value, str):
        if (
            key in {"state", "event_type", "status", "category", "result_status"}
            and value in _STATE_CODES
        ):
            return value
        if (
            key.endswith("sha256")
            and len(value) == 64
            and all(c in "0123456789abcdef" for c in value)
        ):
            return value
        return {"characters": len(value), "sha256": fingerprint(value)}
    return value


class OperationTraceService:
    def __init__(self, session: Session, *, data_dir: Path) -> None:
        self._session = session
        self._data_dir = data_dir
        self._journal_cache: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None

    def journal_window(self, *, since: datetime, until: datetime) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        rows = list(OperationJournal(self._data_dir).entries(issues=issues))
        window = timing_report(rows, since=since, until=until)
        ids = {item["run_id"] for item in window["intervals"]} | set(window["incomplete_runs"])
        keys = {
            "duration_ms",
            "account_id",
            "application_id",
            "task_id",
            "attempt_number",
            "job_key",
            "job_kind",
            "parent_run_id",
            "step_name",
            "required_steps",
            "timing_kind",
            "reason",
        }
        return {
            "records": [
                {
                    **{
                        key: row.get(key)
                        for key in (
                            "timestamp",
                            "run_id",
                            "status",
                            "event",
                            "component",
                            "process_id",
                            "thread",
                        )
                    },
                    "details": {
                        key: value for key, value in row.get("details", {}).items() if key in keys
                    },
                }
                for row in history_records(rows, ids)
            ],
            "journal_read_issues": issues,
        }

    def timeline(
        self,
        *,
        since: datetime,
        until: datetime | None = None,
        additional_records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        rows = list(OperationJournal(self._data_dir).entries(issues=issues))
        seen = {(row.get("run_id"), row.get("timestamp"), row.get("status")) for row in rows}
        rows.extend(
            {**row, "source": "server"}
            for row in additional_records or []
            if (row.get("run_id"), row.get("timestamp"), row.get("status")) not in seen
        )
        end = until or datetime.now(UTC)
        report = timing_report(rows, since=since, until=end)
        run_ids = {item["run_id"] for item in report["intervals"]} | set(report["incomplete_runs"])
        selected = history_records(rows, run_ids)
        return {
            "schema_version": 1,
            "timing": report,
            "history": history_quality(selected),
            "journal_read_issues": issues,
            "scope": "local_data_directory_all_workers",
            "external_state_checked": False,
        }

    def audit(self, *, limit: int = 20, since: datetime | None = None) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise ValueError("Размер выборки должен быть от 1 до 1000")
        if since is not None and since.tzinfo is None:
            raise ValueError("Укажите часовой пояс начала периода")
        query = select(ApplicationModel.id).order_by(ApplicationModel.id.desc()).limit(limit)
        if since is not None:
            query = query.where(
                select(ApplicationEventModel.id)
                .where(
                    ApplicationEventModel.application_id == ApplicationModel.id,
                    ApplicationEventModel.created_at >= since,
                )
                .exists()
            )
        ids = list(self._session.scalars(query))
        read_issues: list[dict[str, Any]] = []
        self._journal_cache = (
            list(OperationJournal(self._data_dir).entries(issues=read_issues)),
            read_issues,
        )
        try:
            reports = [self.application(app_id) for app_id in ids]
        finally:
            self._journal_cache = None
        gap_counts = Counter(gap for report in reports for gap in set(report["gaps"]))
        complete = sum(not report["gaps"] for report in reports)
        storage = {}
        for label, directory, pattern in (
            ("journal", self._data_dir / "logs", "hugin-*.jsonl"),
            ("evidence", self._data_dir / "evidence/models", "*.json"),
        ):
            sizes = []
            unreadable = 0
            for path in directory.glob(pattern):
                try:
                    if path.is_file() and not path.is_symlink():
                        sizes.append(path.stat().st_size)
                except OSError:
                    unreadable += 1
            storage[label] = {"files": len(sizes), "bytes": sum(sizes), "unreadable": unreadable}
        return {
            "schema_version": 1,
            "captured_at": datetime.now(UTC).isoformat(),
            "source_sha256": source_fingerprint(),
            "sample_selection": "latest_application_ids_with_events_in_period",
            "since": since.isoformat() if since else None,
            "limit": limit,
            "sample_size": len(ids),
            "applications_with_complete_recorded_inputs": complete,
            "complete_recorded_inputs_percent": round(100 * complete / len(ids), 2)
            if ids
            else None,
            "gap_counts": dict(gap_counts),
            "journal_read_issues": read_issues,
            "storage": storage,
            "applications": [
                {
                    "application_id": report["application_id"],
                    "gaps": report["gaps"],
                    "quality": report["quality"],
                    "metrics": report["metrics"],
                }
                for report in reports
            ],
            "journal_scope": "local_data_directory_only",
            "external_state_checked": False,
            "assessment_scope": "recorded_inputs_only_not_external_truth_or_decision_correctness",
        }

    def application(self, application_id: int, *, private: bool = False) -> dict[str, Any]:
        application = self._session.get(ApplicationModel, application_id)
        if application is None:
            raise LookupError("Application not found")
        vacancy = self._session.get(VacancyModel, application.vacancy_id)
        assert vacancy is not None
        sections: dict[str, Any] = {
            "application": _row(application),
            "vacancy_current": _row(vacancy),
            "screening_forms": screening_evidence(self._session, application_id),
        }
        for name, model in (
            ("events", ApplicationEventModel),
            ("tasks", ApplicationTaskModel),
            ("status_observations", ApplicationStatusObservationModel),
            ("outcomes", ApplicationOutcomeModel),
            ("letters", CoverLetterModel),
            ("messages", RecruiterMessageModel),
        ):
            sections[name] = [
                _row(row)
                for row in self._session.scalars(
                    select(model).where(model.application_id == application_id).order_by(model.id)
                )
            ]
        for name, history_model in (
            ("discoveries", VacancyDiscoveryModel),
            ("vacancy_history", VacancyChangeModel),
        ):
            sections[name] = [
                _row(row)
                for row in self._session.scalars(
                    select(history_model)
                    .where(history_model.vacancy_id == vacancy.id)
                    .order_by(history_model.id)
                )
            ]
        tracked = (
            self._session.get(DirectionVacancyModel, (application.direction_id, vacancy.id))
            if application.direction_id is not None
            else None
        )
        sections["selection_current"] = _row(tracked) if tracked is not None else None
        sections["discoveries"] = [
            row
            for row in sections["discoveries"]
            if row["direction_id"] == application.direction_id
        ]
        sections["vacancy_history"] = [
            row
            for row in sections["vacancy_history"]
            if row["event_type"] != "RULES_EVALUATED"
            or (
                row["changes"].get("account_id") == application.account_id
                and row["changes"].get("direction_id") == application.direction_id
            )
        ]
        task_ids = {row["id"] for row in sections["tasks"]}
        logs = []
        journal_issues: list[dict[str, Any]] = []
        journal_rows = (
            OperationJournal(self._data_dir).entries(issues=journal_issues)
            if self._journal_cache is None
            else self._journal_cache[0]
        )
        if self._journal_cache is not None:
            journal_issues = self._journal_cache[1]
        for entry in journal_rows:
            details = entry.get("details", {})
            if not isinstance(details, dict):
                continue
            if details.get("application_id") == application_id or (
                details.get("application_id") is None
                and details.get("account_id") == application.account_id
                and (
                    (isinstance(details.get("task_id"), int) and details["task_id"] in task_ids)
                    or str(details.get("vacancy_id")) == vacancy.hh_id
                )
            ):
                logs.append(entry)
        sections["journal"] = logs
        gaps = []
        if journal_issues:
            gaps.append("journal_read_incomplete")
        if not sections["discoveries"]:
            gaps.append("discovery_missing")
        if not any(row["event_type"] == "RULES_EVALUATED" for row in sections["vacancy_history"]):
            gaps.append("ranking_inputs_missing")
        for row in sections["vacancy_history"]:
            if row["event_type"] != "RULES_EVALUATED":
                continue
            evidence = row["changes"]
            if (
                evidence.get("schema_version") != 1
                or evidence.get("kind") != "vacancy_ranking"
                or evidence.get("sha256")
                != fingerprint({k: v for k, v in evidence.items() if k != "sha256"})
            ):
                gaps.append("ranking_inputs_invalid")
                break
        attempts = attempt_quality(sections["events"])
        stages = stage_quality(logs)
        history = history_quality(logs)
        if history["complete"] is False:
            gaps.append("required_steps_missing")
        if not attempts["total"] or attempts["complete"] != attempts["total"]:
            gaps.append("attempt_inputs_missing")
        if stages["incomplete"] or stages["events_without_run_id"]:
            gaps.append("journal_stages_incomplete")
        if not logs:
            gaps.append("journal_missing_or_expired")
        for event in sections["events"]:
            payload = event["payload"]
            if event["event_type"] != "APPLY_INTENT" or payload.get("source") != "hugin_attempt":
                continue
            matching = [
                row
                for row in logs
                if row.get("component") == "applications"
                and row.get("event") == "apply"
                and row.get("details", {}).get("task_id") == payload.get("task_id")
                and row.get("details", {}).get("attempt_number") == payload.get("attempt_number")
            ]
            matched_stages = stage_quality(matching)
            if matched_stages["total"] != 1 or matched_stages["complete"] != 1:
                gaps.append("attempt_stage_links_missing")
                break
        model_evidence = []
        evidence_stages: dict[str, set[str]] = {}
        for row in logs:
            if not isinstance(row.get("run_id"), str):
                continue
            if row.get("event") == "model.complete":
                evidence_stages.setdefault(row["run_id"], set()).update(("request", "response"))
            if row.get("status") == "failed":
                evidence_stages.setdefault(row["run_id"], set()).add("failure")
        for run_id, stages_to_read in sorted(evidence_stages.items()):
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", run_id) is None:
                gaps.append("evidence_run_id_invalid")
                continue
            for stage in sorted(stages_to_read):
                path = self._data_dir / "evidence" / "models" / f"{run_id}-{stage}.json"
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(saved, dict) or (
                        saved.get("schema_version") != 1
                        or saved.get("run_id") != run_id
                        or saved.get("stage") != stage
                    ):
                        raise ValueError("Evidence identity mismatch")
                    unsigned = {key: value for key, value in saved.items() if key != "sha256"}
                    if fingerprint(unsigned) != saved.get("sha256"):
                        raise ValueError("Evidence checksum mismatch")
                except (OSError, ValueError, AttributeError):
                    gaps.append(f"model_{stage}_missing_or_invalid")
                    continue
                model_evidence.append(saved)
        sections["model_evidence"] = model_evidence
        metrics = model_metrics(logs)
        captured_at = datetime.now(UTC)
        task_times = [task_timing(task, logs, now=captured_at) for task in sections["tasks"]]
        return {
            "schema_version": 1,
            "application_id": application_id,
            "captured_at": captured_at.isoformat(),
            "source_sha256": source_fingerprint(),
            "private": private,
            "journal_scope": "local_data_directory_only",
            "other_process_data_directories_checked": False,
            "gaps": gaps,
            "journal_read_issues": journal_issues,
            "quality": {
                "attempts": attempts,
                "stages": stages,
                "history": history,
                "recorded_inputs_complete": not gaps,
                "external_confirmation_verified": False,
            },
            "metrics": metrics,
            "task_timing": task_times,
            "counts": {
                name: len(rows) for name, rows in sections.items() if isinstance(rows, list)
            },
            "sections": sections if private else safe_snapshot(sections),
            "external_state_checked": False,
        }

    def check(self) -> dict[str, Any]:
        queries = {
            "completed_without_confirmation": """
                SELECT t.id FROM application_tasks t WHERE t.state = 'COMPLETED'
                AND NOT EXISTS (SELECT 1 FROM application_events e
                    WHERE e.application_id=t.application_id AND e.event_type='APPLIED')
            """,
            "cross_account_resume": """
                SELECT a.id FROM applications a JOIN resumes r ON r.id=a.resume_id
                WHERE a.account_id<>r.account_id
            """,
            "cross_account_direction": """
                SELECT a.id FROM applications a JOIN career_directions d ON d.id=a.direction_id
                WHERE a.account_id<>d.account_id
            """,
            "unknown_result_ready_for_retry": """
                SELECT t.id FROM application_tasks t WHERE t.state IN ('PENDING','RETRY_SCHEDULED')
                AND (SELECT e.event_type FROM application_events e
                    WHERE e.application_id=t.application_id
                      AND e.event_type IN ('UNKNOWN_RESULT','APPLIED')
                    ORDER BY e.created_at DESC,e.id DESC LIMIT 1)='UNKNOWN_RESULT'
            """,
        }
        problems = {
            name: list(self._session.scalars(text(query))) for name, query in queries.items()
        }
        gap_counts: Counter[str] = Counter()
        for event in self._session.scalars(
            select(ApplicationEventModel).where(ApplicationEventModel.event_type == "APPLIED")
        ):
            if event.payload.get("snapshot_missing") is not False:
                gap_counts["historical_confirmation_without_snapshot"] += 1
        system = (
            self._session.execute(
                text(
                    "SELECT s.state, a.autonomy_policy, a.search_enabled, "
                    "COALESCE(s.supervised_lease_token IS NOT NULL "
                    "AND s.supervised_lease_expires_at>now(),false) AS supervised_active "
                    "FROM system_state s LEFT JOIN application_settings a ON a.id=s.id "
                    "WHERE s.id=1"
                )
            )
            .mappings()
            .one_or_none()
        )
        state = system["state"] if system else None
        policy = system["autonomy_policy"] if system else None
        reply_enabled = (
            policy.get("auto_send_approved_replies", True) if isinstance(policy, dict) else None
        )
        supervised_active = system["supervised_active"] if system else None
        return {
            "schema_version": 1,
            "checked_at": datetime.now(UTC).isoformat(),
            "ok": not any(problems.values()),
            "problems": problems,
            "historical_gaps": dict(gap_counts),
            "system_state": state,
            "automatic_replies_enabled": reply_enabled,
            "search_enabled": system["search_enabled"] if system else None,
            "supervised_submission_active": supervised_active,
            "automatic_sending_stopped": state == "PAUSED"
            and reply_enabled is False
            and supervised_active is False,
            "external_state_checked": False,
        }
