from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import hugin.notification_runtime as runtime
from hugin.adapters.notification_credentials import NotificationGatewayCredentials
from hugin.adapters.notification_gateway import (
    NotificationGatewayClient,
    NotificationGatewayConnectionError,
    NotificationGatewayStatus,
)
from hugin.core.settings import Settings
from hugin.diagnostics import OperationJournal


def configured_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Settings, Path, list[list[str]]]:
    project = tmp_path / "Hugin"
    project.mkdir()
    gateway = tmp_path / "Hugin_tg_bot"
    gateway.mkdir()
    (gateway / "compose.yaml").write_text("services: {}", encoding="utf-8")
    monkeypatch.setattr(
        runtime,
        "load_notification_gateway_credentials",
        lambda _path: NotificationGatewayCredentials("s" * 32),
    )
    calls: list[list[str]] = []

    def compose(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", compose)
    return Settings(environment="test", data_dir=tmp_path / "data"), project, calls


@pytest.mark.parametrize("missing", ["credentials", "compose", "local_url"])
def test_recovery_requires_existing_configured_local_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    settings, project, calls = configured_runtime(tmp_path, monkeypatch)
    if missing == "credentials":
        monkeypatch.setattr(runtime, "load_notification_gateway_credentials", lambda _path: None)
    elif missing == "compose":
        (project.parent / "Hugin_tg_bot" / "compose.yaml").unlink()
    else:
        settings = settings.model_copy(
            update={"notification_gateway_url": "https://notifications.example.org"}
        )
    assert not runtime.ensure_notification_gateway_running(settings, project_directory=project)
    assert calls == []


def test_recovery_leaves_running_gateway_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, project, calls = configured_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(NotificationGatewayClient, "status", lambda _self: status(True))
    assert runtime.ensure_notification_gateway_running(settings, project_directory=project)
    assert calls == []


def status(available: bool) -> NotificationGatewayStatus:
    return NotificationGatewayStatus(available, True, True, True)


def test_recovery_starts_only_existing_service_and_limits_repeated_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, project, calls = configured_runtime(tmp_path, monkeypatch)
    clock = [0.0]
    probes = [0]

    def probe(_client: object) -> NotificationGatewayStatus:
        probes[0] += 1
        if len(calls) == 0:
            raise NotificationGatewayConnectionError("unavailable")
        return status(True)

    monkeypatch.setattr(runtime, "monotonic", lambda: clock[0])
    monkeypatch.setattr(NotificationGatewayClient, "status", probe)
    recovery = runtime.NotificationGatewayRecovery(settings, project_directory=project)
    assert recovery.ensure_running()
    assert not recovery.ensure_running()
    assert probes == [2]
    assert calls == [
        [
            "docker",
            "compose",
            "--file",
            str(project.parent / "Hugin_tg_bot" / "compose.yaml"),
            "--project-directory",
            str(project.parent / "Hugin_tg_bot"),
            "up",
            "--detach",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "20",
            "hugin-tg-bot",
        ]
    ]
    clock[0] = 300.0
    assert recovery.ensure_running()
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "unavailable"])
def test_optional_recovery_failure_is_bounded_and_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    settings, project, _calls = configured_runtime(tmp_path, monkeypatch)

    def unavailable(_client: object) -> NotificationGatewayStatus:
        raise NotificationGatewayConnectionError("unavailable")

    def fail(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        assert options["timeout"] == 30
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        if failure == "unavailable":
            raise OSError("docker unavailable")
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(NotificationGatewayClient, "status", unavailable)
    monkeypatch.setattr(subprocess, "run", fail)
    journal = OperationJournal(settings.data_dir)
    assert not runtime.ensure_notification_gateway_running(
        settings, project_directory=project, journal=journal
    )
    entries = list(journal.entries(component="notifications"))
    assert entries[-1]["status"] == "failed"
