from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from hugin.core.settings import Settings
from hugin.services.backups import BackupRecord, BackupService


class FakeBackupAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.fail_live_restore_once = False
        self.fail_all_live_restores = False
        self.empty_dump = False
        self.negative_table_count = False
        self.stop_error: Exception | None = None

    def dump(self, database_name: str, database_user: str, destination: Path) -> None:
        self.calls.append(("dump", database_name, database_user, destination))
        destination.write_bytes(b"" if self.empty_dump else b"PGDMP\nbackup-data")

    def create_database(self, database_name: str, database_user: str) -> None:
        self.calls.append(("create_database", database_name, database_user))

    def drop_database(self, database_name: str, database_user: str) -> None:
        self.calls.append(("drop_database", database_name, database_user))

    def restore(
        self,
        database_name: str,
        database_user: str,
        source: Path,
    ) -> None:
        self.calls.append(("restore", database_name, database_user, source))
        if database_name == "hugin" and (
            self.fail_live_restore_once or self.fail_all_live_restores
        ):
            self.fail_live_restore_once = False
            raise RuntimeError("restore failed")
        assert source.read_bytes().startswith(b"PGDMP")

    def public_table_count(self, database_name: str, database_user: str) -> int:
        self.calls.append(("count", database_name, database_user))
        return -1 if self.negative_table_count else 42

    def stop_application(self) -> None:
        self.calls.append(("stop",))
        if self.stop_error is not None:
            raise self.stop_error

    def start_application(self) -> None:
        self.calls.append(("start",))


def backup_settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        data_dir=tmp_path,
        database_name="hugin",
        database_user="hugin",
        database_password=SecretStr("database-secret"),
        yandex_ai_api_key=SecretStr("ai-secret"),
    )


