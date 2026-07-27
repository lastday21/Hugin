from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hugin.adapters.postgres_backup import (
    DockerPostgresBackupAdapter,
    find_project_directory,
)


def test_adapter_runs_dump_restore_database_and_application_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}", encoding="utf-8")
    commands: list[list[str]] = []

    def run(command: list[str], **values: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        stdout = values["stdout"]
        if "pg_dump" in command:
            assert hasattr(stdout, "write")
            stdout.write(b"dump")
            output = b""
        elif "psql" in command:
            output = b" 7\r\n"
        else:
            output = b""
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr(subprocess, "run", run)
    adapter = DockerPostgresBackupAdapter(tmp_path)
    dump = tmp_path / "backup" / "database.dump"

    adapter.dump("hugin", "hugin", dump)
    adapter.create_database("temporary", "hugin")
    adapter.restore("temporary", "hugin", dump)
    assert adapter.public_table_count("temporary", "hugin") == 7
    adapter.drop_database("temporary", "hugin")
    adapter.stop_application()
    adapter.start_application()

    assert dump.read_bytes() == b"dump"
    assert commands[0][:6] == ["docker", "compose", "exec", "-T", "db", "pg_dump"]
    assert commands[-2] == ["docker", "compose", "stop", "api"]
    assert commands[-1] == ["docker", "compose", "up", "--detach", "--wait", "api"]


def test_adapter_reports_command_and_query_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DockerPostgresBackupAdapter(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_values: subprocess.CompletedProcess(
            command,
            1,
            b"",
            b"postgres failed",
        ),
    )
    with pytest.raises(RuntimeError, match="postgres failed"):
        adapter.create_database("temporary", "hugin")

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_values: subprocess.CompletedProcess(
            command,
            0,
            b"not-a-number",
            b"",
        ),
    )
    with pytest.raises(RuntimeError, match="не подтвердил"):
        adapter.public_table_count("temporary", "hugin")


def test_find_project_directory_uses_parent_or_configured_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    child = root / "nested"
    child.mkdir(parents=True)
    (root / "compose.yaml").write_text("services: {}", encoding="utf-8")
    assert find_project_directory(child) == root

    configured = tmp_path / "configured"
    configured.mkdir()
    (configured / "compose.yaml").write_text("services: {}", encoding="utf-8")
    monkeypatch.setenv("HUGIN_PROJECT_DIR", str(configured))
    assert find_project_directory(tmp_path / "missing") == configured
    monkeypatch.setenv("HUGIN_PROJECT_DIR", str(tmp_path / "missing-configured"))
    with pytest.raises(RuntimeError, match=r"compose\.yaml"):
        find_project_directory(tmp_path / "missing")
    monkeypatch.delenv("HUGIN_PROJECT_DIR")
    with pytest.raises(RuntimeError, match=r"compose\.yaml"):
        find_project_directory(tmp_path / "missing")
