from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import IO, Any


class DockerPostgresBackupAdapter:
    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir.resolve()

    def dump(self, database_name: str, database_user: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as output:
            self._run_postgres(
                [
                    "pg_dump",
                    "--username",
                    database_user,
                    "--dbname",
                    database_name,
                    "--format=custom",
                    "--compress=6",
                    "--no-owner",
                    "--no-acl",
                    "--no-password",
                ],
                stdout=output,
            )

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
        with source.open("rb") as backup:
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
                ],
                stdin=backup,
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

    def stop_application(self) -> None:
        self._run_compose(["stop", "api"])

    def start_application(self) -> None:
        self._run_compose(["up", "--detach", "--wait", "api"])

    def _run_postgres(
        self,
        arguments: list[str],
        *,
        stdin: IO[Any] | None = None,
        stdout: IO[Any] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return self._run_compose(
            ["exec", "-T", "db", *arguments],
            stdin=stdin,
            stdout=stdout,
        )

    def _run_compose(
        self,
        arguments: list[str],
        *,
        stdin: IO[Any] | None = None,
        stdout: IO[Any] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        selected_stdout: int | IO[Any] = stdout if stdout is not None else subprocess.PIPE
        result: subprocess.CompletedProcess[bytes] = subprocess.run(
            ["docker", "compose", *arguments],
            cwd=self._project_dir,
            check=False,
            stdin=stdin,
            stdout=selected_stdout,
            stderr=subprocess.PIPE,
            text=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
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
