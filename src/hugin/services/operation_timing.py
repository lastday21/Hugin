from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

_TERMINAL = {"completed", "failed", "blocked", "skipped"}


def history_records(rows: list[dict[str, Any]], run_ids: set[str]) -> list[dict[str, Any]]:
    selected = set(run_ids)
    parents = {
        row["run_id"]: row.get("details", {}).get("parent_run_id")
        for row in rows
        if isinstance(row.get("run_id"), str)
    }
    while True:
        related = {
            identifier
            for child, parent in parents.items()
            if child in selected or parent in selected
            for identifier in (child, parent)
            if isinstance(identifier, str)
        }
        if related <= selected:
            break
        selected.update(related)
    return [row for row in rows if row.get("run_id") in selected]


def task_timing(
    task: dict[str, Any], rows: list[dict[str, Any]], *, now: datetime
) -> dict[str, Any]:
    closed = task["state"] in {"COMPLETED", "SKIPPED"}
    try:
        start = datetime.fromisoformat(task["created_at"])
        end = datetime.fromisoformat(task["updated_at"]) if closed else now
    except (TypeError, ValueError):
        return {"task_id": task["id"], "elapsed_ms": None, "issue": "invalid_task_dates"}
    # SQLite returns UTC database timestamps without the timezone marker.
    start = start.replace(tzinfo=UTC) if start.tzinfo is None else start
    end = end.replace(tzinfo=UTC) if end.tzinfo is None else end
    if end < start:
        return {"task_id": task["id"], "elapsed_ms": None, "issue": "invalid_task_dates"}
    selected = [row for row in rows if row.get("details", {}).get("task_id") == task["id"]]
    return {
        **timing_report(selected, since=start, until=end),
        "task_id": task["id"],
        "closed": closed,
        "start_basis": "task_created_at",
        "end_basis": "terminal_task_updated_at" if closed else "report_time",
    }


def _runs(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if isinstance(row.get("run_id"), str):
            result[row["run_id"]].append(row)
    return result


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def history_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    runs = _runs(rows)
    issues = []
    checked = 0
    for run_id, events in runs.items():
        starts = [row for row in events if row.get("status") == "started"]
        if len(starts) != 1:
            continue
        required = starts[0].get("details", {}).get("required_steps")
        if not isinstance(required, list) or not required:
            continue
        checked += 1
        missing = []
        for name in required:
            children = [
                child
                for child in runs.values()
                if child[0].get("details", {}).get("parent_run_id") == run_id
                and child[0].get("details", {}).get("step_name") == name
            ]
            if len(children) != 1 or (
                sum(row.get("status") == "started" for row in children[0]) != 1
                or sum(row.get("status") in _TERMINAL for row in children[0]) != 1
            ):
                missing.append(name)
        if missing:
            issues.append({"run_id": run_id, "missing_steps": missing})
    return {
        "checked_runs": checked,
        "complete": not issues if checked else None,
        "issues": issues,
        "scope": "declared_required_steps_only",
    }


def timing_report(
    rows: list[dict[str, Any]], *, since: datetime, until: datetime
) -> dict[str, Any]:
    if since.tzinfo is None or until.tzinfo is None or until < since:
        raise ValueError("Проверьте порядок дат и наличие часового пояса")
    intervals = []
    incomplete = []
    for run_id, events in _runs(rows).items():
        starts = [row for row in events if row.get("status") == "started"]
        ends = [row for row in events if row.get("status") in _TERMINAL]
        if not starts and all("duration_ms" not in row.get("details", {}) for row in events):
            continue
        if len(starts) != 1 or len(ends) != 1:
            open_before_end = (
                bool(starts)
                and not ends
                and any((stamp := _time(row.get("timestamp"))) and stamp <= until for row in starts)
            )
            if open_before_end or any(
                (stamp := _time(row.get("timestamp"))) and since <= stamp <= until for row in events
            ):
                incomplete.append(run_id)
            continue
        start, end = _time(starts[0].get("timestamp")), _time(ends[0].get("timestamp"))
        if start is None or end is None or end < start:
            incomplete.append(run_id)
            continue
        left, right = max(since, start), min(until, end)
        if right <= left:
            continue
        details = starts[0].get("details", {})
        lifetime = (
            starts[0].get("component") == "desktop"
            and starts[0].get("event") == "application.session"
        )
        lane = (starts[0].get("source"), starts[0].get("process_id"), starts[0].get("thread"))
        intervals.append(
            {
                "run_id": run_id,
                "event": starts[0].get("event"),
                "component": starts[0].get("component"),
                "start": left,
                "end": right,
                "lane": lane,
                "kind": "lifetime" if lifetime else details.get("timing_kind", "work"),
                "reason": details.get("reason"),
                "job_kind": details.get("job_kind"),
                "task_id": details.get("task_id"),
            }
        )
    points = sorted({since, until, *(item[key] for item in intervals for key in ("start", "end"))})
    work = wait = observed_wait = 0.0
    for left, right in pairwise(points):
        active = [item for item in intervals if item["start"] <= left and item["end"] >= right]
        waiting_lanes = {item["lane"] for item in active if item["kind"] == "wait"}
        working = any(
            item["kind"] == "work" and item["lane"] not in waiting_lanes for item in active
        )
        duration = (right - left).total_seconds() * 1000
        if waiting_lanes:
            observed_wait += duration
        if working:
            work += duration
        elif waiting_lanes:
            wait += duration
    elapsed = int((until - since).total_seconds() * 1000)
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "elapsed_ms": elapsed,
        "work_ms": int(work),
        "wait_ms": int(wait),
        "unclassified_ms": elapsed - int(work) - int(wait),
        "observed_wait_ms": round(observed_wait),
        "incomplete_runs": incomplete,
        "intervals": [
            {
                **{key: value for key, value in item.items() if key != "lane"},
                "start": item["start"].isoformat(),
                "end": item["end"].isoformat(),
            }
            for item in intervals
        ],
    }
