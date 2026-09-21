"""Job state, reconstructed by folding the event log.

The log is the write model; this is the read model. Projections are derived and
never persisted: a stored projection can diverge from the log, and then there
are two truths.
"""

from __future__ import annotations

from typing import Any

from datadirector_contracts import (
    Classification, Event, EventKind, SensitivityClass,
)
from datadirector_contracts.events import CompensationPayload
from pydantic import BaseModel, ConfigDict, Field

from ..errors import DataDirectorError


class UnknownEventKind(DataDirectorError):
    """An event this reader does not understand.

    Raised rather than skipped: silently ignoring events written by a newer
    version would let an old reader report a confidently wrong state, which is
    worse than refusing to report at all.
    """


class JobState(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    step: str = "created"
    classification: Classification | None = None
    material: list[str] = Field(default_factory=list)
    confirmed_sets: dict[str, Any] = Field(default_factory=dict)
    pending: dict[str, Any] = Field(default_factory=dict)
    halted_reason: str | None = None
    terminal: str | None = None
    config_digest: str | None = None
    deposit_pid: str | None = None
     # Whether the depositor's own statement has been recorded. Derived from
     # the log like everything else here; the graph needs it because the
     # declaration node may not run until the depositor has said something.
    has_statement: bool = False


      # The kinds of event this log contains. The graph's conditions read
      # it rather than keeping a second copy of the truth beside the log.
    seen_kinds: frozenset[EventKind] = frozenset()


_STEP_FOR: dict[EventKind, str] = {
    EventKind.WORKFLOW_CREATED: "created",
    EventKind.MATERIAL_REGISTERED: "awaiting-declaration",
    EventKind.DECLARATION_PARSED: "awaiting-declaration-confirmation",
    EventKind.DECLARATION_CONFIRMED: "classification",
    EventKind.CLASSIFICATION_COMPLETED: "dmp",
    EventKind.CLASSIFICATION_CONTRADICTED: "awaiting-declaration-confirmation",
    EventKind.REDACTION_PROPOSED: "awaiting-redaction-decision",
    EventKind.REDACTION_DECIDED: "dmp",
    EventKind.DMP_COMMITMENTS_READ: "repository-selection",
    EventKind.DMP_DISCREPANCY_FLAGGED: "awaiting-discrepancy-review",
    # Events that are recorded but do not advance the workflow. Listed rather
    # than silently ignored: the fold refuses kinds it does not know, and a new
    # kind must be considered here rather than fall through.
    EventKind.INSTRUCTIONS_RECEIVED: None,
    EventKind.OWNER_ADDED: None,
    EventKind.METADATA_DRAFTED: None,
    EventKind.DOCUMENTATION_DRAFTED: None,
    EventKind.GATE_ITEMS_DISPLAYED: None,
    EventKind.ACCESS_AUDITED: None,
    EventKind.STEP_STARTED: None,
    EventKind.STEP_COMPLETED: None,
    EventKind.STEP_FAILED: None,
    EventKind.STEP_RECONCILED: None,
    EventKind.REPOSITORY_SELECTED: "metadata",
    EventKind.METADATA_GENERATED: "validation",
    EventKind.VALIDATION_COMPLETED: "awaiting-approval",
    EventKind.METADATA_APPROVED: "deposit",
    EventKind.DEPOSIT_COMPLETED: "complete",
}


def _apply(state: JobState, ev: Event) -> JobState:
    u: dict[str, Any] = {}
    k = ev.kind

    if k is EventKind.WORKFLOW_CREATED:
        u["config_digest"] = ev.payload.get("config_digest")
    elif k is EventKind.INSTRUCTIONS_RECEIVED:
        if (ev.payload.get("channel")
                == "responsibility-and-compliance-statement"):
            u["has_statement"] = True
    elif k is EventKind.MATERIAL_REGISTERED:
        u["material"] = state.material + list(ev.payload.get("artefacts", []))
    elif k is EventKind.DECLARATION_CONFIRMED:
        level = ev.payload.get("sensitivity")
        if level is not None:
            u["classification"] = Classification(
                level=SensitivityClass(level),
                established_by=ev.human,
                rationale="confirmed declaration",
            )
        u["confirmed_sets"] = {**state.confirmed_sets, "declaration": ev.payload}
    elif k is EventKind.CLASSIFICATION_COMPLETED:
        level = ev.payload.get("sensitivity")
        if level is not None and state.classification is not None:
            if SensitivityClass(level) > state.classification.level:
                u["classification"] = state.classification.tighten(
                    SensitivityClass(level),
                    agent=ev.agent,
                    rationale=ev.payload.get("rationale", "classification scan"),
                )
    elif k is EventKind.REDACTION_PROPOSED:
        u["pending"] = {**state.pending, "redaction": ev.payload}
    elif k is EventKind.REDACTION_DECIDED:
        pending = dict(state.pending)
        pending.pop("redaction", None)
        u["pending"] = pending
    elif k is EventKind.DEPOSIT_COMPLETED:
        u["deposit_pid"] = ev.payload.get("pid")
    elif k is EventKind.WORKFLOW_HALTED:
        u["halted_reason"] = ev.payload.get("reason")
    elif k is EventKind.WORKFLOW_CLOSED_NOT_SHARED:
        u["terminal"] = "closed-not-shared"
    elif k is EventKind.COMPENSATED:
        return state  # handled by the fold, which replays
    elif k not in _STEP_FOR:
        raise UnknownEventKind(
            f"event kind {k!r} at sequence {ev.sequence} is not understood by this "
            "version. Upgrade the application rather than continuing with a "
            "partial view of the job."
        )

    step = _STEP_FOR.get(k)
    if step is not None and state.halted_reason is None:
        u["step"] = step
    if k is not EventKind.WORKFLOW_HALTED and state.halted_reason is not None:
        u["halted_reason"] = None  # any subsequent event clears a halt
    return state.model_copy(update=u)


def fold(events: list[Event]) -> JobState:
    """Reconstruct current state. Pure: same events, same state, always.

    Compensation is applied by replaying to the restored sequence and then
    continuing past the compensating event. This is how C8 rollback and P5
    immutability coexist: nothing is removed, and the fold does the undoing.
    """
    if not events:
        raise ValueError("cannot fold an empty log")
    state = JobState(job_id=events[0].job_id)
    i = 0
    while i < len(events):
        ev = events[i]
        if ev.kind is EventKind.COMPENSATED:
            payload = CompensationPayload.model_validate(ev.payload)
            state = fold(events[: payload.restores_state_at_sequence])
            i += 1
            continue
        state = _apply(state, ev)
        i += 1
      # What the log has seen, derived like everything else: the graph's
      # conditions read this rather than a second record of what happened.
    return state.model_copy(update={
        "seen_kinds": frozenset(event.kind for event in events)})


def fold_to(events: list[Event], sequence: int) -> JobState:
    """State as at a given sequence. The resumability claim in one function."""
    return fold([e for e in events if e.sequence <= sequence])
