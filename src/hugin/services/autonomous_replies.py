from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.adapters.yandex_ai import YandexAIError
from hugin.database.models import (
    ApplicationModel,
    InvitationModel,
    RecruiterMessageModel,
    VacancyModel,
)
from hugin.domain.applications import ApplicationState
from hugin.domain.communications import CommunicationStateError
from hugin.domain.content import (
    InvitationState,
    MessageDirection,
    RecruiterMessageState,
)
from hugin.services.autonomy import AutonomyPolicyService
from hugin.services.communications import CommunicationService, RecordingMessageSender
from hugin.services.recruiter_reply import RecruiterReplyService, RecruiterReplyTextModel
from hugin.services.recruiter_reply_policy import (
    RecruiterReplyDisposition,
    classify_recruiter_reply,
)

_BLOCKING_INVITATION_TITLES = (
    "Приглашение на собеседование",
    "Задание от работодателя",
)


@dataclass(frozen=True, slots=True)
class ApprovedReplyToSend:
    message_id: int
    application_id: int
    source_url: str
    content_hash: str
    content_version: int


@dataclass(frozen=True, slots=True)
class AutonomousReplyBatch:
    approved: tuple[ApprovedReplyToSend, ...]
    drafts_created: int
    skipped_manual: int
    failed: int


