from __future__ import annotations

import json
import subprocess
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from hugin import runtime_resources


@pytest.mark.parametrize(
    ("allowed", "expected"), [(0b111111, 0b110000), (0b101010, 0b101000), (1, 1)]
)
def test_gaming_affinity_uses_last_two_allowed_processors(allowed: int, expected: int) -> None:
    assert runtime_resources.background_affinity(allowed) == expected


def test_empty_affinity_is_rejected() -> None:
    with pytest.raises(ValueError):
        runtime_resources.background_affinity(0)


class FakeDocker:
    def __init__(self) -> None:
        self.ids = {"api": "a" * 64, "db": "b" * 64}
        self.limits = {
            self.ids["api"]: {"NanoCpus": 0, "Memory": 0, "MemorySwap": -1},
            self.ids["db"]: {"NanoCpus": 2_000_000_000, "Memory": 2**31, "MemorySwap": 2**31},
        }
        self.updates: list[str] = []

    def run(self, command: list[str], **kwargs: Any) -> CompletedProcess[str]:
        if command[1:4] == ["compose", "ps", "--quiet"]:
            output = self.ids[command[4]]
        elif command[1:3] == ["compose", "up"]:
            identifier = self.ids[command[-1]]
            self.limits[identifier] = {"NanoCpus": 0, "Memory": 0, "MemorySwap": -1}
            output = identifier
        elif command[1] == "inspect":
            output = json.dumps(self.limits[command[-1]])
        elif command[1] == "update":
            identifier = command[-1]
            self.updates.append(identifier)
            self.limits[identifier] = {
                "NanoCpus": round(float(command[command.index("--cpus") + 1]) * 1e9),
                "Memory": int(command[command.index("--memory") + 1]),
                "MemorySwap": int(command[command.index("--memory-swap") + 1]),
            }
            output = identifier
        else:
            raise AssertionError(command)
        return CompletedProcess(command, 0, output, "")


def test_gaming_limits_are_scoped_and_normal_restores_previous_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker()
    prior = {key: dict(value) for key, value in docker.limits.items()}
    monkeypatch.setattr(subprocess, "run", docker.run)
    state = tmp_path / "resources.json"

    runtime_resources.configure_containers(tmp_path, state, "gaming")
    assert docker.limits[docker.ids["api"]] == {
        "NanoCpus": 500_000_000,
        "Memory": 2**30,
        "MemorySwap": 2**30,
    }
    assert docker.limits[docker.ids["db"]] == {
        "NanoCpus": 500_000_000,
        "Memory": 2**30,
        "MemorySwap": 2**30,
    }
    runtime_resources.configure_containers(tmp_path, state, "gaming")
    runtime_resources.configure_containers(tmp_path, state, "normal")
    assert docker.limits == prior
    assert not state.exists()


def test_normal_mode_without_saved_limits_does_not_call_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("Docker"))
    runtime_resources.configure_containers(tmp_path, tmp_path / "absent.json", "normal")


def test_missing_service_never_updates_an_unrelated_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker()
    docker.ids["api"] = ""
    monkeypatch.setattr(subprocess, "run", docker.run)
    with pytest.raises(RuntimeError, match="контейнер"):
        runtime_resources.configure_containers(tmp_path, tmp_path / "state.json", "gaming")
    assert not docker.updates


def test_normal_mode_preserves_manual_change_to_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker()
    monkeypatch.setattr(subprocess, "run", docker.run)
    state = tmp_path / "state.json"
    runtime_resources.configure_containers(tmp_path, state, "gaming")
    changed = {"NanoCpus": 900_000_000, "Memory": 2**30, "MemorySwap": 2**30}
    docker.limits[docker.ids["api"]] = changed

    runtime_resources.configure_containers(tmp_path, state, "normal")

    assert docker.limits[docker.ids["api"]] == changed
