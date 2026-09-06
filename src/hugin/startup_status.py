from __future__ import annotations

import sys
import threading
import tkinter
from queue import Empty, Queue
from tkinter import ttk
from typing import BinaryIO

CLOSE_COMMAND = "__HUGIN_CLOSE__"


def read_status_messages(stream: BinaryIO, messages: Queue[str | None]) -> None:
    try:
        for line in stream:
            message = line.decode("utf-8").rstrip("\r\n")
            if message == CLOSE_COMMAND:
                return
            messages.put(message)
    finally:
        messages.put(None)


def main() -> None:
    messages: Queue[str | None] = Queue()

    reader = threading.Thread(
        target=read_status_messages,
        args=(sys.stdin.buffer, messages),
        name="hugin-startup-status-reader",
        daemon=True,
    )
    reader.start()

    root = tkinter.Tk()
    root.title("Hugin запускается")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.protocol("WM_DELETE_WINDOW", lambda: None)

    frame = ttk.Frame(root, padding=24)
    frame.grid(row=0, column=0, sticky="nsew")
    label = ttk.Label(
        frame,
        text="Погодите, Hugin запускается…",
        width=54,
        anchor="center",
        justify="center",
    )
    label.grid(row=0, column=0, pady=(0, 16))
    progress = ttk.Progressbar(frame, mode="indeterminate", length=380)
    progress.grid(row=1, column=0, sticky="ew")
    progress.start(12)

    root.update_idletasks()
    width = root.winfo_reqwidth()
    height = root.winfo_reqheight()
    left = max((root.winfo_screenwidth() - width) // 2, 0)
    top = max((root.winfo_screenheight() - height) // 2, 0)
    root.geometry(f"{width}x{height}+{left}+{top}")

    def poll_messages() -> None:
        latest: str | None = None
        should_close = False
        while True:
            try:
                item = messages.get_nowait()
            except Empty:
                break
            if item is None:
                should_close = True
            elif item:
                latest = item
        if latest is not None:
            label.configure(text=latest)
        if should_close:
            progress.stop()
            root.destroy()
            return
        root.after(100, poll_messages)

    root.after(0, poll_messages)
    root.mainloop()


if __name__ == "__main__":
    main()
