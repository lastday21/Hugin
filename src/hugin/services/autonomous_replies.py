from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.adapters.codex_cli import CodexCliError
from hugin.adapters.yandex_ai import YandexAIError
from hugin.database.models import (
    ApplicationModel,
    CandidateProfileModel,
    InvitationModel,
    RecruiterMessageActionModel,
    RecruiterMessageModel,
    VacancyModel,
    VerifiedFactModel,
)
from hugin.domain.applications import ApplicationState
from hugin.domain.communications import CommunicationStateError
from hugin.domain.content import (
    ConfirmationState,
    InvitationState,
    MessageDirection,
    RecruiterActionKind,
    RecruiterActionSource,
    RecruiterActionState,
    RecruiterMessageState,
)
from hugin.services.autonomy import AutonomyPolicyService
from hugin.services.communications import CommunicationService, RecordingMessageSender
from hugin.services.recruiter_reply import RecruiterReplyService, RecruiterReplyTextModel
from hugin.services.recruiter_reply_policy import (
    RecruiterReplyDisposition,
    classify_recruiter_reply,
    is_exact_120_net_salary_response,
    is_simple_salary_expectation_question,
    repeated_incoming_already_answered,
    requested_external_action_kind,
    unresolved_action_position_before_invitation_reminder,
)
from hugin.services.recruiter_reply_requirement import (
    ReplyRequirement,
    ReplyRequirementModel,
    classify_reply_requirement,
)