class AutonomousReplyService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def prepare(
        self,
        *,
        account_id: int,
        model_factory: Callable[[], RecruiterReplyTextModel] | None = None,
        incoming_message_ids: Collection[int] = (),
    ) -> AutonomousReplyBatch:
        policy = AutonomyPolicyService(self._session).get()
        if not policy.auto_prepare_replies and not policy.auto_send_approved_replies:
            return AutonomousReplyBatch((), 0, 0, 0)

        messages = tuple(
            self._session.scalars(
                select(RecruiterMessageModel)
                .join(
                    ApplicationModel,
                    ApplicationModel.id == RecruiterMessageModel.application_id,
                )
                .where(ApplicationModel.account_id == account_id)
                .order_by(
                    RecruiterMessageModel.application_id,
                    RecruiterMessageModel.id,
                )
            )
        )
        by_application: dict[int, list[RecruiterMessageModel]] = {}
        for message in messages:
            by_application.setdefault(message.application_id, []).append(message)

        blocked_applications = set(
            self._session.scalars(
                select(InvitationModel.application_id).where(
                    InvitationModel.application_id.in_(tuple(by_application) or (-1,)),
                    InvitationModel.state != InvitationState.CLOSED,
                    InvitationModel.title.in_(_BLOCKING_INVITATION_TITLES),
                )
            )
        )
        application_details: dict[int, tuple[str, ApplicationState]] = {
            application_id: (source_url, state)
            for application_id, source_url, state in self._session.execute(
                select(ApplicationModel.id, VacancyModel.source_url, ApplicationModel.state)
                .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
                .where(
                    ApplicationModel.account_id == account_id,
                    ApplicationModel.id.in_(tuple(by_application) or (-1,)),
                )
            )
        }

        approved: list[ApprovedReplyToSend] = []
        drafts_created = 0
        skipped_manual = 0
        failed = 0
        model: RecruiterReplyTextModel | None = None
        communications = CommunicationService(self._session, RecordingMessageSender())
        eligible_incoming_ids = set(incoming_message_ids)

        for application_id, conversation in by_application.items():
            incoming = next(
                (
                    message
                    for message in reversed(conversation)
                    if message.direction is MessageDirection.INCOMING
                ),
                None,
            )
            if incoming is None:
                continue
            outgoing = next(
                (
                    message
                    for message in reversed(conversation)
                    if message.direction is MessageDirection.OUTGOING
                ),
                None,
            )
            template = policy.matching_reply_template(incoming.body)
            details = application_details.get(application_id)
            if details is None:
                continue
            source_url, application_state = details
            disposition = classify_recruiter_reply(
                application_state,
                incoming.body,
                template.response_text if template is not None else "",
            )
            if disposition is RecruiterReplyDisposition.NO_REPLY:
                continue
            manual = (
                application_id in blocked_applications
                or disposition is RecruiterReplyDisposition.MANUAL
            )
            if outgoing is not None and outgoing.id > incoming.id:
                if (
                    outgoing.state
                    in {
                        RecruiterMessageState.CONFIRMED,
                        RecruiterMessageState.FAILED,
                    }
                    and outgoing.auto_send_approved
                    and template is not None
                    and outgoing.reply_template_key == template.key
                    and outgoing.body == template.response_text
                    and policy.auto_send_approved_replies
                    and not manual
                    and outgoing.content_hash is not None
                ):
                    confirmed = communications.confirm_outgoing_retry(
                        account_id=account_id,
                        message_id=outgoing.id,
                        content_version=outgoing.version,
                        content_hash=outgoing.content_hash,
                    )
                    approved.append(
                        ApprovedReplyToSend(
                            message_id=confirmed.id,
                            application_id=application_id,
                            source_url=source_url,
                            content_hash=confirmed.content_hash or "",
                            content_version=confirmed.content_version,
                        )
                    )
                continue
            if incoming.id not in eligible_incoming_ids:
                continue
            if (
                template is not None
                and policy.auto_send_approved_replies
                and not manual
            ):
                draft = communications.create_outgoing_draft(
                    application_id=application_id,
                    body=template.response_text,
                    auto_send_approved=True,
                    reply_template_key=template.key,
                )
                confirmed = communications.confirm_outgoing_draft(
                    account_id=account_id,
                    message_id=draft.id,
                    content_version=draft.content_version,
                    content_hash=draft.content_hash or "",
                )
                approved.append(
                    ApprovedReplyToSend(
                        message_id=confirmed.id,
                        application_id=application_id,
                        source_url=source_url,
                        content_hash=confirmed.content_hash or "",
                        content_version=confirmed.content_version,
                    )
                )
                continue
            if manual:
                skipped_manual += 1
                continue
            if not policy.auto_prepare_replies or model_factory is None:
                continue
            try:
                if model is None:
                    model = model_factory()
                RecruiterReplyService(self._session, model).generate(
                    account_id=account_id,
                    application_id=application_id,
                )
            except (CommunicationStateError, ValueError, YandexAIError):
                failed += 1
            else:
                drafts_created += 1

        return AutonomousReplyBatch(
            approved=tuple(approved),
            drafts_created=drafts_created,
            skipped_manual=skipped_manual,
            failed=failed,
        )

    def approved_for_send(
        self,
        *,
        account_id: int,
        message_id: int,
        content_version: int,
        content_hash: str,
    ) -> bool:
        policy = AutonomyPolicyService(self._session).get_for_update()
        if not policy.auto_send_approved_replies:
            return False
        row = self._session.execute(
            select(RecruiterMessageModel, ApplicationModel)
            .join(
                ApplicationModel,
                ApplicationModel.id == RecruiterMessageModel.application_id,
            )
            .where(
                RecruiterMessageModel.id == message_id,
                ApplicationModel.account_id == account_id,
            )
        ).one_or_none()
        if row is None:
            return False
        message, application = row
        if (
            message.direction is not MessageDirection.OUTGOING
            or message.state is not RecruiterMessageState.CONFIRMED
            or not message.auto_send_approved
            or not message.reply_template_key
            or message.version != content_version
            or message.content_hash != content_hash
        ):
            return False
        incoming = self._session.scalar(
            select(RecruiterMessageModel)
            .where(
                RecruiterMessageModel.application_id == application.id,
                RecruiterMessageModel.direction == MessageDirection.INCOMING,
            )
            .order_by(RecruiterMessageModel.id.desc())
            .limit(1)
        )
        latest_outgoing_id = self._session.scalar(
            select(RecruiterMessageModel.id)
            .where(
                RecruiterMessageModel.application_id == application.id,
                RecruiterMessageModel.direction == MessageDirection.OUTGOING,
            )
            .order_by(RecruiterMessageModel.id.desc())
            .limit(1)
        )
        if incoming is None or message.id <= incoming.id or latest_outgoing_id != message.id:
            return False
        template = policy.matching_reply_template(incoming.body)
        disposition = classify_recruiter_reply(
            application.state,
            incoming.body,
            template.response_text if template is not None else "",
        )
        if (
            template is None
            or template.key != message.reply_template_key
            or template.response_text != message.body
            or disposition is not RecruiterReplyDisposition.AUTOMATIC_DRAFT
        ):
            return False
        blocking_invitation = self._session.scalar(
            select(InvitationModel.id)
            .where(
                InvitationModel.application_id == application.id,
                InvitationModel.state != InvitationState.CLOSED,
                InvitationModel.title.in_(_BLOCKING_INVITATION_TITLES),
            )
            .limit(1)
        )
        return blocking_invitation is None
