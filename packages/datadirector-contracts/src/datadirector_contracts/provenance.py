"""PROV-O provenance with confidentiality partitions.

Document A sections 7.4 and 7.5. The problem this solves: a complete provenance
record can itself disclose what it was meant to protect. Recording "the
informant's name was removed because she is the only midwife in the district"
defeats the redaction.

Three mechanisms: reasons are coded rather than free text, free-text
justification lives in a restricted store with only its digest in the chain,
and the graph is partitioned by visibility.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .primitives import ArtefactRef, Digest, Orcid, utc_now


class ReasonCode(StrEnum):
    """Controlled vocabulary for why material was withheld or altered.

    A code discloses the shape of what happened without disclosing content.
    """

    PERSONAL_DATA = "personal-data"
    THIRD_PARTY_RIGHTS = "third-party-rights"
    EMBARGO = "embargo"
    INDIGENOUS_GOVERNANCE = "indigenous-governance"
    COMMERCIAL = "commercial"


class Visibility(StrEnum):
    """Named graph partitions. Restricted by default (P3, P5)."""

    OPEN = "open"
    RESTRICTED = "restricted"
    CONFIDENTIAL = "confidential"


class ProvAgent(BaseModel):
    model_config = ConfigDict(frozen=True)

    software: str | None = Field(default=None, description="Agent identity and version.")
    human: Orcid | None = None
    acted_on_behalf_of: Orcid | None = None


class ProvActivity(BaseModel):
    """One recorded action.

    Entities are referenced by identifier and digest. The graph never contains
    payload; the type makes that structural rather than a matter of care.
    """

    model_config = ConfigDict(frozen=True)

    activity_id: str
    activity_type: str
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    agent: ProvAgent
    used: list[ArtefactRef] = Field(default_factory=list)
    generated: list[ArtefactRef] = Field(default_factory=list)
    reason_code: ReasonCode | None = None
    justification_digest: Digest | None = Field(
        default=None,
        description="Digest of free-text justification held in the restricted "
        "store. Presenting the text later and recomputing the digest proves it "
        "is the original, which is sufficient given tamper evidence (ADR-007).",
    )
    visibility: Visibility = Visibility.RESTRICTED

    def model_post_init(self, _context: object) -> None:
        if self.agent.software is None and self.agent.human is None:
            raise ValueError("an activity must name a responsible agent; none act anonymously")


@runtime_checkable
class ProvenanceRecorder(Protocol):
    """Deterministic middleware, not a model-driven agent (ADR-009).

    Provenance produced by a non-deterministic component is not audit evidence.
    No model appears anywhere in this contract.
    """

    def record(self, activity: ProvActivity) -> None: ...

    def export(self, *, up_to: Visibility) -> dict:
        """Emit subgraphs at or below a clearance level as PROV-O in JSON-LD.

        The result remains internally consistent because the restricted portions
        were only ever digests to begin with.
        """
        ...
