from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, aliased

from hugin.adapters.codex_cli import configured_codex_cli_client
from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import ApplicationModel, RecruiterMessageModel
from hugin.diagnostics import OperationJournal, operation_context
from hugin.domain.automation import AutomationJobKind
from hugin.domain.content import MessageDirection, RecruiterMessageState
from hugin.domain.hh_sync import HhSyncBlockedError, HhSyncRetryableError
from hugin.domain.time import as_utc
from hugin.services.autonomous_replies import AutonomousReplyService
from hugin.services.background_processes import BackgroundProcessService
from hugin.services.recruiter_reply import RecruiterReplyTextModel
from hugin.workers.automation import AutomationJobBlocked, AutomationJobRetry
from hugin.workers.hh_sync import HhSyncJobHandler
from hugin.workers.model_turn import ModelTurn

RETRY_AFTER = timedelta(seconds=60)


class ReplyStopped(RuntimeError):
    pass


class ReplyWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        account_id: int = 1,
        browser_lock: threading.Lock | None = None,
        journal: OperationJournal | None = None,
    ) -> None:
        self._settings = settings
        self._account_id = account_id
        self._journal = journal or OperationJournal(settings.data_dir)
        self._sender = HhSyncJobHandler(
            settings, AutomationJobKind.MESSAGES, account_id=account_id, browser_lock=browser_lock
        )
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _allowed(self) -> bool:
        if self._stop.is_set():
            return False
        database = create_database(self._settings)
        try:
            with database.sessions() as session:
                return BackgroundProcessService(session, self._account_id).enabled("replies")
        finally:
            database.close()

    def _claim(self, session: Session) -> tuple[int, int] | None:
        service = BackgroundProcessService(session, self._account_id)
        if not service.enabled("replies"):
            return None
        runtime = service._runtime("replies")
        now = datetime.now(UTC)
        if runtime.retry_after_at and as_utc(runtime.retry_after_at) > now:
            return None
        incoming = (
            select(
                RecruiterMessageModel.application_id.label("application_id"),
                func.max(RecruiterMessageModel.id).label("message_id"),
            )
            .where(RecruiterMessageModel.direction == MessageDirection.INCOMING)
            .group_by(RecruiterMessageModel.application_id)
            .subquery()
        )
        outgoing = aliased(RecruiterMessageModel)
        candidates = list(
            session.execute(
                select(incoming.c.application_id, incoming.c.message_id)
                .join(ApplicationModel, ApplicationModel.id == incoming.c.application_id)
                .where(
                    ApplicationModel.account_id == self._account_id,
                    ~exists(
                        select(outgoing.id).where(
                            outgoing.application_id == incoming.c.application_id,
                            outgoing.direction == MessageDirection.OUTGOING,
                            outgoing.state == RecruiterMessageState.UNKNOWN_RESULT,
                        )
                    ),
                    ~exists(
                        select(outgoing.id).where(
                            outgoing.application_id == incoming.c.application_id,
                            outgoing.direction == MessageDirection.OUTGOING,
                            outgoing.state == RecruiterMessageState.SENT,
                            outgoing.id > incoming.c.message_id,
                        )
                    ),
                )
                .order_by(incoming.c.application_id)
            )
        )
        if not candidates:
            return None
        later = [row for row in candidates if row.application_id > runtime.cursor_application_id]
        if not later and runtime.next_attempt_at and as_utc(runtime.next_attempt_at) > now:
            return None
        selected = later[0] if later else candidates[0]
        if not later or runtime.next_attempt_at is None:
            runtime.next_attempt_at = now + RETRY_AFTER
        runtime.cursor_application_id = selected.application_id
        session.flush()
        return int(selected.application_id), int(selected.message_id)

    def _model(self, turn: ModelTurn, *, requirement: bool = False) -> RecruiterReplyTextModel:
        if not self._allowed():
            raise ReplyStopped("Ответы работодателям остановлены")
        model = configured_codex_cli_client(
            self._settings,
            operation="recruiter_reply_requirement" if requirement else "recruiter_reply",
            model=self._settings.codex_reply_requirement_model if requirement else None,
            timeout_seconds=min(self._settings.codex_reply_requirement_timeout_seconds, 60)
            if requirement
            else 60,
        )
        return turn.wrap(model)

    def run_once(self) -> bool:
        if self._stop.is_set():
            return False
        database = create_database(self._settings)
        try:
            with database.sessions.begin() as session:
                selected = self._claim(session)
            if selected is None:
                return False
            application_id, incoming_id = selected
            turn = ModelTurn(self._allowed)
            run = self._journal.start(
                "replies",
                "prepare_and_send",
                account_id=self._account_id,
                application_id=application_id,
            )
            try:
                with database.sessions.begin() as session:
                    if not self._allowed():
                        run.block(reason="Ответы остановлены")
                        return False
                    with operation_context(
                        account_id=self._account_id,
                        application_id=application_id,
                        parent_run_id=run.run_id,
                    ):
                        batch = AutonomousReplyService(session).prepare(
                            account_id=self._account_id,
                            application_id=application_id,
                            incoming_message_ids=(incoming_id,),
                            include_backlog=True,
                            model_factory=lambda: self._model(turn),
                            requirement_model_factory=lambda: self._model(turn, requirement=True),
                            can_continue=self._allowed,
                        )
                metrics = (
                    self._sender._send_approved_replies(batch.approved, can_continue=self._allowed)
                    if batch.approved and self._allowed()
                    else {}
                )
                run.succeed(
                    drafts_created=batch.drafts_created,
                    skipped_manual=batch.skipped_manual,
                    preparation_failed=batch.failed,
                    **metrics,
                )
                return True
            except ReplyStopped:
                run.block(reason="Ответы остановлены")
                return False
            except HhSyncBlockedError as error:
                self._sender._protect_system(self._sender._system_state_from_code(error.code))
                run.fail(error)
                raise AutomationJobBlocked(error.code, str(error)) from error
            except HhSyncRetryableError as error:
                with database.sessions.begin() as session:
                    runtime = BackgroundProcessService(session, self._account_id)._runtime(
                        "replies"
                    )
                    runtime.retry_after_at = datetime.now(UTC) + timedelta(
                        seconds=error.retry_after_seconds
                    )
                run.fail(error)
                raise AutomationJobRetry(
                    error.code, str(error), retry_after_seconds=error.retry_after_seconds
                ) from error
            except Exception as error:
                run.fail(error)
                raise
        finally:
            database.close()
