"""Small, dependency-free domain models for safe application runs.

The browser adapter deliberately returns these models instead of booleans.  In
particular, a successful JavaScript click is not evidence that BOSS accepted a
message, so ``SendResult`` has separate attempted and confirmed fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class IdentityConfidence(StrEnum):
    STRONG = "strong_identity"
    WEAK = "weak_identity"
    UNKNOWN = "unknown_identity"


class ApplicationStatus(StrEnum):
    DISCOVERED = "discovered"
    EVALUATING = "evaluating"
    SKIPPED = "skipped"
    ELIGIBLE = "eligible"
    DRAFTED = "drafted"
    PENDING_REVIEW = "pending_review"
    SENDING = "sending"
    CONTACT_CONFIRMED = "contact_confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ResumeShareStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    NOT_AVAILABLE = "not_available"
    PENDING = "pending"
    SHARED_CONFIRMED = "shared_confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


TERMINAL_APPLICATION_STATUSES = frozenset({
    ApplicationStatus.SKIPPED,
    ApplicationStatus.CONTACT_CONFIRMED,
    ApplicationStatus.FAILED,
    ApplicationStatus.UNKNOWN,
})


ALLOWED_TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.DISCOVERED: frozenset({ApplicationStatus.EVALUATING, ApplicationStatus.SKIPPED}),
    ApplicationStatus.EVALUATING: frozenset({ApplicationStatus.SKIPPED, ApplicationStatus.ELIGIBLE, ApplicationStatus.FAILED}),
    ApplicationStatus.ELIGIBLE: frozenset({ApplicationStatus.DRAFTED, ApplicationStatus.PENDING_REVIEW, ApplicationStatus.SKIPPED}),
    ApplicationStatus.DRAFTED: frozenset({ApplicationStatus.PENDING_REVIEW, ApplicationStatus.SENDING, ApplicationStatus.SKIPPED}),
    ApplicationStatus.PENDING_REVIEW: frozenset({ApplicationStatus.SENDING, ApplicationStatus.SKIPPED}),
    ApplicationStatus.SENDING: frozenset({ApplicationStatus.CONTACT_CONFIRMED, ApplicationStatus.FAILED, ApplicationStatus.UNKNOWN}),
    # ``skipped`` means no platform write occurred. A later dry-run can
    # safely reconsider it after an explicit local policy/threshold change;
    # the immutable event log preserves the prior decision. Sent/unknown
    # states remain terminal and can never be automatically revisited.
    ApplicationStatus.SKIPPED: frozenset({ApplicationStatus.DISCOVERED}),
    ApplicationStatus.CONTACT_CONFIRMED: frozenset(),
    ApplicationStatus.FAILED: frozenset(),
    ApplicationStatus.UNKNOWN: frozenset(),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class JobIdentity:
    key: str
    confidence: IdentityConfidence
    job_id: str = ""
    job_url: str = ""
    company: str = ""
    title: str = ""
    recruiter: str = ""
    evidence: str = ""

    @property
    def safe_for_auto_send(self) -> bool:
        return self.confidence == IdentityConfidence.STRONG and bool(self.key)


@dataclass(frozen=True)
class JobSnapshot:
    identity: JobIdentity
    description: str
    card_text: str = ""
    jd_changed_after_click: bool = False
    jd_evidence: str = ""


@dataclass(frozen=True)
class MatchResult:
    score: int | None
    reason: str
    matched_requirements: list[str] = field(default_factory=list)
    unmet_requirements: list[str] = field(default_factory=list)
    uncertain_requirements: list[str] = field(default_factory=list)
    degraded: bool = False
    eligible_for_auto_send: bool = False


@dataclass(frozen=True)
class SendResult:
    status: ApplicationStatus
    job_identity: JobIdentity
    recruiter_identity: str = ""
    message_attempted: bool = False
    message_confirmed: bool = False
    resume_share_status: ResumeShareStatus = ResumeShareStatus.NOT_REQUESTED
    confirmation_evidence: str = ""
    error: str = ""
    timestamp: str = field(default_factory=utc_now)


def as_jsonable(value: Any) -> Any:
    """Convert the small domain model set to JSON-safe primitives."""
    if isinstance(value, StrEnum):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return {key: as_jsonable(getattr(value, key)) for key in value.__dataclass_fields__}
    if isinstance(value, (list, tuple)):
        return [as_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): as_jsonable(item) for key, item in value.items()}
    return value
