from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hugin.adapters.postgres_backup import (
    DockerPostgresBackupAdapter,
    find_project_directory,
)


def test_adapter_compares_archive_and_database_table_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "database.dump"
    source.write_bytes(b"PGDMP")
    commands: list[list[str]] = []

    def run(command: list[str], **values: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if "pg_restore" in command:
            assert values["stdin"] == subprocess.DEVNULL
            assert command[-1].startswith("/tmp/hugin-backup-")
            output = (
                b"; Archive created at 2026-09-11\n"
                b"219; 1259 16385 TABLE public applications hugin\n"
                b"220; 1259 16386 TABLE public table with spaces hugin\n"
                b"221; 1259 16387 TABLE another ignored hugin\n"
                b"222; 0 16385 TABLE DATA public applications hugin\n"
                b"223; 1259 16388 SEQUENCE public applications_id_seq hugin\n"
            )
        elif "psql" in command:
            output = b'["applications", "table with spaces"]\n'
        else:
            output = b""
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr(subprocess, "run", run)
    adapter = DockerPostgresBackupAdapter(tmp_path)

    assert adapter.archive_public_table_names(source) == ("applications", "table with spaces")
    assert adapter.public_table_names("temporary", "hugin") == (
        "applications",
        "table with spaces",
    )
    assert any("--list" in command for command in commands)
    assert "temporary" in commands[-1]
    assert commands[0][-2] == str(source)


@pytest.mark.parametrize("output", [b"invalid", b"null", b"{}", b"[42]", b'[""]'])
def test_adapter_rejects_invalid_table_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_values: subprocess.CompletedProcess(command, 0, output, b""),
    )
    with pytest.raises(RuntimeError, match="таблиц"):
        DockerPostgresBackupAdapter(tmp_path).public_table_names("temporary", "hugin")


def test_adapter_runs_dump_restore_database_and_application_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}", encoding="utf-8")
    commands: list[list[str]] = []
    archives: dict[str, bytes] = {}
    dump = tmp_path / "backup" / "database.dump"

    def run(command: list[str], **values: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        assert values["stdin"] == subprocess.DEVNULL
        assert isinstance(values["timeout"], (int, float)) and values["timeout"] > 0
        if "pg_dump" in command:
            archives[command[-1]] = b"dump"
            output = b""
        elif "cp" in command:
            source, destination = command[-2:]
            if source.startswith("db:"):
                Path(destination).write_bytes(archives[source.removeprefix("db:")])
            else:
                archives[destination.removeprefix("db:")] = Path(source).read_bytes()
            output = b""
        elif "rm" in command:
            del archives[command[-1]]
            output = b""
        elif "pg_restore" in command:
            assert archives[command[-1]] == b"dump"
            output = b""
        elif "psql" in command:
            output = b" 7\r\n"
        else:
            output = b""
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr(subprocess, "run", run)
    adapter = DockerPostgresBackupAdapter(tmp_path)

    adapter.dump("hugin", "hugin", dump)
    adapter.create_database("temporary", "hugin")
    adapter.restore("temporary", "hugin", dump)
    assert adapter.public_table_count("temporary", "hugin") == 7
    adapter.drop_database("temporary", "hugin")
    adapter.stop_application()
    adapter.start_application()

    assert dump.read_bytes() == b"dump"
    assert not archives
    postgres_commands = [command for command in commands if "exec" in command]
    assert all("--interactive=false" in command for command in postgres_commands)
    assert all("timeout" in command for command in postgres_commands)
    assert "--compress=1" in commands[0]
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
    is_file = Path.is_file
    local_compose_files = {root / "compose.yaml", configured / "compose.yaml"}
    monkeypatch.setattr(Path, "is_file", lambda path: path in local_compose_files and is_file(path))
    monkeypatch.setenv("HUGIN_PROJECT_DIR", str(configured))
    assert find_project_directory(tmp_path / "missing") == configured
    monkeypatch.setenv("HUGIN_PROJECT_DIR", str(tmp_path / "missing-configured"))
    with pytest.raises(RuntimeError, match=r"compose\.yaml"):
        find_project_directory(tmp_path / "missing")
    monkeypatch.delenv("HUGIN_PROJECT_DIR")
    with pytest.raises(RuntimeError, match=r"compose\.yaml"):
        find_project_directory(tmp_path / "missing")


@pytest.mark.parametrize("operation", ["dump", "list", "restore"])
def test_adapter_cleans_only_its_archive_after_transfer_or_postgres_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    source = tmp_path / "database.dump"
    source.write_bytes(b"original archive")
    remote_paths: list[str] = []
    removed: list[str] = []

    def run(command: list[str], **_values: object) -> subprocess.CompletedProcess[bytes]:
        if "cp" in command:
            remote_paths.append(command[-1].removeprefix("db:"))
            return subprocess.CompletedProcess(command, 1, b"", b"transfer failed")
        if "pg_dump" in command:
            remote_paths.append(command[-1])
            return subprocess.CompletedProcess(command, 1, b"", b"dump failed")
        if "rm" in command:
            removed.append(command[-1])
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    adapter = DockerPostgresBackupAdapter(tmp_path)
    with pytest.raises(RuntimeError, match="failed"):
        if operation == "dump":
            adapter.dump("hugin", "hugin", tmp_path / "failed.dump")
        elif operation == "list":
            adapter.archive_public_table_names(source)
        else:
            adapter.restore("temporary", "hugin", source)

    assert source.read_bytes() == b"original archive"
    assert removed == remote_paths
    assert len(removed) == 1
    assert removed[0].startswith("/tmp/hugin-backup-")


def test_adapter_limits_waiting_and_preserves_timeout_if_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed: list[str] = []

    def run(command: list[str], **values: object) -> subprocess.CompletedProcess[bytes]:
        if "rm" in command:
            removed.append(command[-1])
            return subprocess.CompletedProcess(command, 1, b"", b"cleanup failed")
        timeout = values["timeout"]
        assert isinstance(timeout, (int, float)) and 0 < timeout <= 360
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError, match="время ожидания") as raised:
        DockerPostgresBackupAdapter(tmp_path).dump("hugin", "hugin", tmp_path / "dump")

    assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)
    assert len(removed) == 1
    assert any("cleanup failed" in note for note in raised.value.__notes__)