_BLOCKING_INVITATION_TITLES = (
    "Приглашение на собеседование",
    "Задание от работодателя",
)
_GENERATED_REPLY_APPROVAL_KEY = "model_safe_v1"
_SALARY_EXPECTATION_APPROVAL_KEY = "salary_expectation_120_net"
_SALARY_EXPECTATION_REPLY = "Мои зарплатные ожидания — 120 000 рублей на руки."


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
        requirement_model_factory: Callable[[], ReplyRequirementModel] | None = None,
        incoming_message_ids: Collection[int] = (),
        include_backlog: bool = False,
        backlog_limit: int = 250,
    ) -> AutonomousReplyBatch:
        if backlog_limit < 1:
            raise ValueError("Размер очереди сообщений должен быть положительным")
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
        requirement_model: ReplyRequirementModel | None = None
        communications = CommunicationService(self._session, RecordingMessageSender())
        communications.dismiss_expired_actions(account_id=account_id)
        actions_by_key = {
            (action.message_id, action.kind): action
            for action in communications.message_actions(account_id)
        }
        salary_expectation_reply = self._confirmed_salary_expectation_reply(account_id)
        eligible_incoming_ids = set(incoming_message_ids)
        auto_send_incoming_ids = set(incoming_message_ids)
        if include_backlog:
            backlog_ids: list[int] = []
            for conversation in by_application.values():
                latest_incoming = next(
                    (
                        message
                        for message in reversed(conversation)
                        if message.direction is MessageDirection.INCOMING
                    ),
                    None,
                )
                latest_outgoing = next(
                    (
                        message
                        for message in reversed(conversation)
                        if message.direction is MessageDirection.OUTGOING
                    ),
                    None,
                )
                if latest_incoming is not None and (
                    latest_outgoing is None or latest_outgoing.id < latest_incoming.id
                ):
                    backlog_ids.append(latest_incoming.id)
            eligible_incoming_ids.update(sorted(backlog_ids, reverse=True)[:backlog_limit])

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
            if repeated_incoming_already_answered(conversation):
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
            simple_salary_question = is_simple_salary_expectation_question(incoming.body)
            exact_salary_reply = salary_expectation_reply if simple_salary_question else None
            details = application_details.get(application_id)
            if details is None:
                continue
            source_url, application_state = details
            disposition = classify_recruiter_reply(
                application_state,
                incoming.body,
                (exact_salary_reply or (template.response_text if template is not None else "")),
            )
            if simple_salary_question and exact_salary_reply is None:
                disposition = RecruiterReplyDisposition.REVIEW_DRAFT
            recovered_before_reminder = False
            if disposition is RecruiterReplyDisposition.NO_REPLY:
                action_position = unresolved_action_position_before_invitation_reminder(
                    application_state,
                    conversation,
                )
                if action_position is None:
                    continue
                reminder_id = incoming.id
                incoming = conversation[action_position]
                if (
                    reminder_id not in eligible_incoming_ids
                    and incoming.id not in eligible_incoming_ids
                ):
                    continue
                eligible_incoming_ids.add(incoming.id)
                recovered_before_reminder = True
                template = policy.matching_reply_template(incoming.body)
                simple_salary_question = is_simple_salary_expectation_question(incoming.body)
                exact_salary_reply = salary_expectation_reply if simple_salary_question else None
                disposition = classify_recruiter_reply(
                    application_state,
                    incoming.body,
                    (
                        exact_salary_reply
                        or (template.response_text if template is not None else "")
                    ),
                )
                if simple_salary_question and exact_salary_reply is None:
                    disposition = RecruiterReplyDisposition.REVIEW_DRAFT
                if disposition is RecruiterReplyDisposition.NO_REPLY:
                    continue
            reply_action = actions_by_key.get((incoming.id, RecruiterActionKind.REPLY))
            if disposition is RecruiterReplyDisposition.MANUAL:
                external_kind = requested_external_action_kind(incoming.body)
                external_action = (
                    actions_by_key.get((incoming.id, external_kind))
                    if external_kind is not None
                    else None
                )
                if external_kind is not None and external_action is None:
                    external_action = communications.require_external_action(
                        account_id=account_id,
                        message_id=incoming.id,
                        kind=external_kind,
                        source=RecruiterActionSource.RULE,
                        reason_code="EMPLOYER_EXTERNAL_ACTION_REQUIRED",
                        reason="Работодатель запросил действие вне переписки hh.ru.",
                    )
                    actions_by_key[(incoming.id, external_kind)] = external_action
                if (
                    external_action is not None
                    and external_action.state is not RecruiterActionState.REQUIRED
                    and not (
                        reply_action is not None
                        and reply_action.state is RecruiterActionState.REQUIRED
                    )
                ):
                    continue
            elif reply_action is not None:
                terminal_reply_states = {
                    RecruiterActionState.COMPLETED,
                    RecruiterActionState.DISMISSED,
                }
                if disposition is RecruiterReplyDisposition.AMBIGUOUS:
                    terminal_reply_states.add(RecruiterActionState.NOT_REQUIRED)
                if reply_action.state in terminal_reply_states:
                    continue
            automatic_send_blocked = (
                recovered_before_reminder
                or application_id in blocked_applications
                or disposition
                in {
                    RecruiterReplyDisposition.REVIEW_DRAFT,
                    RecruiterReplyDisposition.MANUAL,
                    RecruiterReplyDisposition.AMBIGUOUS,
                }
            )
            if outgoing is not None and outgoing.id > incoming.id:
                generated_reply_is_safe = (
                    outgoing.reply_template_key == _GENERATED_REPLY_APPROVAL_KEY
                    and classify_recruiter_reply(
                        application_state,
                        incoming.body,
                        outgoing.body,
                    )
                    is RecruiterReplyDisposition.AUTOMATIC_DRAFT
                )
                salary_reply_is_safe = (
                    outgoing.reply_template_key == _SALARY_EXPECTATION_APPROVAL_KEY
                    and exact_salary_reply is not None
                    and outgoing.body == exact_salary_reply
                    and is_exact_120_net_salary_response(outgoing.body)
                )
                if (
                    outgoing.state
                    in {
                        RecruiterMessageState.CONFIRMED,
                        RecruiterMessageState.FAILED,
                    }
                    and outgoing.auto_send_approved
                    and (
                        generated_reply_is_safe
                        or salary_reply_is_safe
                        or (
                            template is not None
                            and outgoing.reply_template_key == template.key
                            and outgoing.body == template.response_text
                        )
                    )
                    and policy.auto_send_approved_replies
                    and not automatic_send_blocked
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
            if exact_salary_reply is not None:
                auto_send = (
                    policy.auto_send_approved_replies
                    and incoming.id in eligible_incoming_ids
                    and not automatic_send_blocked
                )
                draft = communications.create_outgoing_draft(
                    application_id=application_id,
                    body=exact_salary_reply,
                    auto_send_approved=auto_send,
                    reply_template_key=(_SALARY_EXPECTATION_APPROVAL_KEY if auto_send else None),
                )
                drafts_created += 1
                if auto_send:
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
            if (
                template is not None
                and policy.auto_send_approved_replies
                and incoming.id in auto_send_incoming_ids
                and not automatic_send_blocked
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
            if disposition is RecruiterReplyDisposition.MANUAL:
                skipped_manual += 1
                continue
            if disposition is RecruiterReplyDisposition.AMBIGUOUS:
                requirement = ReplyRequirement.REQUIRED
                if reply_action is not None and reply_action.state is RecruiterActionState.REQUIRED:
                    requirement = ReplyRequirement.REQUIRED
                elif requirement_model_factory is not None:
                    try:
                        if requirement_model is None:
                            requirement_model = requirement_model_factory()
                        requirement = classify_reply_requirement(
                            requirement_model,
                            conversation,
                        )
                    except (CodexCliError, LookupError, ValueError, YandexAIError):
                        failed += 1
                        reply_action = communications.record_reply_requirement(
                            account_id=account_id,
                            message_id=incoming.id,
                            required=True,
                            source=RecruiterActionSource.SYSTEM,
                            reason_code="CLASSIFIER_FAILED_SAFE_FALLBACK",
                            reason=(
                                "Проверка неоднозначного сообщения завершилась ошибкой; "
                                "возможный вопрос оставлен для ответа."
                            ),
                        )
                    else:
                        reply_action = communications.record_reply_requirement(
                            account_id=account_id,
                            message_id=incoming.id,
                            required=requirement is ReplyRequirement.REQUIRED,
                            source=RecruiterActionSource.MODEL,
                            reason_code=(
                                "MODEL_REPLY_REQUIRED"
                                if requirement is ReplyRequirement.REQUIRED
                                else "MODEL_NO_REPLY_REQUIRED"
                            ),
                            reason=(
                                "Неоднозначное сообщение требует ответа."
                                if requirement is ReplyRequirement.REQUIRED
                                else "Неоднозначное сообщение не требует ответа."
                            ),
                        )
                    actions_by_key[(incoming.id, RecruiterActionKind.REPLY)] = reply_action
                else:
                    reply_action = communications.record_reply_requirement(
                        account_id=account_id,
                        message_id=incoming.id,
                        required=True,
                        source=RecruiterActionSource.SYSTEM,
                        reason_code="CLASSIFIER_UNAVAILABLE_SAFE_FALLBACK",
                        reason=(
                            "Проверка неоднозначного сообщения недоступна; "
                            "возможный вопрос оставлен для ответа."
                        ),
                    )
                    actions_by_key[(incoming.id, RecruiterActionKind.REPLY)] = reply_action
                if requirement is ReplyRequirement.NOT_REQUIRED:
                    continue
                disposition = RecruiterReplyDisposition.REVIEW_DRAFT
            if not policy.auto_prepare_replies or model_factory is None:
                continue
            try:
                if model is None:
                    model = model_factory()
                draft = RecruiterReplyService(self._session, model).generate(
                    account_id=account_id,
                    application_id=application_id,
                    incoming_message_id=incoming.id,
                )
            except (
                CodexCliError,
                CommunicationStateError,
                LookupError,
                ValueError,
                YandexAIError,
            ):
                failed += 1
            else:
                drafts_created += 1
                generated_disposition = classify_recruiter_reply(
                    application_state,
                    incoming.body,
                    draft.body,
                )
                if (
                    policy.auto_send_approved_replies
                    and incoming.id in auto_send_incoming_ids
                    and not automatic_send_blocked
                    and generated_disposition is RecruiterReplyDisposition.AUTOMATIC_DRAFT
                    and draft.content_hash is not None
                ):
                    approved_draft = communications.approve_outgoing_for_automatic_send(
                        account_id=account_id,
                        message_id=draft.id,
                        content_version=draft.content_version,
                        content_hash=draft.content_hash,
                        approval_key=_GENERATED_REPLY_APPROVAL_KEY,
                    )
                    confirmed = communications.confirm_outgoing_draft(
                        account_id=account_id,
                        message_id=approved_draft.id,
                        content_version=approved_draft.content_version,
                        content_hash=approved_draft.content_hash or "",
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
        if incoming is not None:
            action = self._session.get(
                RecruiterMessageActionModel,
                (incoming.id, RecruiterActionKind.REPLY),
            )
            if action is not None:
                blocked_states = {
                    RecruiterActionState.COMPLETED,
                    RecruiterActionState.DISMISSED,
                }
                if (
                    classify_recruiter_reply(application.state, incoming.body)
                    is RecruiterReplyDisposition.AMBIGUOUS
                ):
                    blocked_states.add(RecruiterActionState.NOT_REQUIRED)
                if action.state in blocked_states:
                    return False
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
        if message.reply_template_key == _SALARY_EXPECTATION_APPROVAL_KEY:
            salary_reply = self._confirmed_salary_expectation_reply(account_id)
            if (
                salary_reply is None
                or message.body != salary_reply
                or not is_simple_salary_expectation_question(incoming.body)
                or not is_exact_120_net_salary_response(message.body)
            ):
                return False
        elif message.reply_template_key == _GENERATED_REPLY_APPROVAL_KEY:
            disposition = classify_recruiter_reply(
                application.state,
                incoming.body,
                message.body,
            )
            if disposition is not RecruiterReplyDisposition.AUTOMATIC_DRAFT:
                return False
        else:
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

    def _confirmed_salary_expectation_reply(self, account_id: int) -> str | None:
        contents = self._session.scalars(
            select(VerifiedFactModel.content)
            .join(
                CandidateProfileModel,
                CandidateProfileModel.id == VerifiedFactModel.profile_id,
            )
            .where(
                CandidateProfileModel.account_id == account_id,
                VerifiedFactModel.state == ConfirmationState.CONFIRMED,
                VerifiedFactModel.allow_in_messages.is_(True),
            )
        )
        for content in contents:
            normalized = " ".join(content.casefold().split())
            digits = "".join(character for character in normalized if character.isdigit())
            if (
                "120000" in digits
                and "на руки" in normalized
                and any(
                    marker in normalized for marker in ("зарплат", "заработн", "оклад", "доход")
                )
            ):
                return _SALARY_EXPECTATION_REPLY
        return None
