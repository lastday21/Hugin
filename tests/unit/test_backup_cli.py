from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hugin import backup_cli
from hugin.core.settings import Settings
from hugin.services.backups import BackupRecord


def test_backup_cli_supports_create_list_verify_and_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup = tmp_path / "backup"
    record = BackupRecord(
        path=backup,
        created_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
        reason="manual",
        size_bytes=100,
        verified_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
    )
    calls: list[tuple[object, ...]] = []

    @contextmanager
    def restore_lock() -> Iterator[None]:
        calls.append(("lock",))
        yield

    class Service:
        def __init__(self, _settings: Settings) -> None:
            pass

        def create(self, reason: str) -> BackupRecord:
            calls.append(("create", reason))
            return record

        def list(self) -> tuple[BackupRecord, ...]:
            calls.append(("list",))
            return (record, record.__class__(backup, record.created_at, "daily", 50, None))

        def verify(self, path: Path) -> BackupRecord:
            calls.append(("verify", path))
            return record

        def restore(self, path: Path, *, confirmation: str) -> BackupRecord:
            calls.append(("restore", path, confirmation))
            return record

    monkeypatch.setattr(backup_cli, "get_settings", lambda: Settings(environment="test"))
    monkeypatch.setattr(backup_cli, "BackupService", Service)
    monkeypatch.setattr(backup_cli, "restoration_lock", restore_lock)

    assert backup_cli.main(["create", "--reason", "pre-update"]) == 0
    assert backup_cli.main(["list"]) == 0
    assert backup_cli.main(["verify", str(backup)]) == 0
    assert backup_cli.main(["restore", str(backup), "--confirm-database", "hugin"]) == 0
    output = capsys.readouterr().out
    assert "Создана и проверена" in output
    assert "не проверена" in output
    assert "Страховочная копия" in output
    assert calls == [
        ("create", "pre-update"),
        ("list",),
        ("verify", backup),
        ("lock",),
        ("restore", backup, "hugin"),
    ]


def test_backup_cli_requires_subcommand() -> None:
    with pytest.raises(SystemExit):
        backup_cli.main([])


def test_restoration_lock_rejects_running_desktop() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()

    with (
        backup_cli.restoration_lock(port),
        pytest.raises(RuntimeError, match="закройте окно"),
        backup_cli.restoration_lock(port),
    ):
        pass
