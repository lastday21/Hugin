from __future__ import annotations

from typing import Protocol

from hugin.domain.communications import (
    MessageSendRequest,
    MessageSendResult,
)


class HhMessageBrowser(Protocol):
    def send_recruiter_message(self, source_url: str, body: str) -> MessageSendResult: ...


class HhBrowserMessageSender:
    def __init__(self, browser: HhMessageBrowser, source_url: str) -> None:
        self._browser = browser
        self._source_url = source_url

    def send(self, request: MessageSendRequest) -> MessageSendResult:
        return self._browser.send_recruiter_message(
            self._source_url,
            request.body,
        )
