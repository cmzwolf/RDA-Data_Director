"""Turning events into sentences a person can read.

The history page rendered event kinds and payload dictionaries directly:
`declaration.parsed by declaration/0.1.0`, then `authority: proposed`,
`claim_count: 1`, and a Python dict repr of the claims. That is a log file in a
browser. It is legible to whoever wrote the event and to nobody else, and the
person it most needs to serve — a researcher deciding whether to publish, or an
auditor two years later — is precisely the one who cannot read it.

Two rules here.

**Say what happened, not what was emitted.** `declaration.parsed` is the name of
an event; "we read your statement" is the thing that occurred.

**Show the fields that inform a decision, and omit the rest.** A digest, an
agent version and an authority state are real and belong in the record; they do
not belong on a page someone is reading to decide something. They remain in the
event log, which is where a person who wants them will look.
"""

from __future__ import annotations

from datadirector_contracts import Event, EventKind
from pydantic import BaseModel, ConfigDict, Field

LEVELS = {0: "public", 1: "internal", 2: "sensitive"}

# What each event means, in the second person where a person did it.
SENTENCES: dict[EventKind, str] = {
    EventKind.WORKFLOW_CREATED: "Job started",
    EventKind.MATERIAL_REGISTERED: "Your files were taken in",
    EventKind.INSTRUCTIONS_RECEIVED: "You told us something",
    EventKind.DECLARATION_PARSED: "We read your statement",
    EventKind.DECLARATION_CONFIRMED: "You confirmed what it said",
    EventKind.CLASSIFICATION_COMPLETED: "We worked out how sensitive it is",
    EventKind.DMP_COMMITMENTS_READ: "We read your data management plan",
    EventKind.DMP_DISCREPANCY_FLAGGED: "The plan and the deposit differ",
    EventKind.METADATA_DRAFTED: "We drafted the metadata",
    EventKind.DOCUMENTATION_DRAFTED: "We drafted the documentation",
    EventKind.VALIDATION_COMPLETED: "We checked the metadata",
    EventKind.METADATA_APPROVED: "You approved the metadata",
    EventKind.REDACTION_PROPOSED: "Something was put to you for review",
    EventKind.REDACTION_DECIDED: "You decided an item",
    EventKind.REPOSITORY_SELECTED: "You chose where to publish",
    EventKind.DEPOSIT_COMPLETED: "Published",
    EventKind.WORKFLOW_CLOSED_NOT_SHARED: "Closed without publishing",
    EventKind.WORKFLOW_HALTED: "Stopped",
    EventKind.OWNER_ADDED: "An owner was added",
    EventKind.ACCESS_AUDITED: "An auditor read this job",
    EventKind.STEP_STARTED: "A step began",
    EventKind.STEP_COMPLETED: "A step finished",
    EventKind.STEP_FAILED: "A step failed and will be tried again",
    EventKind.STEP_RECONCILED: "An interrupted step was settled",
    EventKind.COMPENSATED: "An earlier step was undone",
}

# Payload keys that are machinery rather than information: digests, version
# strings, internal markers. Present in the log, absent from the page.
INTERNAL_KEYS = {
    "source_digest", "config_digest", "input_digest", "authority", "marker",
    "idempotency_key", "claim_count", "items", "record", "profile",
    "reason_recorded", "created_by", "channel", "sequence",
}


class Told(BaseModel):
    """One event, as a sentence and a few facts."""

    model_config = ConfigDict(frozen=True)

    what: str
    who: str | None = None
    detail: list[str] = Field(default_factory=list)
    concerning: str | None = Field(
        default=None,
        description="Set where the event is a problem, so the page can mark it "
        "without the reader having to parse the sentence for tone.")


