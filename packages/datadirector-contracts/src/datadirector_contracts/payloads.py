"""The three assertion payloads: declaration, DMP commitment, instruction.

These are the types that differ between the three document workflows. The
envelope in assertions.py is shared; what varies is the payload and what a
confirmed payload is permitted to affect.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .sensitivity import SensitivityClass


# --------------------------------------------------------------------------
# Declaration (Document A section 9.2)
# --------------------------------------------------------------------------


class LegalBasis(StrEnum):
    CONSENT = "consent"
    PUBLIC_TASK = "public-task"
    LEGITIMATE_INTEREST = "legitimate-interest"
    NOT_PERSONAL_DATA = "not-personal-data"
    OTHER = "other"


class DeclarationClaim(BaseModel):
    """One assertion from the responsibility and compliance statement.

    This is the input on which the whole confidentiality architecture depends.
    It is deliberately an assertion by an accountable human, not a detection.

    Sensitivity is carried in two separate fields because they have different
    epistemic status and conflating them discards signal (ADR-027):

      - `stated_sensitivity` is what the document says. Transcribed, never
        inferred. A document that does not use the words public, internal or
        sensitive states nothing, and nothing is what belongs here.
      - `inferred_sensitivity` is the agent's reading of what the document
        *describes*. A statement listing dates of birth and home addresses
        states no level while describing material any data steward would call
        sensitive.

    An inferred level may only ever tighten the outcome, never relax it, which
    is the same asymmetry the classification scan obeys (Document A section 9.3).
    """

    model_config = ConfigDict(frozen=True)

    stated_sensitivity: SensitivityClass | None = None
    inferred_sensitivity: SensitivityClass | None = None
    inference_indicators: list[str] = Field(
        default_factory=list,
        description="What in the document produced the inference, e.g. "
        "'dates of birth', 'consent excludes publication'. Shown to the human "
        "at the gate: an inference without its grounds is not reviewable.",
    )
    ethics_approval_reference: str | None = None
    ethics_approval_body: str | None = None
    legal_basis: LegalBasis | None = None
    third_party_rights: bool | None = None
    jurisdiction: str | None = Field(default=None, description="ISO 3166 code or free text.")
    embargo_until: date | None = None
    indigenous_data_indicated: bool | None = Field(
        default=None,
        description="Triggers CARE detection and referral only. The system does not "
        "assess CARE compliance (Document A section 3.4).",
    )


# --------------------------------------------------------------------------
# DMP commitments (Document A section 8.3)
# --------------------------------------------------------------------------


class CommitmentKind(StrEnum):
    REPOSITORY = "repository"
    LICENCE = "licence"
    EMBARGO = "embargo"
    METADATA_STANDARD = "metadata-standard"
    SHARING_INTENT = "sharing-intent"
    RETENTION = "retention"


class DmpCommitment(BaseModel):
    """Something promised to a funder. Distinct from what is legally permitted.

    A commitment is compared with the intended deposit; discrepancies are
    flagged for human review, never enforced. `sharing-intent: none` is a valid
    plan and produces the terminal state closed-not-shared.
    """

    model_config = ConfigDict(frozen=True)

    kind: CommitmentKind
    value: str
    plan_section: str | None = None


# --------------------------------------------------------------------------
# Instructions (Document A section 8.5)
# --------------------------------------------------------------------------


class InstructionKind(StrEnum):
    CONSTRAINT = "constraint"
    PRECEDENT = "precedent"
    GUIDANCE = "guidance"


class PrecedentScope(StrEnum):
    """What a precedent reference may transfer.

    METADATA_DECISIONS is the only permitted value. The enum exists so the
    restriction is visible in the type and so an attempt to widen it is a
    change someone must make deliberately (ADR-024).
    """

    METADATA_DECISIONS = "metadata-decisions"


class Instruction(BaseModel):
    """A user preference expressed within the space policy permits."""

    model_config = ConfigDict(frozen=True)

    kind: InstructionKind
    subject: str = Field(description="What it concerns: 'schema', 'licence', 'repository'.")
    value: str
    precedent_scope: PrecedentScope | None = None

    def model_post_init(self, _context: object) -> None:
        if self.kind is InstructionKind.PRECEDENT and self.precedent_scope is None:
            object.__setattr__(self, "precedent_scope", PrecedentScope.METADATA_DECISIONS)
