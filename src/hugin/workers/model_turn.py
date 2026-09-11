from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import Protocol


class TextModel(Protocol):
    @property
    def model_name(self) -> str: ...

    def complete(self, system_prompt: str, user_prompt: str) -> str: ...


class ModelTurn:
    def __init__(self, allowed: Callable[[], bool], *, seconds: float = 180) -> None:
        self._allowed = allowed
        self._deadline = monotonic() + seconds

    def check(self) -> None:
        if not self._allowed():
            raise RuntimeError("Процесс остановлен; новый запрос модели отменён")
        if monotonic() >= self._deadline:
            raise RuntimeError("Время хода истекло; очередь передана следующему процессу")

    def wrap(self, model: TextModel) -> GuardedTextModel:
        return GuardedTextModel(model, self)


class GuardedTextModel:
    def __init__(self, model: TextModel, turn: ModelTurn) -> None:
        self._model = model
        self._turn = turn

    @property
    def model_name(self) -> str:
        return self._model.model_name

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self._turn.check()
        return self._model.complete(system_prompt, user_prompt)
