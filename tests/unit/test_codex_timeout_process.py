from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import cast

import pytest

from hugin.adapters import codex_cli


@pytest.mark.parametrize(
    "launcher",
    ["direct", pytest.param("cmd", marks=pytest.mark.skipif(os.name != "nt", reason="Windows"))],
)
def test_timeout_finishes_when_child_inherits_output(tmp_path: Path, launcher: str) -> None:
    helper = tmp_path / "child_output.py"
    helper.write_text(
        "import subprocess, sys, time\n"
        "if '--child' not in sys.argv:\n"
        "    child = subprocess.Popen([sys.executable, __file__, '--child'])\n"
        "    from pathlib import Path\n"
        "    Path(__file__).with_suffix('.pid').write_text(str(child.pid))\n"
        "print('partial output', flush=True)\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    command = [sys.executable, str(helper)]
    if launcher == "cmd":
        wrapper = tmp_path / "codex.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{helper}"\n', encoding="utf-8")
        command = [str(wrapper)]
    started = monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        codex_cli.run_cli(
            command,
            input="",
            timeout=0.5,
            env=os.environ.copy(),
        )
    assert monotonic() - started < 3
    assert "partial output" in codex_cli.CodexCliClient._captured_text(caught.value.stdout)
    child_pid = int(helper.with_suffix(".pid").read_text())
    if os.name == "nt":
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        assert f'"{child_pid}"' not in listing


def test_cli_preserves_unicode_input_and_output(tmp_path: Path) -> None:
    helper = tmp_path / "echo_utf8.py"
    helper.write_text(
        "import sys\n"
        "data=sys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(data)\n"
        "sys.stderr.buffer.write('Диагностика'.encode('utf-8'))\n",
        encoding="utf-8",
    )
    prompt = "Подтверждённый опыт 🐍\n" * 5000
    result = codex_cli.run_cli(
        [sys.executable, str(helper)], input=prompt, timeout=5, env=os.environ.copy()
    )
    assert result.returncode == 0
    assert result.stdout == prompt
    assert result.stderr == "Диагностика"


@pytest.mark.parametrize("assignment_delay", [0, 0.3])
def test_cli_waits_for_output_after_parent_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, assignment_delay: float
) -> None:
    if os.name == "nt" and assignment_delay:
        import time

        original_assign = codex_cli._WindowsProcessJob.assign

        def delayed_assign(job: codex_cli._WindowsProcessJob, pid: int) -> None:
            time.sleep(assignment_delay)
            original_assign(job, pid)

        monkeypatch.setattr(codex_cli._WindowsProcessJob, "assign", delayed_assign)
    helper = tmp_path / "late_output.py"
    helper.write_text(
        "import subprocess, sys, time\n"
        "if '--child' in sys.argv:\n"
        "    time.sleep(0.4)\n"
        "    print('child result', flush=True)\n"
        "else:\n"
        "    subprocess.Popen([sys.executable, __file__, '--child'])\n"
        "    print('parent result', flush=True)\n",
        encoding="utf-8",
    )
    result = codex_cli.run_cli(
        [sys.executable, str(helper)], input="", timeout=3, env=os.environ.copy()
    )
    assert result.stdout.splitlines() == ["parent result", "child result"]


def test_interruption_stops_the_started_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = tmp_path / "interrupted.py"
    helper.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    original_wait = cast(
        Callable[[subprocess.Popen[bytes], float | None], int], subprocess.Popen.wait
    )
    started: list[subprocess.Popen[bytes]] = []

    def interrupted_wait(process: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        if not started:
            started.append(process)
            raise KeyboardInterrupt
        return original_wait(process, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupted_wait)
    with pytest.raises(KeyboardInterrupt):
        codex_cli.run_cli([sys.executable, str(helper)], input="", timeout=3, env=os.environ.copy())
    assert started[0].poll() is not None
