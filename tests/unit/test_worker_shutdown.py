from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hugin.core.settings import Settings
from hugin.workers.applications import ApplicationWorker
from hugin.workers.backups import BackupWorker
from hugin.workers.notifications import NotificationWorker


@pytest.mark.parametrize("worker_type", [ApplicationWorker, NotificationWorker, BackupWorker])
def test_timed_out_stop_preserves_active_thread_and_prevents_restart(
    worker_type: type[ApplicationWorker] | type[NotificationWorker] | type[BackupWorker],
    tmp_path: Path,
) -> None:
    worker = worker_type(Settings(data_dir=tmp_path))
    release = threading.Event()
    original = threading.Thread(target=release.wait, daemon=True)
    original.start()
    worker._thread = original
    try:
        worker.stop(timeout_seconds=0.001)
        assert worker.running
        assert worker._thread is original
        worker.start()
        assert worker._thread is original
        assert worker._stop.is_set()
    finally:
        release.set()
        original.join(1)
        worker.stop()
    assert not worker.running
    assert worker._thread is None
