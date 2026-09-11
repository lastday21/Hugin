from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
AREAS = {
    "diagnostics": [
        "test_operation_timing",
        "test_trace_quality",
        "test_journal_storage_boundaries",
        "test_startup_native",
        "test_startup_recovery",
        "test_operation_evidence",
        "test_model_failure_evidence",
        "test_diagnostics",
        "test_diagnostic_independent_review",
        "test_diagnostic_resilience",
        "test_journal_cli",
        "test_codex_cli",
        "test_yandex_ai",
    ],
    "ranking": [
        "test_semantic_selection",
        "test_semantic_snapshot",
        "test_semantic_analyzer",
        "test_semantic_cache",
        "test_semantic_ranking",
        "test_semantic_processing",
        "test_semantic_routing_conflict",
        "test_semantic_cli",
        "test_vacancy_analysis",
        "test_vacancy_collection",
        "test_vacancy_administration",
        "test_vacancy_fit",
        "test_vacancy_duplicates",
        "test_requirement_sections",
    ],
    "letters": [
        "test_numeric_claims",
        "test_cover_letter",
        "test_resume_numeric_independent_review",
        "test_resume_numeric_evidence",
        "test_resume_improvement",
    ],
    "workers": [
        "test_background_processes",
        "test_process_worker",
        "test_incremental_search",
        "test_model_turn",
        "test_reply_worker",
        "test_automation_scheduler",
        "test_automation_worker",
        "test_automation_worker_unit",
        "test_semantic_processing",
        "test_semantic_routing_conflict",
        "test_worker_shutdown",
        "test_worker_recovery",
        "test_application_worker_stop_boundary",
        "test_application_result_independent_review",
        "test_hh_submission_evidence",
        "test_hh_submission_independent_review",
        "test_hh_sync_worker_reliability",
    ],
}


def fingerprints() -> dict[str, str]:
    paths = [
        path
        for directory in ("src", "tests", "web/src", "tools")
        for path in (ROOT / directory).rglob("*")
        if path.is_file() and path.suffix in {".py", ".ts", ".tsx", ".css", ".html", ".js"}
    ]
    paths.extend(ROOT / name for name in ("pyproject.toml", "uv.lock", "web/package-lock.json"))
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Сохранить проверяемый результат местных проверок")
    parser.add_argument("area", choices=[*AREAS, "all"])
    args = parser.parse_args()
    output = ROOT / ".work/checks" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True)
    python = sys.executable
    commands = []
    if args.area == "all":
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError("npm не найден")
        commands.extend(
            [
                [python, "-m", "ruff", "check", "src", "tests", "tools"],
                [python, "-m", "ruff", "format", "--check", "src", "tests", "tools"],
                [python, "-m", "mypy", "src", "tests"],
                [npm, "run", "check", "--prefix", "web"],
            ]
        )
        tests = [
            "--cov=hugin",
            "--cov-branch",
            f"--cov-report=json:{output / 'coverage.json'}",
            "--cov-report=term-missing",
        ]
    else:
        tests = [f"tests/unit/{name}.py" for name in AREAS[args.area]] + ["--no-cov"]
    commands.append([python, "-m", "pytest", *tests, "-q", f"--junitxml={output / 'tests.xml'}"])
    before = fingerprints()
    results = []
    for index, command in enumerate(commands):
        started = monotonic()
        path = output / f"{index + 1}.log"
        print(f"Проверка {index + 1}/{len(commands)}: {path}", flush=True)
        with path.open("w", encoding="utf-8") as stream:
            result = subprocess.run(
                command,
                cwd=ROOT,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            )
        results.append(
            {
                "command": command,
                "exit_code": result.returncode,
                "seconds": round(monotonic() - started, 3),
            }
        )
        if result.returncode:
            break
    after = fingerprints()
    report = {
        "area": args.area,
        "python": sys.version,
        "source_before": before,
        "source_after": after,
        "unchanged": before == after,
        "commands": results,
        "ok": before == after and all(row["exit_code"] == 0 for row in results),
    }
    (output / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Результат: {output / 'result.json'}", flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
