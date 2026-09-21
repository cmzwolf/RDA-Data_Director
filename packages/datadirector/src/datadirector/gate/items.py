"""Collecting and resolving gate items.

Cluster 3 Part E. The workflow proceeds; the gate blocks.
"""

from __future__ import annotations

from datadirector_contracts import (
    Event, EventKind, GateItem, GateItemKind, GateState, InspectionTier,
    ItemDecision, MediaFinding, Orcid, RedactionProposal, Resolution,
)

from ..errors import AuthorityError

UNINSPECTED_DECISIONS = [
    ItemDecision.INSPECTED_EXTERNALLY,
    ItemDecision.PUBLISH_AS_IS,
    ItemDecision.EXCLUDE_FROM_DEPOSIT,
    ItemDecision.CLASSIFY_SENSITIVE,
]

REDACTION_DECISIONS = [ItemDecision.APPROVE, ItemDecision.REJECT]

CARE_DECISIONS = [
    ItemDecision.CONSULTED,
    ItemDecision.NOT_APPLICABLE,
    ItemDecision.EXCLUDE_FROM_DEPOSIT,
]

DISCREPANCY_DECISIONS = [
    ItemDecision.APPROVE,
    ItemDecision.CLASSIFY_SENSITIVE,
    ItemDecision.EXCLUDE_FROM_DEPOSIT,
]


def from_media_findings(findings: list[MediaFinding]) -> list[GateItem]:
    """One item per file nobody looked at.

    The summary says plainly that the file was not inspected and that its
    classification is a presumption, because the alternative — an item that
    reads like a finding — invites approval on the assumption that someone
    checked.
    """
    items = []
    for finding in findings:
        if finding.tier is not InspectionTier.NONE:
            continue
        detail = list(finding.metadata_findings)
        detail.append(f"not inspected: {finding.uninspected_reason.value}")
        detail.append(f"presumed {finding.sensitivity.label} on the basis of medium "
                      f"alone, not on inspection")
        items.append(GateItem(
            item_id=f"uninspected:{finding.artefact}",
            kind=GateItemKind.UNINSPECTED_FILE,
            artefact=finding.artefact,
            summary=(f"{finding.artefact} was not inspected "
                     f"({finding.uninspected_reason.value}). Treated as "
                     f"{finding.sensitivity.label} by presumption."),
            detail=detail,
            permitted_decisions=UNINSPECTED_DECISIONS,
        ))
    return items


def care_referral_item(assessment) -> GateItem | None:
    """A CARE referral as a gate item, or nothing if CARE is not indicated.

    The permitted decisions deliberately exclude `approve`: approving would
    imply the tool had assessed something, and it has not. A person either
    consulted the community, determined it does not apply, or withdrew the
    material.
    """
    if not assessment.may_apply:
        return None
    detail = list(assessment.guidance)
    for category, terms in assessment.signals.items():
        detail.append(f"signal — {category}: {', '.join(terms)}")
    if assessment.declared_by_depositor:
        detail.append("the depositor's own statement indicated Indigenous data")
    return GateItem(
        item_id="care:referral",
        kind=GateItemKind.CARE_REFERRAL,
        summary=("This material may engage the CARE Principles. The tool cannot "
                 "assess that; a person must consult the community or governance "
                 "body concerned before deposit."),
        detail=detail,
        permitted_decisions=CARE_DECISIONS,
    )


def from_redaction_proposals(proposals: list[RedactionProposal]) -> list[GateItem]:
    """One item per proposed change. Never grouped: grouping is bulk accept by
    another name."""
    return [
        GateItem(
            item_id=f"redaction:{p.artefact}:{p.location}",
            kind=GateItemKind.REDACTION_PROPOSAL,
            artefact=p.artefact,
            summary=(f"{p.artefact} · {p.location}: {p.treatment.value} "
                     f"({p.reason_code.value})"),
            detail=[p.evidence],
            proposal=p,
            permitted_decisions=REDACTION_DECISIONS,
        )
        for p in proposals
    ]


def discrepancy_item(item_id: str, summary: str, detail: list[str],
                     kind: GateItemKind) -> GateItem:
    return GateItem(item_id=item_id, kind=kind, summary=summary, detail=detail,
                    permitted_decisions=DISCREPANCY_DECISIONS)


class Gate:
    SERVES = ("P4", "C13", "C14")
    """Holds items and records resolutions. Blocks deposit until all are decided."""

    def __init__(self, state: GateState | None = None) -> None:
        self.state = state or GateState()

    def add(self, items: list[GateItem]) -> None:
        existing = {i.item_id for i in self.state.items}
        new = [i for i in items if i.item_id not in existing]
        self.state = self.state.model_copy(
            update={"items": self.state.items + new})

    def resolve(self, item_id: str, decision: ItemDecision, *, human: Orcid,
                reason: str | None = None) -> Resolution:
        """Record one decision on one item.

        There is no method taking a list of item ids. The absence is the
        contract: a reviewer who can accept forty items with one call has
        reviewed nothing, and §9.5 forbids bulk accept for redaction
        specifically.
        """
        item = next((i for i in self.state.items if i.item_id == item_id), None)
        if item is None:
            raise KeyError(f"no gate item {item_id!r}")
        if decision not in item.permitted_decisions:
            raise AuthorityError(
                f"{decision.value!r} is not available for {item_id!r}; "
                f"permitted: {[d.value for d in item.permitted_decisions]}"
            )
        if decision is ItemDecision.CONSULTED and not (reason or "").strip():
            # Who was consulted is the substance of the decision; without it the
            # record says only that someone clicked past a referral.
            raise AuthorityError(
                f"{item_id!r}: recording a consultation requires naming who was "
                "consulted and what they said. Without that the record shows "
                "only that the referral was dismissed."
            )
        if decision is ItemDecision.PUBLISH_AS_IS and not (reason or "").strip():
            # Publishing material nobody inspected is a person accepting
            # responsibility, and a recorded reason is the least that should
            # cost.
            raise AuthorityError(
                f"{item_id!r}: publishing an uninspected artefact requires a "
                "recorded reason, since the decision is an acceptance of "
                "responsibility rather than a finding"
            )
        resolution = Resolution(item_id=item_id, decision=decision,
                                decided_by=human, reason=reason)
        self.state = self.state.model_copy(
            update={"resolutions": self.state.resolutions + [resolution]})
        return resolution

    def blocks_deposit(self) -> bool:
        return self.state.blocks_deposit

    def unresolved(self) -> list[GateItem]:
        return self.state.unresolved

    def events(self, job_id: str, agent: str) -> list[Event]:
        return [Event(sequence=1, job_id=job_id, kind=EventKind.REDACTION_PROPOSED,
                      agent=agent, payload={
                          "items": len(self.state.items),
                          "unresolved": len(self.state.unresolved),
                          "kinds": sorted({i.kind.value for i in self.state.items}),
                      })]

    def resolution_events(self, job_id: str, agent: str) -> list[Event]:
        return [
            Event(sequence=1, job_id=job_id, kind=EventKind.REDACTION_DECIDED,
                  agent=agent, human=r.decided_by, payload={
                      "item_id": r.item_id, "decision": r.decision.value,
                      "reason_recorded": bool(r.reason),
                  })
            for r in self.state.resolutions
        ]
