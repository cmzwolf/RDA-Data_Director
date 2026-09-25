"""Request and response shapes for the core API.

Requirement C5 states that every Data Director instance must expose a harmonised
API for core features so that instances can interact. The Blueprint does not say
what that API is, so this is a candidate offered for community discussion rather
than a conformance claim (§13).

Two things shaped it that could not have been known when Document A deferred it.

**A deposit has a long, resumable middle.** The repository holds a draft
indefinitely and validates only at publication, so the interesting states are not
"in progress" and "done" but a sequence a client may leave and return to. The
resource model therefore exposes job state as something to poll and act on,
never as the return value of a blocking call.

**Publication is the only irreversible transition.** Everything before it can be
retried, revised or abandoned; that one cannot. It is the only operation with
idempotency semantics spelled out in the contract.

Nothing here carries payload. Responses reference artefacts by identifier and
digest, as everything else in this system does (commitment C-3).
"""

from __future__ import annotations

from datetime import datetime

from datadirector_contracts import SensitivityClass
from pydantic import BaseModel, ConfigDict, Field


class ServiceDescription(BaseModel):
    """What this instance is, for a client that has just found it."""

    model_config = ConfigDict(frozen=True)

    service: str = "data-director"
    api_version: str
    implementation: str
    implements: str = "RDA Data Director Agentic AI Blueprint v1.0"
    deployment_profile: str
    documentation: str | None = None


class RequirementCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: str
    declared_status: str
    components: list[str] = Field(default_factory=list)


class Capabilities(BaseModel):
    """What this instance can do, so another can decide what to send it.

    This is the endpoint that makes inter-instance interaction possible at all.
    Two Directors cannot usefully exchange work without first establishing what
    the other is able to accept: which schemas it emits, which repositories it
    can deposit to, whether it can inspect images, where it processes data.
    Sending a deposit to an instance that cannot handle it and discovering so
    from an error is not interoperability.

    Residency is included because it is a precondition, not a detail: an
    instance that processes extra-jurisdiction must not be sent material another
    institution has classified as sensitive.
    """

    model_config = ConfigDict(frozen=True)

    coverage: list[RequirementCoverage]
    schemas_emitted: list[str] = Field(default_factory=list)
    repositories: list[str] = Field(default_factory=list)
    model_residencies: list[str] = Field(
        default_factory=list,
        description="Where this instance may process material, by sensitivity "
        "class. A precondition for sending it anything.")
    sensitivity_classes: list[str] = Field(default_factory=list)
    accepts_deposits: bool = False


class JobSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    step: str
    created_at: datetime | None = None
    sensitivity: str | None = None
    halted_reason: str | None = None
    terminal: str | None = None


class ArtefactSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    digest: str | None = None
    byte_size: int | None = None


class JobDetail(JobSummary):
    """Folded state. Always derived from the event log, never stored.

    A client that polls this after a restart, a crash or a week away gets the
    same answer, because the answer is a function of the log rather than of
    anything held in memory.
    """

    model_config = ConfigDict(frozen=True)

    event_count: int
    config_digest: str | None = None
    material: list[str] = Field(default_factory=list)
    deposit_pid: str | None = None
    gate_unresolved: int = 0
    blocks_deposit: bool = False


class EventView(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence: int
    kind: str
    occurred_at: datetime
    agent: str
    human: str | None = None
    payload: dict = Field(default_factory=dict)


class GateItemView(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    kind: str
    artefact: str | None = None
    summary: str
    detail: list[str] = Field(default_factory=list)
    permitted_decisions: list[str]
    resolved: bool = False
    resolved_by: str | None = None
    decision: str | None = None


class GateView(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[GateItemView]
    unresolved: int
    blocks_deposit: bool


class ResolveRequest(BaseModel):
    """One decision on one item.

    There is deliberately no list form. A client that could resolve forty items
    in one call has reviewed nothing, and the absence is the same contract the
    gate itself keeps (§9.7).
    """

    model_config = ConfigDict(frozen=True)

    decision: str
    reason: str | None = None


class ConfirmDeclarationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    sensitivity: SensitivityClass | None = Field(
        default=None,
        description="Omit to accept the most restrictive class. An absent "
        "statement is not a statement of openness (ADR-023).")
    accepted_claims: list[int] | None = Field(
        default=None,
        description="Claim indices to confirm; omit for all. Per item, as "
        "everywhere else a human decides.")
    note: str | None = None


class AddOwnerRequest(BaseModel):
    """Grant ownership to an ORCID.

    There is deliberately no corresponding removal request. Ownership is
    append-only, and the absence of the route is the contract.
    """

    model_config = ConfigDict(frozen=True)

    orcid: str = Field(description="The ORCID to add. Need not have signed in.")
    reason: str | None = Field(
        default=None,
        description="Why. Recorded, not enforced: 'added M. Aroa' is weaker "
        "than 'added M. Aroa, taking over while I am on leave', and the second "
        "is what an auditor reading it in two years needs.")


class DepositRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    repository: str
    confirm_irreversible: bool = Field(
        description="Must be true. Publication cannot be undone, and a client "
        "that reached this endpoint by accident should not succeed at it.")


class DepositResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    pid: str | None = Field(
        default=None,
        description="The version PID. Absent only where nothing was published, "
          "which happens when every artefact was excluded at the gate.")
    concept_pid: str | None = None
    landing_page: str | None = None
    already_published: bool = Field(
        default=False,
        description="True where this job had already been published and the "
        "existing identifiers were returned. Repeating a deposit is not an "
        "error: a client that lost the response must be able to ask again.")
    nothing_to_deposit: bool = Field(
        default=False,
        description="True where the researcher withheld every artefact at the "
          "gate, so the job closed without publishing anything. A decision "
          "honoured rather than a failure — hence a 200 with no PID, not a 500.")



class ProblemDetail(BaseModel):
    """Errors carry a code and a status.

    Learned from the repository driver, where a bare 'Not found.' left it open
    whether the endpoint was wrong, the record missing, or the operation
    refused. A message that cannot be acted on is barely better than none.
    """

    model_config = ConfigDict(frozen=True)

    status: int
    code: str
    detail: str
    requirement: str | None = Field(
        default=None,
        description="The Blueprint requirement this refusal enforces, where "
        "there is one. A refusal a client cannot trace to a rule looks like a "
        "defect.")
