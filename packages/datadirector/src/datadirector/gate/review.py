"""Every model output, put in front of a person before anything reads it.

An agent that asks a model for prose has produced an assertion, not a finding.
The stages behind it cannot tell the two apart: the validation agent checks an
abstract for a missing year, not for a year that was invented, and the
documentation agent fills the record's own Description from a README it wrote
seconds earlier. Each hop makes a draft look more settled, and the only reader
who can tell model prose from evidence is the researcher.

So the orchestrator raises one review item per model output, carrying the
digest of what the model wrote, and refuses to run anything downstream of that
agent until a named person has validated it, written their own version of it,
or asked for another attempt. Asking again does not clear the item: in the
earlier design a person who pressed "try again" had an unvalidated draft
flowing downstream while they waited, because every recorded decision looked
like a validation.

Two things this deliberately does not do. It does not gate agents whose output
is already put to a person individually: redaction proposals and proposed
claims each have their own item, and a coarser item on top would be a second
thing to approve rather than more review. And it never rewrites a log entry in
place: asking again appends the second attempt beside the first, so a reader
can see that the abstract they approved is the abstract that was written.
"""

from __future__ import annotations

import hashlib
import json

from datadirector_contracts import (
    Event, EventKind, GateItem, GateItemKind, GateState, ItemDecision,
    Resolution,
)
from datadirector_contracts.gate import counts_as_validation

from ..errors import ConfigurationError
from ..gate.items import Gate
from ..job_handle import log_items

ITEM_PREFIX = "llm-output"

REVIEW_DECISIONS = [
    ItemDecision.APPROVE,
    ItemDecision.EDITED,
    ItemDecision.REQUEST_RERUN,
]
# The three answers a person can give about a model's draft. `approve` and
# `edited` are validations; `request-rerun` is not. That rule lives in
# GateState.resolved_ids rather than here, so every reader of the gate — the
# workflow, the deposit check, the screen — sees the same answer.


