from __future__ import annotations

import argparse
import socket
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from hugin.core.settings import get_settings
from hugin.services.backups import BackupService


@contextmanager
def restoration_lock(port: int = 47631) -> Iterator[None]:
    instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        instance_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_EXCLUSIVEADDRUSE,
            1,
        )
    try:
        instance_socket.bind(("127.0.0.1", port))
    except OSError as error:
        instance_socket.close()
        raise RuntimeError("Перед восстановлением закройте окно Hugin") from error
    try:
        yield
    finally:
        instance_socket.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hugin-backup")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="создать и проверить резервную копию")
    create.add_argument(
        "--reason",
        choices=("daily", "manual", "pre-update"),
        default="manual",
    )

    subparsers.add_parser("list", help="показать доступные резервные копии")

    verify = subparsers.add_parser("verify", help="проверить восстановление копии")
    verify.add_argument("path", type=Path)

    restore = subparsers.add_parser("restore", help="восстановить базу из копии")
    restore.add_argument("path", type=Path)
    restore.add_argument(
        "--confirm-database",
        required=True,
        help="точное название заменяемой базы",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    service = BackupService(get_settings())
    if arguments.command == "create":
        record = service.create(arguments.reason)
        print(f"Создана и проверена: {record.path}")
    elif arguments.command == "list":
        for record in service.list():
            status = "проверена" if record.verified_at else "не проверена"
            print(
                f"{record.created_at.isoformat()} | {record.reason} | "
                f"{record.size_bytes} байт | {status} | {record.path}"
            )
    elif arguments.command == "verify":
        record = service.verify(arguments.path)
        print(f"Восстановление проверено: {record.path}")
    else:
        with restoration_lock():
            safety = service.restore(
                arguments.path,
                confirmation=arguments.confirm_database,
            )
        print(f"База восстановлена. Страховочная копия прежнего состояния: {safety.path}")
    return 0
