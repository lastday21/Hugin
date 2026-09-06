from __future__ import annotations

import json
import os
import subprocess
import sys
from io import BytesIO
from queue import Queue

from hugin.startup_status import CLOSE_COMMAND, read_status_messages


def test_status_pipe_preserves_russian_under_windows_encoding() -> None:
    message = "Погодите, запускается Docker Desktop…"
    command = (
        "import json, sys; from queue import Queue; "
        "from hugin.startup_status import read_status_messages; "
        "messages = Queue(); read_status_messages(sys.stdin.buffer, messages); "
        "print(json.dumps([messages.get(), messages.get()]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", command],
        input=f"{message}\n{CLOSE_COMMAND}\n".encode(),
        capture_output=True,
        check=True,
        timeout=10,
        env={**os.environ, "PYTHONIOENCODING": "cp1251", "PYTHONUTF8": "0"},
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    assert json.loads(result.stdout) == [message, None]


def test_status_reader_closes_on_eof_and_ignores_messages_after_close() -> None:
    for content in (b"ready\r\n", f"ready\n{CLOSE_COMMAND}\nlate\n".encode()):
        messages: Queue[str | None] = Queue()
        read_status_messages(BytesIO(content), messages)
        assert messages.get_nowait() == "ready"
        assert messages.get_nowait() is None
        assert messages.empty()
