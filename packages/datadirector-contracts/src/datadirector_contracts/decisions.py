"""Decision records: structured explanation instead of reasoning traces.

Document A commitment C-6 and ADR-006. Principle P8 asks for a chain-of-thought
window; we emit this instead, because reasoning traces are not reliably faithful
accounts of how an output was produced and are unavailable on some backends.

Every agent step emits one of these, whether or not a model was involved.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .primitives import ArtefactRef, Digest, Orcid


class FieldOrigin(StrEnum):
    """Where a metadata value came from. Required by C15 consistency checking
    and by C14 explainability."""

    INFERRED = "inferred"
    RESEARCHER_SUPPLIED = "researcher-supplied"
    PRECEDENT = "precedent"
    DMP_COMMITMENT = "dmp-commitment"
    REPOSITORY_DEFAULT = "repository-default"
    ABSENT_BY_DESIGN = "absent-by-design"
    """No value exists and none should be invented. Requirement R2 obliges the
    system to state openly when no controlled vocabulary exists for a domain;
    this makes that statement a field rather than a sentence in generated prose."""


class ConsideredOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    option: str
    rejected_because: str | None = None


class DecisionRecord(BaseModel):
    """What an agent consulted, what it chose, why, and what it could not determine."""

    model_config = ConfigDict(frozen=True)

    agent: str
    step: str
    inputs_consulted: list[ArtefactRef] = Field(default_factory=list)
    sources_consulted: list[str] = Field(
        default_factory=list, description="External services queried, by URI."
    )
    options_considered: list[ConsideredOption] = Field(default_factory=list)
    selected: str | None = None
    selection_basis: str = Field(description="The rule or evidence that selected it.")
    undetermined: list[str] = Field(
        default_factory=list,
        description="What the agent could not establish. Surfaced to the human "
        "rather than filled with a plausible guess.",
    )
    model_used: str | None = None
    input_digest: Digest | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class FieldProvenance(BaseModel):
    """Per-field origin, so a reviewer can ask why any single value is there."""

    model_config = ConfigDict(frozen=True)

    field_path: str
    origin: FieldOrigin
    contributed_by: Orcid | None = None
    credit_role: str | None = Field(
        default=None, description="CRediT role for human contributions (P5)."
    )
    decision_ref: str | None = None
