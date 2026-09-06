from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import datetime
from typing import Any

from hugin.services.decision_evidence import fingerprint

_TERMINAL = {"completed", "failed", "blocked", "skipped"}


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _digest(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def model_metrics(logs: list[dict[str, Any]]) -> dict[str, Any]:
    calls: dict[str, dict[str, Any]] = {}
    for row in logs:
        if row.get("event") == "model.complete" and isinstance(row.get("run_id"), str):
            calls[row["run_id"]] = row
    terminal = [row for row in calls.values() if row.get("status") in _TERMINAL]
    known = [
        row["details"]["total_tokens"]
        for row in terminal
        if isinstance(row.get("details"), dict)
        and row["details"].get("token_usage_available") is True
        and _count(row["details"].get("total_tokens"))
    ]
    return {
        "model_calls": len(calls),
        "model_calls_completed": sum(row.get("status") == "completed" for row in terminal),
        "model_calls_failed": sum(row.get("status") == "failed" for row in terminal),
        "model_calls_with_known_tokens": len(known),
        "model_calls_with_unknown_tokens": len(calls) - len(known),
        "token_usage_complete": bool(calls) and len(known) == len(calls),
        "known_total_tokens": sum(known) if known else None,
        "cost": None,
        "cost_available": False,
    }


def stage_quality(logs: list[dict[str, Any]]) -> dict[str, Any]:
    runs: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    missing_id = 0
    for row in logs:
        if not isinstance(row.get("run_id"), str) or not row["run_id"]:
            missing_id += 1
            continue
        runs[(str(row.get("component")), str(row.get("event")), row["run_id"])].append(row)
    issues = []
    durations = []
    complete = 0
    for (_, _, run_id), rows in runs.items():
        reasons = []
        starts = [row for row in rows if row.get("status") == "started"]
        ends = [row for row in rows if row.get("status") in _TERMINAL]
        if len(starts) != 1:
            reasons.append("start_missing_or_duplicated")
        if len(ends) != 1:
            reasons.append("result_missing_or_duplicated")
        if any(not _digest(row.get("source_sha256")) for row in rows):
            reasons.append("source_version_missing")
        if any(_timestamp(row.get("timestamp")) is None for row in rows):
            reasons.append("timestamp_invalid")
        if len(starts) == len(ends) == 1:
            start = _timestamp(starts[0].get("timestamp"))
            end = _timestamp(ends[0].get("timestamp"))
            if start is not None and end is not None and end < start:
                reasons.append("result_precedes_start")
            details = ends[0].get("details")
            duration = details.get("duration_ms") if isinstance(details, dict) else None
            if (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and math.isfinite(duration)
                and duration >= 0
            ):
                durations.append(duration)
            else:
                reasons.append("duration_missing_or_invalid")
        if reasons:
            issues.append({"run_id_sha256": fingerprint(run_id), "reasons": reasons})
        else:
            complete += 1
    return {
        "total": len(runs),
        "complete": complete,
        "incomplete": len(runs) - complete,
        "events_without_run_id": missing_id,
        "complete_percent": round(100 * complete / len(runs), 2) if runs else None,
        "known_stage_duration_ms": sum(durations) if durations else None,
        "duration_is_wall_clock": False,
        "issues": issues,
    }


def attempt_quality(events: list[dict[str, Any]]) -> dict[str, Any]:
    intents = [row for row in events if row.get("event_type") == "APPLY_INTENT"]
    attempts = [
        row
        for row in intents
        if isinstance(row.get("payload"), dict)
        and (
            row["payload"].get("source") == "hugin_attempt"
            or "attempt_number" in row["payload"]
            or "task_id" in row["payload"]
        )
    ]
    issues = []
    for event in attempts:
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        reasons = []
        if not _count(payload.get("task_id")) or not _count(payload.get("attempt_number")):
            reasons.append("attempt_identity_missing")
        snapshot = payload.get("selection_snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        if not snapshot.get("rules_version") or not isinstance(snapshot.get("rules_details"), dict):
            reasons.append("rules_missing")
        if not _digest(snapshot.get("source_sha256")):
            reasons.append("source_version_missing")
        if snapshot.get("sha256") != fingerprint(
            {k: v for k, v in snapshot.items() if k != "sha256"}
        ):
            reasons.append("snapshot_checksum_missing_or_invalid")
        context = snapshot.get("outcome_context")
        context = context if isinstance(context, dict) else {}
        profile = context.get("profile")
        profile = profile if isinstance(profile, dict) else {}
        if _timestamp(profile.get("captured_at")) is None:
            reasons.append("profile_time_missing")
        resume = profile.get("resume_content")
        if (
            not isinstance(resume, str)
            or not resume
            or fingerprint(resume) != profile.get("resume_content_sha256")
        ):
            reasons.append("resume_missing_or_invalid")
        facts = profile.get("profile_facts")
        if not isinstance(facts, list) or (
            facts and fingerprint(facts) != profile.get("profile_facts_sha256")
        ):
            reasons.append("profile_facts_missing_or_invalid")
        letter = context.get("letter_text")
        if (
            not isinstance(letter, str)
            or not letter
            or hashlib.sha256(letter.encode()).hexdigest() != context.get("letter_sha256")
        ):
            reasons.append("letter_missing_or_invalid")
        vacancy = context.get("vacancy")
        vacancy = vacancy if isinstance(vacancy, dict) else {}
        description = vacancy.get("description")
        if (
            not isinstance(description, str)
            or not description
            or hashlib.sha256(description.encode()).hexdigest() != vacancy.get("description_sha256")
        ):
            reasons.append("vacancy_missing_or_invalid")
        if reasons:
            issues.append({"event_id": event.get("id"), "reasons": reasons})
    return {
        "preparation_or_unidentified_intents": len(intents) - len(attempts),
        "total": len(attempts),
        "complete": len(attempts) - len(issues),
        "complete_percent": round(100 * (len(attempts) - len(issues)) / len(attempts), 2)
        if attempts
        else None,
        "issues": issues,
    }
