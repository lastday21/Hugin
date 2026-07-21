from enum import StrEnum


class ConfirmationState(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class ResumeMappingRole(StrEnum):
    PRIMARY = "PRIMARY"
    RESERVE = "RESERVE"


class CompanyRuleType(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"


class CoverLetterState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"
    SENT = "SENT"


class ScreeningFormState(StrEnum):
    DRAFT = "DRAFT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    INPUT_REQUIRED = "INPUT_REQUIRED"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
    SENT = "SENT"


class AnswerSource(StrEnum):
    PROFILE = "PROFILE"
    BANK = "BANK"
    YANDEXGPT = "YANDEXGPT"
    USER = "USER"


class MessageDirection(StrEnum):
    INCOMING = "INCOMING"
    OUTGOING = "OUTGOING"


class RecruiterMessageState(StrEnum):
    RECEIVED = "RECEIVED"
    DRAFT = "DRAFT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CONFIRMED = "CONFIRMED"
    SENT = "SENT"
    FAILED = "FAILED"


class InvitationState(StrEnum):
    RECEIVED = "RECEIVED"
    PREPARING = "PREPARING"
    SCHEDULED = "SCHEDULED"
    CLOSED = "CLOSED"


class IncidentSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class IncidentState(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class NotificationChannel(StrEnum):
    WINDOWS = "WINDOWS"
    TELEGRAM = "TELEGRAM"
    EMAIL = "EMAIL"


class DeliveryState(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
