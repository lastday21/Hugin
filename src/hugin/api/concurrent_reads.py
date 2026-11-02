from collections.abc import Callable
from concurrent.futures import Future
from threading import Lock
from typing import TypeVar, cast

T = TypeVar("T")


class ConcurrentReads:
    """Share overlapping reads, without retaining completed responses."""

    def __init__(self, *, on_invalidate: Callable[[], None] | None = None) -> None:
        self._lock = Lock()
        self._pending: dict[tuple[str, int], Future[object]] = {}
        self._on_invalidate = on_invalidate

    def run(self, key: tuple[str, int], load: Callable[[], T]) -> T:
        with self._lock:
            future = self._pending.get(key)
            leader = future is None or future.done()
            if leader:
                future = Future()
                self._pending[key] = future
        assert future is not None
        if not leader:
            return cast(T, future.result())
        try:
            result = load()
        except BaseException as error:
            future.set_exception(error)
            raise
        else:
            future.set_result(result)
            return result
        finally:
            with self._lock:
                if self._pending.get(key) is future:
                    del self._pending[key]

    def invalidate(self) -> None:
        # Existing readers keep their result; reads after a write start afresh.
        with self._lock:
            self._pending.clear()
        if self._on_invalidate is not None:
            self._on_invalidate()
