from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hugin import semantic_cli
from hugin.core.settings import Settings
from hugin.services.semantic_processing import SemanticSelectionProcessor
from tests.unit.test_semantic_processing import Client, seed

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("command", ["inspect", "analyze"])
def test_cli_reads_or_analyzes_without_preparing_queue(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    from sqlalchemy import func, select

    from hugin.database import create_database
    from hugin.database.models import ApplicationModel

    account_id, direction_id, vacancy_id, _, _ = seed(settings)
    monkeypatch.setattr(semantic_cli, "get_settings", lambda: settings)
    client = Client()
    processor = SemanticSelectionProcessor(settings, client_factory=lambda *_: client)
    monkeypatch.setattr(semantic_cli, "SemanticSelectionProcessor", lambda _: processor)
    output = tmp_path / "selection.json"
    assert (
        semantic_cli.main(
            [
                command,
                "--account-id",
                str(account_id),
                "--direction-id",
                str(direction_id),
                "--vacancy-id",
                str(vacancy_id),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == ("ALLOW" if command == "analyze" else "REVIEW")
    assert client.calls == (2 if command == "analyze" else 0)
    assert report["hh_actions"] == 0
    database = create_database(settings)
    try:
        with database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(ApplicationModel)) == 0
    finally:
        database.close()


@pytest.mark.parametrize("invalid_id,existing", [(True, False), (False, True)])
def test_cli_rejects_bad_arguments_before_loading_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_id: bool, existing: bool
) -> None:
    def forbidden() -> Any:
        raise AssertionError("Settings must not be loaded")

    monkeypatch.setattr(semantic_cli, "get_settings", forbidden)
    output = tmp_path / "selection.json"
    if existing:
        output.write_text("Saved report", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        semantic_cli.main(
            [
                "analyze",
                "--direction-id",
                "0" if invalid_id else "1",
                "--vacancy-id",
                "1",
                "--output",
                str(output),
            ]
        )
    assert error.value.code == 2
    if existing:
        assert output.read_text(encoding="utf-8") == "Saved report"