def describe(event: Event) -> Told:
    """One event as something a person can read."""
    what = SENTENCES.get(event.kind, event.kind.value)
    who = event.human.value if event.human else None
    payload = event.payload or {}
    detail: list[str] = []
    concerning = None

    if event.kind is EventKind.MATERIAL_REGISTERED:
        count = len(payload.get("artefacts") or [])
        detail.append(f"{count} file(s)")
        for nested in payload.get("nested_archives") or []:
            detail.append(f"{nested} is an archive inside your submission; it "
                          "was received and not opened")

    elif event.kind is EventKind.DECLARATION_PARSED:
        inferred = payload.get("inferred_sensitivity")
        stated = payload.get("stated_sensitivity")
        detail.append(
            f"you stated: {LEVELS.get(stated, 'no level')}"
            if stated is not None else "you did not state a level")
        if inferred is not None:
            detail.append(f"read from what you described: "
                          f"{LEVELS.get(inferred, inferred)}")
        for indicator in (payload.get("inference_indicators") or [])[:6]:
            detail.append(f"because: {indicator}")

    elif event.kind is EventKind.DECLARATION_CONFIRMED:
        level = payload.get("sensitivity")
        detail.append(f"treated as {LEVELS.get(level, level)}")
        if payload.get("assumed_most_restrictive"):
            detail.append("you did not choose a level, so the most restrictive "
                          "was applied")

    elif event.kind is EventKind.CLASSIFICATION_COMPLETED:
        level = payload.get("sensitivity")
        detail.append(f"{LEVELS.get(level, level)}")
        for indicator in (payload.get("indicators") or [])[:6]:
            detail.append(indicator)

    elif event.kind is EventKind.DMP_COMMITMENTS_READ:
        route = payload.get("route")
        count = payload.get("commitments", 0)
        if route == "none-found":
            detail.append("no plan was supplied or found, so there are no "
                          "commitments to check against. That is not permission "
                          "to publish.")
        else:
            where = {"supplied": "from the file you gave us",
                     "found-in-submission": "found inside your submission"
                     }.get(route, route)
            detail.append(f"{count} commitment(s), {where}")
            if payload.get("structured") is False:
                detail.append("read from prose, so these are a reading of the "
                              "plan rather than facts about it")

    elif event.kind is EventKind.INSTRUCTIONS_RECEIVED:
        if payload.get("statement"):
            detail.append("your statement about the data, kept exactly as you "
                          "wrote it")
        elif payload.get("backend_names") is not None:
            names = payload.get("backend_names") or []
            ceiling = payload.get("residency_at_most")
            if ceiling:
                detail.append(f"process no further than {ceiling}")
            if names:
                detail.append(f"prefer {', '.join(names)}")
        elif payload.get("instruction"):
            detail.append(payload["instruction"][:300])

    elif event.kind is EventKind.VALIDATION_COMPLETED:
        findings = payload.get("findings") or []
        errors = [f for f in findings if f.get("severity") == "error"]
        detail.append(f"{len(errors)} problem(s) that would stop a deposit, "
                      f"{len(findings) - len(errors)} worth knowing about")
        if errors:
            concerning = "blocked"

    elif event.kind is EventKind.WORKFLOW_HALTED:
        detail.append(payload.get("reason", "no reason recorded"))
        concerning = "stopped"

    elif event.kind is EventKind.STEP_FAILED:
        detail.append(payload.get("error", ""))
        if payload.get("retrying_in_seconds"):
            detail.append(f"trying again in "
                          f"{payload['retrying_in_seconds']:.0f} seconds")
        concerning = "failed"

    elif event.kind is EventKind.DEPOSIT_COMPLETED:
        detail.append(f"identifier {payload.get('pid')}")
        for excluded in payload.get("excluded") or []:
            detail.append(f"{excluded} was not published, at your decision")

    elif event.kind is EventKind.OWNER_ADDED:
        detail.append(payload.get("owner", ""))
        if payload.get("reason"):
            detail.append(payload["reason"])

    elif event.kind is EventKind.REDACTION_DECIDED:
        detail.append(f"{payload.get('item_id', '')}: "
                      f"{payload.get('decision', '')}")
        if payload.get("reason"):
            detail.append(payload["reason"])

    else:
        # Anything without a hand-written reading shows its simple values only.
        # Never a dict or a list of dicts: a Python repr on a page is a log
        # file, which is the thing this module exists to stop.
        for key, value in payload.items():
            if key in INTERNAL_KEYS or isinstance(value, (dict, list)):
                continue
            if value is None or value == "":
                continue
            detail.append(f"{key.replace('_', ' ')}: {value}")

    return Told(what=what, who=who, detail=[d for d in detail if d],
                concerning=concerning)