def test_create_verifies_lists_and_skips_recent_daily_backup(tmp_path: Path) -> None:
    adapter = FakeBackupAdapter()
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    service = BackupService(
        backup_settings(tmp_path),
        adapter=adapter,
        now=now,
    )

    record = service.create("manual")

    assert record.reason == "manual"
    assert record.verified_at == now
    assert record.size_bytes > 0
    assert record.path.parent == tmp_path / "backups"
    manifest = json.loads((record.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["verified_at"] == now.isoformat()
    assert manifest["public_tables"] == 42
    configuration = (record.path / "configuration.json").read_text(encoding="utf-8")
    assert "database-secret" not in configuration
    assert "ai-secret" not in configuration
    assert '"secrets_included": false' in configuration
    assert service.list() == (record,)
    assert service.ensure_daily() is None
    assert [call[0] for call in adapter.calls] == [
        "dump",
        "create_database",
        "restore",
        "count",
        "drop_database",
    ]


def test_daily_backup_prunes_expired_records_and_ignores_broken_folders(
    tmp_path: Path,
) -> None:
    adapter = FakeBackupAdapter()
    settings = backup_settings(tmp_path)
    old_time = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    old_record = BackupService(settings, adapter=adapter, now=old_time).create(
        "daily",
        retention_days=90,
    )
    broken = tmp_path / "backups" / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{", encoding="utf-8")
    (tmp_path / "backups" / ".pending-leftover").mkdir()

    new_time = old_time + timedelta(days=31)
    service = BackupService(settings, adapter=adapter, now=new_time)
    new_record = service.ensure_daily(retention_days=30)

    assert isinstance(new_record, BackupRecord)
    assert new_record.reason == "daily"
    assert not old_record.path.exists()
    assert broken.exists()
    assert service.prune(30) == 0


def test_validation_rejects_bad_inputs_and_damaged_copy(tmp_path: Path) -> None:
    adapter = FakeBackupAdapter()
    settings = backup_settings(tmp_path)
    aware = datetime(2026, 7, 27, tzinfo=UTC)
    service = BackupService(settings, adapter=adapter, now=aware)

    with pytest.raises(ValueError, match="причина"):
        service.create("other")
    with pytest.raises(ValueError, match="Срок"):
        service.create("manual", retention_days=0)
    with pytest.raises(ValueError, match="Срок"):
        service.prune(0)
    with pytest.raises(ValueError, match="часовой пояс"):
        BackupService(
            settings,
            adapter=adapter,
            now=datetime(2026, 7, 27),
        ).create("manual")
    with pytest.raises(ValueError, match="название базы"):
        BackupService(
            settings.model_copy(update={"database_name": "bad-name"}),
            adapter=adapter,
        )

    assert BackupService(settings, adapter=adapter).list() == ()
    adapter.empty_dump = True
    with pytest.raises(RuntimeError, match="пустую"):
        service.create("manual")
    assert not any(path.name.startswith(".pending-") for path in service.backup_dir.iterdir())
    adapter.empty_dump = False

    unverified = service.create("manual", verify=False)
    assert unverified.verified_at is None

    record = service.create("manual")
    (record.path / "database.dump").write_bytes(b"damaged")
    with pytest.raises(RuntimeError, match="повреждена"):
        service.verify(record.path)

    manifest_path = record.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database_file"] = "../database.dump"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="имя файла"):
        service.verify(record.path)


def test_verification_rejects_bad_manifest_and_negative_table_result(
    tmp_path: Path,
) -> None:
    adapter = FakeBackupAdapter()
    service = BackupService(
        backup_settings(tmp_path),
        adapter=adapter,
        now=datetime(2026, 7, 27, tzinfo=UTC),
    )
    record = service.create("manual", verify=False)
    manifest_path = record.path / "manifest.json"

    manifest_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="описание"):
        service.verify(record.path)

    record = service.create("manual", verify=False)
    manifest_path = record.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["format_version"] = 999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="формат"):
        service.verify(record.path)

    record = service.create("manual", verify=False)
    manifest_path = record.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database_sha256"] = "bad"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="контрольная сумма"):
        service.verify(record.path)

    record = service.create("manual", verify=False)
    adapter.negative_table_count = True
    with pytest.raises(RuntimeError, match="структуру"):
        service.verify(record.path)
    assert adapter.calls[-1][0] == "drop_database"


def test_restore_replaces_database_and_keeps_safety_copy(tmp_path: Path) -> None:
    adapter = FakeBackupAdapter()
    service = BackupService(
        backup_settings(tmp_path),
        adapter=adapter,
        now=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
    )
    source = service.create("manual")
    adapter.calls.clear()

    with pytest.raises(ValueError, match="точное название"):
        service.restore(source.path, confirmation="wrong")
    safety = service.restore(source.path, confirmation="hugin")

    assert safety.reason == "before-restore"
    operations = [call[0] for call in adapter.calls]
    assert "stop" in operations
    assert operations[-1] == "start"
    stop_index = operations.index("stop")
    assert operations[stop_index + 1 : stop_index + 5] == [
        "drop_database",
        "create_database",
        "restore",
        "count",
    ]


def test_restore_rolls_back_after_failure_and_does_not_touch_database_if_stop_fails(
    tmp_path: Path,
) -> None:
    adapter = FakeBackupAdapter()
    service = BackupService(
        backup_settings(tmp_path),
        adapter=adapter,
        now=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
    )
    source = service.create("manual")
    adapter.calls.clear()
    adapter.fail_live_restore_once = True

    with pytest.raises(RuntimeError, match="исходная база возвращена"):
        service.restore(source.path, confirmation="hugin")
    assert [call[0] for call in adapter.calls].count("restore") == 4
    assert adapter.calls[-1] == ("start",)

    adapter.calls.clear()
    adapter.stop_error = RuntimeError("cannot stop")
    with pytest.raises(RuntimeError, match="cannot stop"):
        service.restore(source.path, confirmation="hugin")
    operations = [call[0] for call in adapter.calls]
    stop_index = operations.index("stop")
    assert "drop_database" not in operations[stop_index + 1 :]


def test_restore_reports_failure_if_safety_copy_cannot_be_returned(
    tmp_path: Path,
) -> None:
    adapter = FakeBackupAdapter()
    service = BackupService(
        backup_settings(tmp_path),
        adapter=adapter,
        now=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
    )
    source = service.create("manual")
    adapter.fail_all_live_restores = True

    with pytest.raises(RuntimeError, match="страховочную копию"):
        service.restore(source.path, confirmation="hugin")
    assert adapter.calls[-1] == ("start",)
