from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

type ResourceMode = Literal["normal", "gaming"]


@dataclass(frozen=True)
class ContainerLimits:
    nano_cpus: int
    memory: int
    memory_swap: int


GAMING_LIMITS = {
    "api": ContainerLimits(500_000_000, 2**30, 2**30),
    "db": ContainerLimits(500_000_000, 2**30, 2**30),
}


def background_affinity(allowed: int) -> int:
    if allowed <= 0:
        raise ValueError("Нет разрешённых логических процессоров")
    processors = [1 << index for index in range(allowed.bit_length()) if allowed & (1 << index)]
    return sum(processors[-2:])


def configure_process(mode: ResourceMode) -> dict[str, str | int]:
    if mode == "normal":
        return {"mode": mode}
    if os.name != "nt":
        raise RuntimeError("Игровой режим оконной части поддерживается в Windows")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessAffinityMask.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel.GetCurrentProcess()
    allowed, system = ctypes.c_size_t(), ctypes.c_size_t()
    if not kernel.GetProcessAffinityMask(handle, ctypes.byref(allowed), ctypes.byref(system)):
        raise ctypes.WinError(ctypes.get_last_error())
    mask = background_affinity(allowed.value)
    if not kernel.SetPriorityClass(handle, 0x00004000):  # BELOW_NORMAL_PRIORITY_CLASS
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.SetProcessAffinityMask(handle, mask):
        raise ctypes.WinError(ctypes.get_last_error())
    # Windows passes this affinity and the below-normal priority to child processes.
    return {
        "mode": mode,
        "priority": "below_normal",
        "affinity": mask,
        "logical_processors": mask.bit_count(),
    }


def _docker(root: Path, arguments: list[str]) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    if result.returncode:
        raise RuntimeError(
            "Настройка ресурсов контейнера завершилась ошибкой: " + result.stderr[:400]
        )
    return result.stdout.strip()


def _current_limits(root: Path, identifier: str) -> ContainerLimits:
    raw = json.loads(_docker(root, ["inspect", "--format", "{{json .HostConfig}}", identifier]))
    return ContainerLimits(int(raw["NanoCpus"]), int(raw["Memory"]), int(raw["MemorySwap"]))


def _update_limits(root: Path, identifier: str, limits: ContainerLimits) -> None:
    _docker(
        root,
        [
            "update",
            "--cpus",
            str(limits.nano_cpus / 1e9),
            "--memory",
            str(limits.memory),
            "--memory-swap",
            str(limits.memory_swap),
            identifier,
        ],
    )
    if _current_limits(root, identifier) != limits:
        raise RuntimeError("Docker не применил ограничения ресурсов Hugin")


def configure_containers(root: Path, state_path: Path, mode: ResourceMode) -> None:
    if mode == "normal" and not state_path.exists():
        return
    stored = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    selected: list[tuple[str, str, ContainerLimits]] = []
    for service, target in GAMING_LIMITS.items():
        identifier = _docker(root, ["compose", "ps", "--quiet", service])
        if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
            raise RuntimeError(f"Отсутствует единственный работающий контейнер: {service}")
        current = _current_limits(root, identifier)
        prior = stored.get(service)
        if mode == "gaming":
            if prior is None or prior.get("id") != identifier:
                original = asdict(current)
                # Docker uses -1 to explicitly restore unlimited swap with unlimited RAM.
                if original["memory"] == 0 and original["memory_swap"] == 0:
                    original["memory_swap"] = -1
                stored[service] = {"id": identifier, "original": original}
            selected.append((service, identifier, target))
        elif prior is not None and prior.get("id") == identifier:
            original_limits = ContainerLimits(**prior["original"])
            if current in (target, original_limits) or prior.get("restoring") is True:
                selected.append((service, identifier, original_limits))
                prior["restoring"] = True
            # A manually changed limit or a recreated container belongs to its new owner.
    if mode == "gaming" or selected:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        temporary.replace(state_path)
    for service, identifier, limits in selected:
        current = _current_limits(root, identifier)
        if mode == "normal" and (
            (limits.nano_cpus == 0 and current.nano_cpus != 0)
            or (limits.memory == 0 and current.memory != 0)
        ):
            # Docker update ignores zero CPU/RAM values. Recreate this service with
            # Compose defaults to remove a hard limit; its named data volume stays.
            _docker(
                root,
                ["compose", "up", "--detach", "--no-deps", "--force-recreate", "--wait", service],
            )
            identifier = _docker(root, ["compose", "ps", "--quiet", service])
            if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
                raise RuntimeError("Контейнер после возврата обычного режима недоступен")
            stored[service]["id"] = identifier
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(stored, indent=2), encoding="utf-8")
            temporary.replace(state_path)
        if _current_limits(root, identifier) != limits:
            _update_limits(root, identifier, limits)
    if mode == "normal":
        state_path.unlink(missing_ok=True)
