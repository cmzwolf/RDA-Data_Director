"""Policy configuration and the Policy Enforcement Point contract.

Document A section 9.4. Every model call in the system passes through the PEP.
Agents never name a model backend; they ask for the most restrictive permitted
one and the PEP resolves it. A misconfigured agent cannot bypass policy because
it has no way to express a bypass.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .primitives import Residency  # re-exported for callers of this module
from .sensitivity import SensitivityClass

if TYPE_CHECKING:  # avoids a cycle: plugins imports policy for Residency
    from .plugins import ModelBackend, ModelCapability


class NoBackendAction(StrEnum):
    HALT = "halt"
    """The only permitted value. Present so the alternative is nameable and refused."""


class BackendDeclaration(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    kind: str = Field(description="ollama | openai-compatible | anthropic | ...")
    endpoint: str | None = None
    residency: Residency
    capabilities: list[str] = Field(
        default_factory=list,
        description="Capability names this backend provides, e.g. ['vision']. "
        "Empty means text-generation and structured-output only. Declared rather "
        "than probed: a backend selected for a capability it lacks fails "
        "mid-workflow, which is the failure C7 forbids.",
    )
    timeout_seconds: float = Field(
        default=120.0,
        description="Large local models exceed a two-minute default on ordinary "
        "hardware; a 122B model on a short document can take longer still.",
    )


class PolicyConfig(BaseModel):
    """Owned by the data steward or research office. Auditable, version-controlled.

    Deliberately separate from wiring configuration (ADR-018): different owner,
    different rate of change, different audit obligation.
    """

    model_config = ConfigDict(frozen=True)

    backend_by_sensitivity: dict[SensitivityClass, list[str]]
    on_no_permitted_backend: NoBackendAction = NoBackendAction.HALT
    infer_sensitivity_from_declaration: bool = Field(
        default=True,
        description="Whether the declaration agent may form its own view of "
        "sensitivity from what a statement describes, in addition to "
        "transcribing what it states. On by default because it is what a data "
        "steward would do; an institution preferring a strict transcription "
        "posture can disable it. An inference can only ever tighten (ADR-027).",
    )
    oversight: dict[str, str] = Field(
        default_factory=dict,
        description="Workflow name -> 'human-in-the-loop' | 'human-on-the-loop' (P4).",
    )
    retention_days: dict[str, int] = Field(default_factory=dict)

    def model_post_init(self, _context: object) -> None:
        missing = set(SensitivityClass) - set(self.backend_by_sensitivity)
        if missing:
            raise ValueError(
                "policy must state permitted backends for every sensitivity class; "
                f"missing: {sorted(m.label for m in missing)}"
            )


class PolicyHalt(Exception):
    """No permitted backend exists for this classification.

    Raised rather than degrading to a permitted-but-weaker path. A deployment
    intending to handle sensitive material must configure a local backend; if
    it has not, refusing the work is the honest behaviour.
    """


class CapabilityUnavailable(PolicyHalt):
    """No backend permitted at this classification provides the capability needed.

    A subclass of PolicyHalt because the workflow consequence is the same, but
    distinguishable because the remedy is not: PolicyHalt means configure a
    backend with acceptable residency, this means configure one that can do the
    job. The message names both the classification and the missing capability.
    """


@runtime_checkable
class PolicyEnforcementPoint(Protocol):
    """The single choke point for model access.

    Note the absence of any method taking a backend name. That absence is the
    contract: it is what makes bypass inexpressible rather than merely
    forbidden.
    """

    def resolve_backend(self, classification: SensitivityClass,
                        capability: "ModelCapability") -> "ModelBackend":
        """Return the most restrictive permitted *and capable* backend.

        Ordering is load-bearing: policy filters first, capability narrows within
        the permitted set, residency orders what remains. Capability is a filter
        and never a selector, or the property that a bypass is inexpressible
        would be lost.
        """
        ...

    def permitted_backends(self, classification: SensitivityClass) -> list[str]:
        ...
