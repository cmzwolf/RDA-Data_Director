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

from ..gate.items import PROPOSAL_WORDS

LEVELS = {0: "public", 1: "internal", 2: "sensitive"}
# Sentences that already name the reader, so the page prints the
# identifier without a "by" that contradicts them.
SECOND_PERSON = ("You ", "Your ", "We took ")

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
    EventKind.REDACTION_PROPOSED: "Proposals were put to you for review",
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

# The treatment words live with the gate items (PROPOSAL_WORDS) so that the
# gate screen and this history page cannot drift into wording the other has
# not been reviewed for: a bare verb like "pseudonymise" never reaches a page.
NOTHING_APPLIED = ("Nothing has been masked or removed: these are proposals "
                    "only. This tool edits no file; a redaction, if one is "
                    "wanted, is carried out by a person outside it.")


def _proposal_line(item: dict) -> str | None:
    """One gate item as a line about what *would* change, never what did.

    The stored summary carries the agent's own treatment verb; where a
    proposal is present its fields are re-read and re-worded instead, so
    a proposal logged under the older summary is still shown for what it
    is.
    """
    proposal = item.get("proposal")
    if not isinstance(proposal, dict):
        summary = item.get("summary")
        return summary if isinstance(summary, str) else None
    treatment = str(proposal.get("treatment", "")).lower()
    words = PROPOSAL_WORDS.get(
        treatment, f"a treatment was proposed for the values of "
                     f"``{treatment or 'an unnamed location'}``")
    artefact = item.get("artefact") or proposal.get("artefact") or ""
    location = proposal.get("location") or "an unstated location"
    reason = proposal.get("reason_code") or "reason unstated"
    prefix = f"{artefact} · " if artefact else ""
    return f"Proposed: {prefix}{location} — {words} (reason: {reason})"



class Told(BaseModel):
    """One event, as a sentence and a few facts."""

    model_config = ConfigDict(frozen=True)

    what: str
    who: str | None = None
    detail: list[str] = Field(default_factory=list)
    # The person's own words, verbatim. `detail` is our sentence about the
    # event; this is what they typed, and the page quotes it rather than
    # confirming that something was typed.
    said: str | None = None
    # Set when the sentence already speaks to the reader as "you", so the
    # identifier after it is printed in parentheses rather than after a "by"
    # that makes it read as though the telling were done by someone else.
    second_person: bool = False
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
    said = None

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
        # Quoted, not announced. What was here stated only that a statement
        # exists. A person returning to this page weeks later is not checking
        # whether one exists, they are trying to remember what they said, and
        # only the words themselves help.
        if payload.get("statement"):
            what = "You told us what the data is about"
            said = payload["statement"]
        elif payload.get("backend_names") is not None:
            names = payload.get("backend_names") or []
            ceiling = payload.get("residency_at_most")
            if ceiling:
                detail.append(f"process no further than {ceiling}")
            if names:
                detail.append(f"prefer {', '.join(names)}")
        elif payload.get("instruction"):
            # Whole, not excerpted: a cut at three hundred characters is the same
            # evocation as a summary, with the added insult that it falls wherever
            # the count falls rather than where the sense does.
            what = "You told us what to do with it"
            said = payload["instruction"]

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
             # Their words, not ours, and written in a box three rows high:
              # quoted through the same path as a statement rather than flattened
              # into one line of the detail list.
            said = payload["reason"]

    elif event.kind is EventKind.REDACTION_PROPOSED:
        # An item put in front of a person should read as one. The
        # payload carries the item whole; the history page needs its
        # summary line, which the gate screen will show in full beside
        # the decision. The stored summary words the treatment as a
        # bare verb - "suppress", "pseudonymise" - and on a page of
        # things that happened that reads as an applied change. Nothing
        # is applied anywhere in this system, so the line is rebuilt
        # from the proposal itself, in the conditional, and the page
        # says so once beneath the list.
        proposed = False
        items = payload.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    line = _proposal_line(item)
                    if line:
                        detail.append(line)
                        proposed = True
        if proposed:
            detail.append(NOTHING_APPLIED)
        if payload.get("summary"):
            detail.append(payload["summary"])

    elif event.kind is EventKind.REDACTION_DECIDED:
        detail.append(f"{payload.get('item_id', '')}: "
                      f"{payload.get('decision', '')}")
        if payload.get("reason"):
            said = payload["reason"]

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
                concerning=concerning, said=said,
                second_person=what.startswith(SECOND_PERSON))
