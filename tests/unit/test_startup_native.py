from __future__ import annotations

import os
import sys
import tkinter
from types import SimpleNamespace
from typing import Any

import pytest

from hugin import startup_status


def test_native_startup_shows_russian_updates_and_closes_from_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "win32" and not os.environ.get("DISPLAY"):
        pytest.skip("Native Tk requires a display; use xvfb-run on Linux")
    root = tkinter.Tk()
    root.withdraw()
    reader, writer = os.pipe()
    stream = os.fdopen(reader, "rb")
    final_labels: list[str] = []
    final_titles: list[str] = []
    original_destroy = root.destroy
    closed = False

    def labels(widget: Any) -> list[str]:
        values = [str(widget.cget("text"))] if widget.winfo_class() == "TLabel" else []
        return values + [value for child in widget.winfo_children() for value in labels(child)]

    def destroy() -> None:
        final_titles.append(root.title())
        final_labels.extend(labels(root))
        original_destroy()

    def close() -> None:
        nonlocal closed
        if not closed:
            os.write(writer, f"{startup_status.CLOSE_COMMAND}\n".encode())
            os.close(writer)
            closed = True

    monkeypatch.setattr(tkinter, "Tk", lambda: root)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=stream))
    monkeypatch.setattr(root, "destroy", destroy)
    root.after(20, lambda: os.write(writer, "Подготовка базы данных…\n".encode()))
    root.after(170, lambda: os.write(writer, "Проверка завершена, приложение готово.\n".encode()))
    root.after(350, close)
    try:
        startup_status.main()
        assert final_titles == ["Hugin запускается"]
        assert final_labels == ["Проверка завершена, приложение готово."]
    finally:
        close()
        stream.close()
