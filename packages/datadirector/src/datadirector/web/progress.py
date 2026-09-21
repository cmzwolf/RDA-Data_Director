"""How far a job has got, and what happened at each stage.

Two things this deliberately does not do.

**No percentage.** Steps are conditional — a submission with no images never
runs media inspection — so a completion figure would be invented. A sequence of
named phases, each marked, says what is true without implying arithmetic that
does not exist.

**No blurring of waiting and stuck.** A job halted after two failed attempts
must not look like one quietly awaiting a researcher's confirmation. It is the
rule this system keeps arriving at: something that did not happen must not
resemble something that did.
"""

from __future__ import annotations

from enum import StrEnum

from datadirector_contracts import Event, EventKind
from pydantic import BaseModel, ConfigDict, Field


class PhaseState(StrEnum):
    DONE = "done"
    CURRENT = "current"
    WAITING_ON_YOU = "waiting-on-you"
    HALTED = "halted"
    PENDING = "pending"
    SKIPPED = "skipped"


# The phases a researcher recognises, and the events that evidence each. Named
# for what happens rather than for the agent that does it: "reading your data"
# is a thing a person understands, "classification agent" is an implementation.
PHASES: list[tuple[str, str, tuple[EventKind, ...]]] = [
    ("received", "Received",
     (EventKind.MATERIAL_REGISTERED,)),
    ("declaration", "Your statement about the data",
     (EventKind.DECLARATION_PARSED, EventKind.DECLARATION_CONFIRMED)),
    ("classification", "Working out what is sensitive",
     (EventKind.CLASSIFICATION_COMPLETED,)),
    ("metadata", "Drafting metadata and documentation",
     (EventKind.METADATA_DRAFTED, EventKind.DOCUMENTATION_DRAFTED,
      EventKind.VALIDATION_COMPLETED)),
    # Only a *decision* completes the review. Counting REDACTION_PROPOSED here
    # meant that raising an item marked the review done: the phase turned green
    # at the moment work appeared, which is the opposite of what it should say.
    ("review", "Your review",
     (EventKind.REDACTION_DECIDED, EventKind.METADATA_APPROVED)),
    ("deposit", "Publishing",
     (EventKind.DEPOSIT_COMPLETED, EventKind.WORKFLOW_CLOSED_NOT_SHARED)),
]

# Steps a person must take. Distinguished because "waiting on you" and "running"
# are different situations and a researcher needs to know which they are in.
HUMAN_PHASES = {"declaration", "review", "deposit"}


class Phase(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    state: PhaseState
    event_count: int = 0
    detail: str | None = Field(
        default=None,
        description="What is happening or what went wrong. Present for the "
        "current phase and for any that halted.")
    attempts_failed: int = Field(
        default=0,
        description="Retries recorded in this phase. Shown because a job that "
        "recovered after two failures is not the same as one that ran cleanly, "
        "and only the record distinguishes them.")


def phases_for(events: list[Event], state, *, unresolved: int = 0
               ) -> list[Phase]:
    """The phase sequence for one job, from its log.

    `unresolved` is passed in rather than derived here because the gate lives in
    the service: a phase cannot be done while items are still outstanding,
    however many decisions have been recorded.
    """
    by_kind: dict[EventKind, int] = {}
    failures: list[Event] = []
    for event in events:
        by_kind[event.kind] = by_kind.get(event.kind, 0) + 1
        if event.kind is EventKind.STEP_FAILED:
            failures.append(event)

    reached = _reached(state)
    out: list[Phase] = []
    for index, (key, label, kinds) in enumerate(PHASES):
        count = sum(by_kind.get(kind, 0) for kind in kinds)
        failed_here = sum(1 for f in failures
                          if _phase_of_step(f.payload.get("step")) == key)

        if key == "review" and unresolved:
            # Outstanding items mean the review is not finished, whatever
            # decisions have already been made.
            phase_state = PhaseState.WAITING_ON_YOU
            out.append(Phase(key=key, label=label, state=phase_state,
                             event_count=count,
                             detail=f"{unresolved} item(s) still to decide",
                             attempts_failed=failed_here))
            continue

        if state.halted_reason and key == reached:
            phase_state = PhaseState.HALTED
            detail = state.halted_reason
        elif count:
            phase_state = PhaseState.DONE
            detail = None
        elif key == reached:
            phase_state = (PhaseState.WAITING_ON_YOU if key in HUMAN_PHASES
                           else PhaseState.CURRENT)
            detail = _awaiting(key, state)
        elif _index_of(reached) > index:
            # Passed without evidence: a phase that did not apply, such as
            # media inspection with no images. Marked skipped rather than done,
            # because "did not apply" and "ran and found nothing" are different
            # and only one of them inspected anything.
            phase_state = PhaseState.SKIPPED
            detail = "nothing in this submission required it"
        else:
            phase_state = PhaseState.PENDING
            detail = None

        out.append(Phase(key=key, label=label, state=phase_state,
                         event_count=count, detail=detail,
                         attempts_failed=failed_here))
    return out


def _reached(state) -> str:
    """The phase a job is actually at.

    `ingestion` maps to the declaration phase rather than to `received`, because
    once material is registered the job is waiting for the depositor's
    statement — and a job that shows "received, in progress" when it is waiting
    for a person will be left alone.
    """
    if state.terminal:
        return "deposit"
    mapping = {"ingestion": "declaration", "declaration": "declaration",
               "classification": "classification", "metadata": "metadata",
               "deposit": "deposit"}
    return mapping.get(state.step, "declaration")


def _index_of(key: str) -> int:
    for index, (candidate, _, _) in enumerate(PHASES):
        if candidate == key:
            return index
    return 0


def _phase_of_step(step: str | None) -> str | None:
    if not step:
        return None
    return {"classification": "classification", "metadata": "metadata",
            "documentation": "metadata", "validation": "metadata",
            "deposit": "deposit"}.get(step)


def _awaiting(key: str, state) -> str | None:
    return {
        "declaration": ("Tell us what this data is and confirm what was "
                        "proposed."),
        "review": "Decide each outstanding item.",
        "deposit": "Confirm where to publish, and that it cannot be undone.",
        "classification": "Reading the data to work out what is sensitive.",
        "metadata": "Drafting metadata, documentation and validating them.",
    }.get(key)


def events_in_phase(events: list[Event], key: str) -> list[Event]:
    """The events belonging to one phase, for the history view.

    Filtered rather than offering one link to everything: "what happened during
    classification?" is the question a person actually has, and a flat log of
    forty entries does not answer it.
    """
    kinds = next((k for phase, _, k in PHASES if phase == key), ())
    related = set(kinds)
    out = []
    for event in events:
        if event.kind in related:
            out.append(event)
        elif event.kind in (EventKind.STEP_STARTED, EventKind.STEP_FAILED,
                            EventKind.STEP_COMPLETED,
                            EventKind.STEP_RECONCILED):
            if _phase_of_step(event.payload.get("step")) == key:
                out.append(event)
        elif event.kind is EventKind.WORKFLOW_HALTED and key != "received":
            out.append(event)
    return out
