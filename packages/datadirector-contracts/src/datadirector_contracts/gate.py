"""Gate items: everything awaiting a human decision, in one reviewable set.

Cluster 3 Part E. The governing arrangement is that **the workflow proceeds and
the gate blocks**. Every file is profiled, every proposal generated, and the
human reviews them together with the declaration and the DMP in view. Deposit
cannot commit while any item is unresolved.

Halting per file was the alternative and is worse: deciding "can we publish this
instrument file?" in isolation, as an interruption, is a poorer decision than
deciding it alongside everything else known about the deposit. It also makes a
deposit containing one unreadable file unusable, which no researcher would
tolerate and which would push people back to depositing without the tool.

Per-item decisions throughout, no bulk accept, each bound to an ORCID with a
reason. The rule from redaction (§9.5) generalises: a reviewer who can accept
forty items with one click has reviewed nothing.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .primitives import Orcid, utc_now
from .provenance import ReasonCode


class GateItemKind(StrEnum):
    CARE_REFERRAL = "care-referral"
    """CARE may apply and a person must consult the community concerned.

    An item rather than a warning because it must be acknowledged before
    deposit: the whole point is that the tool cannot resolve it, so it must not
    be able to proceed past it silently."""

    UNINSPECTED_FILE = "uninspected-file"
    REDACTION_PROPOSAL = "redaction-proposal"
    DECLARATION_DISCREPANCY = "declaration-discrepancy"
    DMP_DISCREPANCY = "dmp-discrepancy"

    LLM_OUTPUT = "llm-output"
    """Something a model wrote, put in front of a person before anything
    downstream reads it.

    An abstract, a README, a reading of a plan, a redaction proposal. The
    alternative was to let the next agent consume a draft as though it were a
    finding, which is how a plausible wrong value becomes a published value:
    each stage after the first has no way to tell model prose from evidence,
    and the researcher is the only reader who can. The item names the agent
    that wrote it and carries the digest of what it wrote, so an approval is
    bound to one version rather than to whatever the field happens to hold at
    deposit time.
    """


class Treatment(StrEnum):
    """What a redaction would do. Proposed, never applied without approval."""

    SUPPRESS = "suppress"
    GENERALISE = "generalise"
    PSEUDONYMISE = "pseudonymise"
    COARSEN = "coarsen"


class ItemDecision(StrEnum):
    """What a human decided about one item."""
    """What a human decided about one item.

    `PUBLISH_AS_IS` on an uninspected file is the interesting one: it is a
    person accepting responsibility for material nobody looked at, and it is
    recorded as exactly that rather than as an absence of concern.
    """

    APPROVE = "approve"
    REJECT = "reject"
    CONSULTED = "consulted"
    """A referral was acted on: the community or governance body was consulted.

    Distinct from `approve`, which would suggest the tool had judged something.
    Here the tool judged nothing; a person did the thing it asked for."""

    NOT_APPLICABLE = "not-applicable"
    """A person determined the referral does not apply to this material."""

    ACKNOWLEDGE = "acknowledge"
    """A person has read a report; the tool is not asking them to decide.

    Every other decision here changes something: an approval validates a draft,
    an exclusion takes a file out of the deposit. This one is for the items that
    are reports rather than questions, where a yes/no pair would be an
    invention — an item asking whether to deposit somewhere, when nothing
    downstream reads the answer and the destination is settled later by a
    different step, offers a vote that decides nothing. An item that carries no
    consequence should ask to be read, not for a verdict.

    Deliberately absent from `NON_VALIDATING_DECISIONS`: reading is not a
    request for another attempt, so the item does settle, and a job is not held
    open forever waiting on somebody to click past a notice. It is also never
    offered for a model's draft (`llm-output`), where attesting to content
    without writing anything would be precisely the rubber stamp that flag was
    added to prevent.
    """

    INSPECTED_EXTERNALLY = "inspected-externally"
    PUBLISH_AS_IS = "publish-as-is"
    EXCLUDE_FROM_DEPOSIT = "exclude-from-deposit"
    CLASSIFY_SENSITIVE = "classify-sensitive"

    EDITED = "edited"
    """A person wrote their own version of what a model produced.

    Distinct from `approve`: the machine's wording is not being accepted, it
    is being replaced. Recorded as a decision because the correction is the
    evidence, and because the item it closes is the review item for the draft
    that was corrected."""

    REQUEST_RERUN = "request-rerun"
    """Ask the model again, with what was wrong said back to it.

    **This is deliberately not a validation.** An earlier reading of this
    design treated any decision as clearing an item, which let a person press
    "try again" and have the unvalidated draft flow downstream while they were
    waiting for the second attempt. A request is an instruction to the tool,
    not a judgement about the output, so the item stays open until someone
    approves or edits what the model actually produced."""


NON_VALIDATING_DECISIONS = frozenset({ItemDecision.REQUEST_RERUN})
"""Decisions a person can record that do not attest to the output.

