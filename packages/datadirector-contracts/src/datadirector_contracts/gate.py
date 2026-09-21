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

    INSPECTED_EXTERNALLY = "inspected-externally"
    PUBLISH_AS_IS = "publish-as-is"
    EXCLUDE_FROM_DEPOSIT = "exclude-from-deposit"
    CLASSIFY_SENSITIVE = "classify-sensitive"


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
        return {r.item_id for r in self.resolutions}

    @property
    def unresolved(self) -> list[GateItem]:
        return [i for i in self.items if i.item_id not in self.resolved_ids]

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
