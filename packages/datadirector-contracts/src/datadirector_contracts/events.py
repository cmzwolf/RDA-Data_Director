"""The event log: append-only state with a hash chain.

Document A section 7.2. One mechanism yields four properties: resumability,
rollback (Blueprint C8), tamper evidence (P5, C1), and traceability (R10).

Rollback is appending a compensating event, never deleting. There is no delete
operation in this module, and that absence is the contract.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .primitives import Digest, JobId, Orcid, utc_now


class EventKind(StrEnum):
    # Intent and completion, for steps with effects outside this system.
    #
    # Recording only completions makes a crash *during* a step indistinguishable
    # from a step never attempted, which is harmless for internal work and not
    # harmless for anything external: a process that dies after a repository
    # accepts an upload but before the completion is written leaves a log saying
    # "not deposited", and a naive resume deposits again.
    #
    # An intent written before the effect turns that silence into a question. A
    # started record with no matching outcome is not retried blindly; it is
    # reconciled, by asking the external service what actually happened.
    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"
    STEP_FAILED = "step.failed"
    STEP_RECONCILED = "step.reconciled"

    GATE_ITEMS_DISPLAYED = "gate.items-displayed"
    """Items were put in front of a person.

    Recorded because §4.1 claims rubber-stamping is detectable from approval
    latency, and an interval needs two endpoints: without a display time there
    is nothing to measure a decision against."""

    OWNER_ADDED = "ownership.owner-added"
    ACCESS_AUDITED = "access.audited"
    """An auditor read a job they do not own.

    Recorded because a role whose use is invisible is an unlogged back door with
    a respectable name."""

    METADATA_DRAFTED = "metadata.drafted"
    DOCUMENTATION_DRAFTED = "documentation.drafted"

    WORKFLOW_CREATED = "workflow.created"
    MATERIAL_REGISTERED = "material.registered"
    DECLARATION_PARSED = "declaration.parsed"
    DECLARATION_CONFIRMED = "declaration.confirmed"
    CLASSIFICATION_COMPLETED = "classification.completed"
    CLASSIFICATION_CONTRADICTED = "classification.contradicted"
    REDACTION_PROPOSED = "redaction.proposed"
    REDACTION_DECIDED = "redaction.decided"
    DMP_COMMITMENTS_READ = "dmp.commitments-read"
    DMP_DISCREPANCY_FLAGGED = "dmp.discrepancy-flagged"
    INSTRUCTIONS_RECEIVED = "instructions.received"
    REPOSITORY_SELECTED = "repository.selected"
    METADATA_GENERATED = "metadata.generated"
    METADATA_APPROVED = "metadata.approved"
    VALIDATION_COMPLETED = "validation.completed"
    DEPOSIT_COMPLETED = "deposit.completed"
    WORKFLOW_HALTED = "workflow.halted"
    WORKFLOW_CLOSED_NOT_SHARED = "workflow.closed-not-shared"
    COMPENSATED = "state.compensated"


HUMAN_ACTS: frozenset[EventKind] = frozenset(
    {
        EventKind.OWNER_ADDED,
        EventKind.GATE_ITEMS_DISPLAYED,
        EventKind.DECLARATION_CONFIRMED,
        EventKind.REDACTION_DECIDED,
        EventKind.METADATA_APPROVED,
        EventKind.DEPOSIT_COMPLETED,
    }
)
"""The four nodal points of Document A section 4.1.

Each must name the human responsible. The recorder rejects any of these
lacking an ORCID rather than writing a null, per Blueprint section 5.4.
"""


class Event(BaseModel):
    """One entry in a job's log.

    Files are named <sequence>-<timestamp>.json. The sequence precedes the
    timestamp because timestamps collide and do not sort reliably across clock
    adjustments.
    """

    model_config = ConfigDict(frozen=True)

    sequence: int = Field(ge=1)
    job_id: JobId
    kind: EventKind
    occurred_at: datetime = Field(default_factory=utc_now)
    agent: str = Field(description="Software agent identity and version.")
    human: Orcid | None = None
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="References and digests only. Never payload, secrets, or "
        "confidential free text (commitment C-3).",
    )
    prev_digest: Digest | None = Field(
        default=None, description="None only for the first event in a job."
    )

    def model_post_init(self, _context: object) -> None:
        if self.kind in HUMAN_ACTS and self.human is None:
            raise ValueError(
                f"{self.kind} is a human act and must name the responsible ORCID. "
                "Agents never act anonymously (Blueprint section 5.4)."
            )
        if self.sequence == 1 and self.prev_digest is not None:
            raise ValueError("the first event in a job has no predecessor")
        if self.sequence > 1 and self.prev_digest is None:
            raise ValueError("only the first event may omit prev_digest; the chain must not break")

    def digest(self) -> Digest:
        return Digest.of_canonical_json(self.model_dump(mode="json"))


class EffectKind(StrEnum):
    """What kind of external effect a step is about to attempt.

    Recorded because reconciliation differs by kind: a created deposition can be
    looked up, a released credential cannot be unreleased, and an exposure that
    may or may not have reached a model must be assumed to have done so.
    """

    REPOSITORY_CREATE = "repository.create"
    REPOSITORY_UPLOAD = "repository.upload"
    REPOSITORY_PUBLISH = "repository.publish"
    MODEL_CALL = "model.call"
    EXTERNAL_FETCH = "external.fetch"


class StepIntent(BaseModel):
    """What is about to be attempted, written before it is attempted.

    `idempotency_key` is what makes reconciliation possible rather than merely
    visible: it is the handle by which the external service can be asked whether
    this particular attempt already took effect.
    """

    model_config = ConfigDict(frozen=True)

    step: str
    effect: EffectKind
    target: str = Field(description="Service or resource the effect acts on.")
    idempotency_key: str = Field(
        description="Stable across retries of the same logical attempt. Two "
        "attempts sharing a key are the same attempt; two with different keys "
        "are different ones, and conflating them is how duplicates are made.")
    detail: dict[str, Any] = Field(default_factory=dict)


class StepOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    step: str
    idempotency_key: str
    result: dict[str, Any] = Field(default_factory=dict)


class Reconciliation(BaseModel):
    """What was found when an unfinished attempt was investigated."""

    model_config = ConfigDict(frozen=True)

    step: str
    idempotency_key: str
    finding: str = Field(
        description="took-effect | did-not-take-effect | indeterminate")
    detail: str
    resolved_by: Orcid | None = Field(
        default=None,
        description="Set where a human decided. An indeterminate outcome is not "
        "the system's to resolve: guessing is how a dataset gets published "
        "twice or silently not at all.")


class CompensationPayload(BaseModel):
    """Rollback, expressed as an addition rather than a removal (ADR-008)."""

    model_config = ConfigDict(frozen=True)

    compensates_sequence: int = Field(ge=1)
    restores_state_at_sequence: int = Field(ge=1)
    reason: str


def verify_chain(events: list[Event]) -> None:
    """Raise if the chain has been broken. Tamper evidence in one function.

    Detects: reordering, gaps, and any alteration to an event's content, since
    altering event N invalidates the prev_digest recorded in event N+1.
    """
    for i, ev in enumerate(events):
        if ev.sequence != i + 1:
            raise ValueError(f"sequence gap or reordering at position {i}: got {ev.sequence}")
        if i == 0:
            continue
        expected = events[i - 1].digest()
        if ev.prev_digest != expected:
            raise ValueError(
                f"chain broken at sequence {ev.sequence}: "
                f"expected predecessor {expected}, recorded {ev.prev_digest}"
            )
