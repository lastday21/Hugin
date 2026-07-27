from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from hugin.adapters.postgres_backup import (
    DockerPostgresBackupAdapter,
    find_project_directory,
)
from hugin.core.settings import Settings

BACKUP_FORMAT_VERSION = 1
DATABASE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
BACKUP_REASONS = {"daily", "manual", "pre-update", "before-restore"}


class PostgresBackupAdapter(Protocol):
    def dump(self, database_name: str, database_user: str, destination: Path) -> None: ...

    def create_database(self, database_name: str, database_user: str) -> None: ...

    def drop_database(self, database_name: str, database_user: str) -> None: ...

    def restore(
        self,
        database_name: str,
        database_user: str,
        source: Path,
    ) -> None: ...

    def public_table_count(self, database_name: str, database_user: str) -> int: ...

    def stop_application(self) -> None: ...

    def start_application(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BackupRecord:
    path: Path
    created_at: datetime
    reason: str
    size_bytes: int
    verified_at: datetime | None


class BackupService:
    def __init__(
        self,
        settings: Settings,
        *,
        adapter: PostgresBackupAdapter | None = None,
        now: datetime | None = None,
    ) -> None:
        self._settings = settings
        self._adapter = adapter or DockerPostgresBackupAdapter(find_project_directory())
        self._now = now
        self._validate_identifier(settings.database_name, "название базы")
        self._validate_identifier(settings.database_user, "имя пользователя базы")

    @property
    def backup_dir(self) -> Path:
        return self._settings.data_dir / "backups"

    def create(
        self,
        reason: str,
        *,
        retention_days: int = 30,
        verify: bool = True,
    ) -> BackupRecord:
        selected_reason = reason.strip().lower()
        if selected_reason not in BACKUP_REASONS:
            raise ValueError("Неизвестная причина резервного копирования")
        if retention_days < 1:
            raise ValueError("Срок хранения должен быть положительным")

        now = self._selected_now()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        pending = self.backup_dir / f".pending-{uuid4().hex}"
        pending.mkdir()
        dump_path = pending / "database.dump"
        try:
            self._adapter.dump(
                self._settings.database_name,
                self._settings.database_user,
                dump_path,
            )
            if not dump_path.is_file() or dump_path.stat().st_size == 0:
                raise RuntimeError("PostgreSQL создал пустую резервную копию")
            config_path = pending / "configuration.json"
            self._write_json(config_path, self._public_configuration())
            manifest = {
                "format_version": BACKUP_FORMAT_VERSION,
                "created_at": now.isoformat(),
                "reason": selected_reason,
                "database_file": dump_path.name,
                "configuration_file": config_path.name,
                "database_sha256": self._checksum(dump_path),
                "database_size_bytes": dump_path.stat().st_size,
                "verified_at": None,
                "public_tables": None,
            }
            self._write_json(pending / "manifest.json", manifest)
            final = self.backup_dir / (
                f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{selected_reason}-{uuid4().hex[:8]}"
            )
            pending.replace(final)
            record = self._read_record(final)
            if verify:
                record = self.verify(final, verified_at=now)
            self.prune(retention_days, now=now)
            return record
        except Exception:
            if pending.exists():
                shutil.rmtree(pending)
            raise

    def ensure_daily(self, *, retention_days: int = 30) -> BackupRecord | None:
        now = self._selected_now()
        recent = next(
            (
                backup
                for backup in self.list()
                if backup.verified_at is not None and backup.created_at >= now - timedelta(hours=24)
            ),
            None,
        )
        if recent is not None:
            self.prune(retention_days, now=now)
            return None
        return self.create("daily", retention_days=retention_days)

    def list(self) -> tuple[BackupRecord, ...]:
        if not self.backup_dir.is_dir():
            return ()
        records: list[BackupRecord] = []
        for path in self.backup_dir.iterdir():
            if not path.is_dir() or path.name.startswith(".pending-"):
                continue
            try:
                records.append(self._read_record(path))
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                continue
        records.sort(key=lambda item: item.created_at, reverse=True)
        return tuple(records)

    def verify(
        self,
        backup_path: Path,
        *,
        verified_at: datetime | None = None,
    ) -> BackupRecord:
        path, manifest, dump_path = self._validated_files(backup_path)
        temporary_database = f"hugin_restore_check_{uuid4().hex[:16]}"
        self._adapter.create_database(
            temporary_database,
            self._settings.database_user,
        )
        try:
            self._adapter.restore(
                temporary_database,
                self._settings.database_user,
                dump_path,
            )
            public_tables = self._adapter.public_table_count(
                temporary_database,
                self._settings.database_user,
            )
            if public_tables < 0:
                raise RuntimeError("PostgreSQL не подтвердил структуру восстановленной базы")
        finally:
            self._adapter.drop_database(
                temporary_database,
                self._settings.database_user,
            )
        timestamp = verified_at or self._selected_now()
        manifest["verified_at"] = timestamp.isoformat()
        manifest["public_tables"] = public_tables
        self._write_json(path / "manifest.json", manifest)
        return self._read_record(path)

    def restore(self, backup_path: Path, *, confirmation: str) -> BackupRecord:
        if confirmation != self._settings.database_name:
            raise ValueError("Для восстановления укажите точное название базы")
        path, _manifest, dump_path = self._validated_files(backup_path)
        self.verify(path)
        safety = self.create("before-restore")
        safety_dump = safety.path / "database.dump"
        self._adapter.stop_application()
        try:
            try:
                self._replace_database(dump_path)
                self._adapter.public_table_count(
                    self._settings.database_name,
                    self._settings.database_user,
                )
            except Exception as error:
                try:
                    self._replace_database(safety_dump)
                except Exception as rollback_error:
                    raise RuntimeError(
                        "Восстановление не удалось; не удалось вернуть и страховочную копию"
                    ) from rollback_error
                raise RuntimeError(
                    "Восстановление не удалось; исходная база возвращена из страховочной копии"
                ) from error
        finally:
            self._adapter.start_application()
        return safety

    def prune(self, retention_days: int, *, now: datetime | None = None) -> int:
        if retention_days < 1:
            raise ValueError("Срок хранения должен быть положительным")
        selected_at = now or self._selected_now()
        threshold = selected_at - timedelta(days=retention_days)
        removed = 0
        for record in self.list():
            if record.created_at < threshold:
                shutil.rmtree(record.path)
                removed += 1
        return removed

    def _replace_database(self, dump_path: Path) -> None:
        self._adapter.drop_database(
            self._settings.database_name,
            self._settings.database_user,
        )
        self._adapter.create_database(
            self._settings.database_name,
            self._settings.database_user,
        )
        self._adapter.restore(
            self._settings.database_name,
            self._settings.database_user,
            dump_path,
        )

    def _validated_files(
        self,
        backup_path: Path,
    ) -> tuple[Path, dict[str, object], Path]:
        path = backup_path.resolve()
        manifest_path = path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Некорректное описание резервной копии")
        if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
            raise ValueError("Неподдерживаемый формат резервной копии")
        database_file = manifest.get("database_file")
        expected_checksum = manifest.get("database_sha256")
        if not isinstance(database_file, str) or Path(database_file).name != database_file:
            raise ValueError("Некорректное имя файла базы")
        if not isinstance(expected_checksum, str) or len(expected_checksum) != 64:
            raise ValueError("Некорректная контрольная сумма резервной копии")
        dump_path = path / database_file
        if not dump_path.is_file() or self._checksum(dump_path) != expected_checksum:
            raise RuntimeError("Резервная копия повреждена")
        return path, manifest, dump_path

    def _read_record(self, path: Path) -> BackupRecord:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise TypeError("Некорректное описание резервной копии")
        verified = manifest.get("verified_at")
        return BackupRecord(
            path=path,
            created_at=datetime.fromisoformat(str(manifest["created_at"])),
            reason=str(manifest["reason"]),
            size_bytes=int(manifest["database_size_bytes"]),
            verified_at=datetime.fromisoformat(str(verified)) if verified else None,
        )

    def _public_configuration(self) -> dict[str, object]:
        return {
            "app_name": self._settings.app_name,
            "environment": self._settings.environment,
            "api_host": self._settings.api_host,
            "api_port": self._settings.api_port,
            "desktop_api_url": self._settings.desktop_api_url,
            "database_host": self._settings.database_host,
            "database_port": self._settings.database_port,
            "database_name": self._settings.database_name,
            "database_user": self._settings.database_user,
            "database_connect_timeout": self._settings.database_connect_timeout,
            "hh_login_url": self._settings.hh_login_url,
            "hh_resumes_url": self._settings.hh_resumes_url,
            "hh_search_url": self._settings.hh_search_url,
            "hh_browser_timeout_ms": self._settings.hh_browser_timeout_ms,
            "yandex_ai_model": self._settings.yandex_ai_model,
            "yandex_ai_base_url": self._settings.yandex_ai_base_url,
            "secrets_included": False,
        }

    def _selected_now(self) -> datetime:
        value = self._now or datetime.now(UTC)
        if value.tzinfo is None:
            raise ValueError("Время резервной копии должно содержать часовой пояс")
        return value.astimezone(UTC)

    @staticmethod
    def _checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _validate_identifier(value: str, label: str) -> None:
        if DATABASE_IDENTIFIER.fullmatch(value) is None:
            raise ValueError(f"Некорректное {label}")