Read by everywhere that asks "is this item still open", so the rule lives in
one place: a request for another attempt leaves the item open, and an item
that stays open keeps the workflow behind it."""


def counts_as_validation(decision: ItemDecision) -> bool:
    """Whether this decision is a person standing behind the output."""
    return decision not in NON_VALIDATING_DECISIONS


def review_agent(item: "GateItem") -> str | None:
    """The agent whose model output a review item is bound to, from its id.

    A review item is identified ``llm-output:<agent>:<digest>``, so the agent
    segment groups the several drafts one agent has written. Returns nothing
    for an item that is not a model draft, since superseding makes no sense
    for a redaction proposal or a plan discrepancy: those are findings, not
    successive attempts at the same sentence.
    """
    if item.kind is not GateItemKind.LLM_OUTPUT:
        return None
    return review_agent_id(item.item_id)


def review_agent_id(item_id: str) -> str | None:
    """The agent segment of a model-draft item's id, or nothing.

    A review item is identified ``llm-output:<agent>:<digest>``, so the agent
    segment groups the several drafts one agent has written. Returns nothing for
    an id that is not a model draft, since superseding makes no sense for a
    redaction proposal or a plan discrepancy: those are findings, not successive
    attempts at the same sentence. The orchestrator's ``agent_of`` reads through
    this, so the identifier shape has one owner rather than a copy in each
    package that could drift on its own.
    """
    parts = item_id.split(":")
    if len(parts) < 3 or parts[0] != GateItemKind.LLM_OUTPUT.value:
        return None
    return parts[1] or None


class RedactionProposal(BaseModel):
    """One proposed change, with its evidence. Never applied on its own."""

    model_config = ConfigDict(frozen=True)

    artefact: str
    location: str = Field(description="Field name, row range, or character span.")
    reason_code: ReasonCode
    treatment: Treatment
    evidence: str = Field(
        description="Why this location. A description, not the content: the "
        "proposal travels through the open record.",
    )


class GateItem(BaseModel):
    """One thing a human must decide before deposit can proceed."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    kind: GateItemKind
    artefact: str | None = None
    summary: str
    detail: list[str] = Field(default_factory=list)
    proposal: RedactionProposal | None = None
    permitted_decisions: list[ItemDecision]

    def model_post_init(self, _context: object) -> None:
        if not self.permitted_decisions:
            raise ValueError(
                f"gate item {self.item_id} offers no decisions; an item a human "
                "cannot act on is an item that will be ignored"
            )


class Resolution(BaseModel):
    """A human's decision on one item. A human act, so it names one."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    decision: ItemDecision
    decided_by: Orcid
    reason: str | None = None
    decided_at: datetime = Field(default_factory=utc_now)


class GateState(BaseModel):
    """The set of items and their resolutions.

    `blocks_deposit` is the property everything else exists to support.
    """

    model_config = ConfigDict(frozen=True)

    items: list[GateItem] = Field(default_factory=list)
    resolutions: list[Resolution] = Field(default_factory=list)

    @property
    def resolved_ids(self) -> set[str]:
        # A request for another attempt is not a validation, so it does not
        # belong here: the draft it concerns is still unvalidated while the
        # second attempt is running, and nothing downstream may read it.
        return {r.item_id for r in self.resolutions
                if counts_as_validation(r.decision)}

    @property
    def superseded_ids(self) -> set[str]:
        """Drafts that a later draft from the same agent has replaced.

        Asking an agent to write again deliberately leaves the item on the
        draft being replaced open -- it must not flow downstream unseen while
        the second attempt is produced. That rule is right at the moment of
        the request and wrong forever after: once a person has validated a
        LATER draft from that agent, the thing they stood behind is the newest
        one, and the newest one is what every reader and the deposit read
        (`latest_drafted_record` returns the latest and only the latest). The
        superseded draft can no longer reach a deposit, so an unvalidated item
        on it must not keep the workflow, and the person, waiting on prose
        nobody will ever publish.

        The replacement releases the earlier draft, and only the replacement:
        a draft nobody has written again stays open, exactly as before. The
        rule lives here rather than beside the request-rerun path so every
        reader -- the workflow, the deposit check, the screen -- agrees on
        which items are still waiting, as `resolved_ids` already does.
        """
        latest = {}     # agent -> position of the newest validated draft
        for position, item in enumerate(self.items):
            agent = review_agent(item)
            if agent is not None and item.item_id in self.resolved_ids:
                latest[agent] = max(latest.get(agent, -1), position)
        superseded = set()
        for position, item in enumerate(self.items):
            agent = review_agent(item)
            if agent is None or item.item_id in self.resolved_ids:
                continue
            if position < latest.get(agent, -1):
                superseded.add(item.item_id)
        return superseded

    @property
    def unresolved(self) -> list[GateItem]:
        settled = self.resolved_ids | self.superseded_ids
        return [i for i in self.items if i.item_id not in settled]

    @property
    def blocks_deposit(self) -> bool:
        return bool(self.unresolved)

    def excluded_artefacts(self) -> list[str]:
        by_id = {i.item_id: i for i in self.items}
        return sorted({
            by_id[r.item_id].artefact
            for r in self.resolutions
            if r.decision is ItemDecision.EXCLUDE_FROM_DEPOSIT
            and by_id.get(r.item_id) and by_id[r.item_id].artefact
        })
