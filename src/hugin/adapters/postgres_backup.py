from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

POSTGRES_TIMEOUT_SECONDS = 300
DOCKER_TIMEOUT_SECONDS = POSTGRES_TIMEOUT_SECONDS + 30


class DockerPostgresBackupAdapter:
    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir.resolve()

    def dump(self, database_name: str, database_user: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._temporary_archive() as archive:
            self._run_postgres(
                [
                    "pg_dump",
                    "--username",
                    database_user,
                    "--dbname",
                    database_name,
                    "--format=custom",
                    "--compress=1",
                    "--no-owner",
                    "--no-acl",
                    "--no-password",
                    "--file",
                    archive,
                ],
            )
            self._run_compose(["cp", f"db:{archive}", str(destination.resolve())])

    def create_database(self, database_name: str, database_user: str) -> None:
        self._run_postgres(
            [
                "createdb",
                "--username",
                database_user,
                "--owner",
                database_user,
                "--no-password",
                database_name,
            ]
        )

    def drop_database(self, database_name: str, database_user: str) -> None:
        self._run_postgres(
            [
                "dropdb",
                "--username",
                database_user,
                "--if-exists",
                "--force",
                "--no-password",
                database_name,
            ]
        )

    def restore(
        self,
        database_name: str,
        database_user: str,
        source: Path,
    ) -> None:
        with self._temporary_archive(source) as archive:
            self._run_postgres(
                [
                    "pg_restore",
                    "--username",
                    database_user,
                    "--dbname",
                    database_name,
                    "--no-owner",
                    "--no-acl",
                    "--exit-on-error",
                    "--no-password",
                    archive,
                ],
            )

    def public_table_count(self, database_name: str, database_user: str) -> int:
        result = self._run_postgres(
            [
                "psql",
                "--username",
                database_user,
                "--dbname",
                database_name,
                "--tuples-only",
                "--no-align",
                "--no-password",
                "--command",
                ("SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public';"),
            ]
        )
        try:
            return int(result.stdout.strip())
        except ValueError as error:
            raise RuntimeError("PostgreSQL не подтвердил восстановление копии") from error

    def archive_public_table_names(self, source: Path) -> tuple[str, ...]:
        with self._temporary_archive(source) as archive:
            result = self._run_postgres(["pg_restore", "--list", archive])
        names = []
        for line in result.stdout.decode("utf-8").splitlines():
            match = re.fullmatch(r"\d+;\s+\d+\s+\d+\s+TABLE public (.+) \S+", line)
            if match is not None:
                names.append(match.group(1))
        return tuple(sorted(names))

    def public_table_names(self, database_name: str, database_user: str) -> tuple[str, ...]:
        result = self._run_postgres(
            [
                "psql",
                "--username",
                database_user,
                "--dbname",
                database_name,
                "--tuples-only",
                "--no-align",
                "--no-password",
                "--command",
                (
                    "SELECT COALESCE(json_agg(tablename ORDER BY tablename), '[]'::json) "
                    "FROM pg_catalog.pg_tables WHERE schemaname = 'public';"
                ),
            ]
        )
        try:
            names = json.loads(result.stdout)
            if not isinstance(names, list) or any(
                not isinstance(name, str) or not name for name in names
            ):
                raise ValueError("Некорректный состав таблиц")
        except (ValueError, UnicodeDecodeError) as error:
            raise RuntimeError("PostgreSQL не подтвердил состав таблиц") from error
        return tuple(names)

    def stop_application(self) -> None:
        self._run_compose(["stop", "api"])

    def start_application(self) -> None:
        self._run_compose(["up", "--detach", "--wait", "api"])

    @contextmanager
    def _temporary_archive(self, source: Path | None = None) -> Iterator[str]:
        archive = f"/tmp/hugin-backup-{uuid4().hex}.dump"
        try:
            if source is not None:
                self._run_compose(["cp", str(source.resolve()), f"db:{archive}"])
            yield archive
        finally:
            original_error = sys.exception()
            try:
                self._run_postgres(["rm", "-f", archive])
            except Exception as cleanup_error:
                if original_error is None:
                    raise
                original_error.add_note(f"Не удалось удалить временный архив: {cleanup_error}")

    def _run_postgres(self, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        # Ограничение внутри контейнера останавливает и сам PostgreSQL-процесс,
        # даже если Windows завершит только клиент Docker по внешнему тайм-ауту.
        return self._run_compose(
            [
                "exec",
                "-T",
                "--interactive=false",
                "db",
                "timeout",
                "--kill-after=10",
                str(POSTGRES_TIMEOUT_SECONDS),
                *arguments,
            ]
        )

    def _run_compose(
        self,
        arguments: list[str],
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result: subprocess.CompletedProcess[bytes] = subprocess.run(
                ["docker", "compose", *arguments],
                cwd=self._project_dir,
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=False,
                timeout=DOCKER_TIMEOUT_SECONDS,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Превышено время ожидания команды резервного копирования") from error
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(message[-500:] or "Команда PostgreSQL завершилась с ошибкой")
        return result


def find_project_directory(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "compose.yaml").is_file():
            return candidate
    configured = os.getenv("HUGIN_PROJECT_DIR")
    if configured:
        candidate = Path(configured).resolve()
        if (candidate / "compose.yaml").is_file():
            return candidate
    raise RuntimeError("Не найден compose.yaml для резервного копирования")
