from __future__ import annotations

import argparse
from collections.abc import Sequence

from hugin.core.settings import get_settings
from hugin.database.schema import (
    check_database_schema,
    current_revision,
    downgrade_database,
    upgrade_database,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hugin-db")
    parser.add_argument("command", choices=("upgrade", "downgrade", "current", "check"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    settings = get_settings()

    if arguments.command == "upgrade":
        upgrade_database(settings)
    elif arguments.command == "downgrade":
        downgrade_database(settings)
    elif arguments.command == "current":
        print(current_revision(settings) or "base")
    else:
        check_database_schema(settings)
    return 0