def digest_of(payloads):
    """A short digest of what the model asserted.

    Key-sorted, so two runs that write the same fields in a different order
    produce the same digest. A review item that moved on a re-serialisation
    would demand a fresh approval for a value nobody altered, and a gate that
    cries wolf is a gate that gets clicked through.
    """
    canonical = json.dumps(payloads, sort_keys=True, ensure_ascii=False,
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def item_id_for(agent, digest):
    """Content-addressed, on purpose.

    `llm-output:metadata` would survive every redraft, so approving the first
    abstract would silence the review of the third. The digest belongs in the
    identifier for the same reason the plan comparisons carry the values they
    compare: a changed output has to ask again.
    """
    return "{}:{}:{}".format(ITEM_PREFIX, agent, digest)


def review_item(agent, label, payloads, editable=True, edit_note="",
                detail=None):
    """One model output, held for a person.

    `payloads` is what the agent asserted, in the shape it sits on the log, so
    the digest names that content and nothing else. `edit_note` carries why a
    person may not write their own version: an unexplained missing button
    reads as an oversight rather than as a decision.
    """
    digest = digest_of(payloads)
    name = agent.split("/")[0]
    lines = list(detail or [])
    lines.append("written by {}; this item is bound to what it wrote ({}), "
                 "not to whatever the field holds later".format(agent, digest))
    lines.append("nothing downstream reads this until you validate it, edit "
                 "it, or ask for another attempt")
    if not editable and edit_note:
        lines.append(edit_note)
    return GateItem(
        item_id=item_id_for(name, digest),
        kind=GateItemKind.LLM_OUTPUT,
        summary="Check what {} wrote: {}".format(name, label),
        detail=lines,
        permitted_decisions=list(REVIEW_DECISIONS))


def agent_of(item_id):
    """The agent named in a review item's identifier."""
    if not item_id.startswith(ITEM_PREFIX + ":"):
        return None
    parts = item_id.split(":")
    return parts[1] if len(parts) > 1 else None



def review_resolutions(events):
    """Every decision on a review item, folded from the log.

    Decisions on other kinds of item are left to the core API's own reader:
    mixing the two would let a redaction decision silence a model's draft.
    """
    out = []
    for event in events:
        if event.kind is not EventKind.REDACTION_DECIDED or not event.human:
            continue
        item_id = event.payload.get("item_id") or ""
        if not item_id.startswith(ITEM_PREFIX + ":"):
            continue
        try:
            out.append(Resolution(
                item_id=item_id,
                decision=ItemDecision(event.payload.get("decision")),
                decided_by=event.human,
                reason=event.payload.get("reason"),
                decided_at=event.occurred_at))
        except (ValueError, KeyError):
            continue
    return out


def pending(events):
    """Model outputs nobody has validated yet.

    Folded from the log rather than kept beside it, so a restarted process
    still refuses to run the agent that was waiting. A request for another
    attempt is not in here: the draft it refers to is still unvalidated, which
    is exactly why the workflow stays stopped.
    """
    validated = set()
    for resolution in review_resolutions(events):
        if counts_as_validation(resolution.decision):
            validated.add(resolution.item_id)
    return [item for item in log_items(events)
            if item.kind is GateItemKind.LLM_OUTPUT
            and item.item_id not in validated]


def pending_agents(events):
    """Which agents have an output sitting unvalidated right now."""
    names = set()
    for item in pending(events):
        name = agent_of(item.item_id)
        if name:
            names.add(name)
    return names



def review_gate(events):
    """The gate as the log states it, restricted to review items.

    Every entry point that resolves a review item comes through here, so the
    command line and the browser refuse the same decisions for the same
    reasons: permitted decisions and required reasons are decided by
    `Gate.resolve`, which is the one place those rules live.
    """
    items = [item for item in log_items(events)
             if item.kind is GateItemKind.LLM_OUTPUT]
    return Gate(GateState(items=items,
                          resolutions=review_resolutions(events)))


def rerun_request(events, agent):
    """What the person said was wrong with the last draft.

    The newest request for this agent, verbatim, because it is what the model
    is told. A person who wrote "the village column must not be named" and got
    the same draft back can then see whether their words reached it.
    """
    for resolution in reversed(review_resolutions(events)):
        if (resolution.decision is ItemDecision.REQUEST_RERUN
                and agent_of(resolution.item_id) == agent):
            return resolution.reason
    return None


def review_note_event(job_id, agent, note, human, agent_id):
    """The reviewer's words about the draft, recorded as their own.

    Recorded on the authenticated channel, because the person who pressed the
    button was authenticated when they did it, and marked with a purpose so it
    is never mistaken for the submission instruction: `depositor_context`
    returns it separately, which is what lets "the abstract is wrong because X"
    reach a model without overwriting what the depositor originally asked for.
    """
    return Event(
        sequence=1, job_id=job_id, kind=EventKind.INSTRUCTIONS_RECEIVED,
        agent=agent_id, human=human,
        payload={"instruction": note[:2000],
                 "channel": "authenticated-depositor",
                 "purpose": "output-review-note", "about": agent})


def review_note(events):
    """The most recent reviewer's note about a draft, for a drafting prompt.

    `None` when nobody has complained about a draft yet, which is the ordinary
    case and must not read as "the depositor said nothing".
    """
    for event in reversed(events):
        if (event.kind is EventKind.INSTRUCTIONS_RECEIVED
                and event.payload.get("purpose") == "output-review-note"
                and event.payload.get("instruction")):
            return str(event.payload["instruction"])
    return None


def decision_event(job_id, item_id, decision, human, reason, agent):
    """A person's decision on one review item, in the shape the gate reads.

    Identical to what the core API records for any other item, because a
    decision recorded in a second shape is a decision that reads as an absence.
    """
    return Event(
        sequence=1, job_id=job_id, kind=EventKind.REDACTION_DECIDED,
        agent=agent, human=human,
        payload={"item_id": item_id, "decision": decision.value,
                 "reason": reason, "reason_recorded": bool(reason)})



def claimed_review(agent, job_id, outcome):
    """The review item for what one agent's model just wrote, if any.

    Built by the orchestrator rather than by the agent: an agent that raises its
    own gate item is an agent that can decide not to. What the agent supplies is
    the claim on its capabilities — what its model writes, which events carry
    it, and what a person may do about it. The content is read off the events
    the agent asked to append, so the digest names what actually reached the
    log rather than what the agent chose to describe.

    Three runs raise nothing. An agent with no model output to its name is not
    asked to validate prose it never wrote. An agent whose model wrote no fields
    this time — a metadata step that found nothing to add, a media step with no
    images in front of it — has nothing to put in front of a person either. And
    a payload whose newest entry carries a person's own identity is that
    person's writing, already authorized: asking somebody to validate their own
    words produces a click that means nothing.

    A claim that names no events and returns no draft is refused, because it
    cannot be checked. The ordinary failure here is a declaration that quietly
    stopped corresponding to the code, which is what `human_follows` became.
    """
    claim = agent.capabilities().llm_output
    if claim is None:
        return None
    review = dict(outcome.review or {})
    payloads = review.get("payloads")
    if payloads is None:
        if not claim.produces:
            raise ConfigurationError(
                 f"{agent.identity} declares model output without naming the "
                    "events that carry it or returning a draft, so the claim "
                    "cannot be checked and nothing may be built on it")
        payloads = [event.payload for event in outcome.events
                     if event.kind in claim.produces and not event.human]
    if not payloads:
        return None
    detail = list(review.get("detail") or [])
    return review_item(agent.identity, claim.label, payloads,
                        editable=claim.editable, edit_note=claim.edit_note,
                        detail=detail)
