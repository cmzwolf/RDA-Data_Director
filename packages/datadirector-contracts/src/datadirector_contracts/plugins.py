"""Plugin protocols: the eight extension points.

Document A section 6.2. A plugin implements one protocol, for one external
system or one standard. Protocols are shared across agents, which is what keeps
the extension mechanism singular rather than per-agent.

These are Python Protocols, not web services (ADR-003). Requiring plugins to be
network services would preclude single-machine deployment and contradict
principles P11 and P14.

Correction to an earlier count: there are eight protocols, not nine.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .primitives import ArtefactRef, Digest, Orcid, Residency


class ModelCapability(StrEnum):
    """What a model backend can do, as distinct from what policy permits it to do.

    Declared in the manifest rather than inferred, so a deployment can be audited
    for whether it is able to inspect the material it holds. A deposit containing
    images at a classification whose permitted backends are all text-only is not
    a runtime surprise; it is a fact about the configuration, knowable at startup.
    """

    TEXT_GENERATION = "text-generation"
    VISION = "vision"
    AUDIO = "audio"
    LONG_CONTEXT = "long-context"
    STRUCTURED_OUTPUT = "structured-output"


class CapabilityManifest(BaseModel):
    """What a plugin declares about itself (Document A section 6.3).

    Two consequences. Part of the conformance matrix can be generated from what
    is actually installed. And at startup the system can report which Blueprint
    requirements the current configuration is capable of satisfying, so a
    deployment lacking a DMPSource says so rather than silently omitting R8.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    protocol: str
    requirements_supported: list[str] = Field(
        default_factory=list, description="Blueprint IDs, e.g. ['R2', 'R6']."
    )
    schemas_emitted: list[str] = Field(default_factory=list)
    offline_capable: bool = False
    residency: Residency | None = None
    requires_network: bool = True
    model_capabilities: list[ModelCapability] = Field(
        default_factory=list,
        description="For ModelBackend plugins only. Empty for every other protocol.",
    )


@runtime_checkable
class Plugin(Protocol):
    """Base contract. Every plugin declares its capabilities."""

    def manifest(self) -> CapabilityManifest: ...


# --------------------------------------------------------------------------


class DepositReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    pid: str = Field(description="The version PID, e.g. a DOI.")
    concept_pid: str | None = Field(
        default=None, description="Resolves to the latest version, where the repository has one."
    )
    landing_page: str | None = None
    deposited_at: str


@runtime_checkable
class RepositoryDriver(Plugin, Protocol):
    """One repository: authentication, pre-flight checks, upload, PID retrieval.

    Credentials are never passed as values. The driver requests them by scope
    from the credential broker, which injects at call time (section 6.4).
    """

    def preflight(self, record: dict[str, Any], artefacts: list[ArtefactRef]) -> list[str]:
        """Repository-specific checks. Returns human-readable problems; empty means ready."""
        ...

    def deposit(
        self, record: dict[str, Any], artefacts: list[ArtefactRef], *, on_behalf_of: Orcid
    ) -> DepositReceipt: ...

    def new_version_of(self, concept_pid: str) -> str | None:
        """Begin a new version of an existing deposit (Document A section 7.1)."""
        ...


@runtime_checkable
class SchemaProfile(Plugin, Protocol):
    """One metadata standard: emit it, and validate against it."""

    def schema_id(self) -> str: ...

    def project(self, canonical: dict[str, Any]) -> dict[str, Any]:
        """Late binding: canonical internal record -> this standard's shape."""
        ...

    def required_fields(self) -> list[str]: ...


class VocabularyTerm(BaseModel):
    model_config = ConfigDict(frozen=True)

    uri: str
    label: str
    scheme: str
    definition: str | None = None


@runtime_checkable
class VocabularyProvider(Plugin, Protocol):
    """One terminology service."""

    def search(self, query: str, *, scheme: str | None = None, limit: int = 10) -> list[VocabularyTerm]: ...

    def scheme_exists_for(self, domain: str) -> bool:
        """Requirement R2 obliges the system to state openly when no controlled
        vocabulary exists for a domain. This is how that is established."""
        ...


class ValidationFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    severity: str = Field(description="error | warning | info")
    field: str | None = None
    message: str
    rule: str | None = None


@runtime_checkable
class ValidatorDriver(Plugin, Protocol):
    def validate(self, record: dict[str, Any], *, schema_id: str) -> list[ValidationFinding]: ...


class ImageAttachment(BaseModel):
    """An image sent to a multimodal backend.

    Attached to the *user* turn and never to the system turn, for the same
    reason ingested text is: an image is untrusted content. Text rendered in an
    image — a whiteboard, a scanned memo, a caption — is instruction-shaped
    material that a vision model reads, and visual prompt injection is the
    direct analogue of the textual case (commitment C-4). Keeping images in the
    user position is what stops a photograph from issuing directives.
    """

    model_config = ConfigDict(frozen=True)

    media_type: str = Field(description="image/jpeg, image/png, ...")
    data_base64: str
    label: str | None = Field(
        default=None, description="Artefact name, for the record. Never content.")


class ModelRequest(BaseModel):
    """A request to a model backend.

    Constructed only by the PEP. Agents do not build these directly, which is
    why there is no backend name field: by the time a request exists, the
    backend has already been resolved by policy.
    """

    model_config = ConfigDict(frozen=True)

    system: str
    user_content: str
    trusted_instructions: str | None = Field(
        default=None,
        description="Instructions from an authenticated channel (ADR-024). Kept "
        "separate from user_content so that ingested material and directives are "
        "never concatenated into one undifferentiated prompt.",
    )
    images: list[ImageAttachment] = Field(
        default_factory=list,
        description="Requires a backend declaring the vision capability. Always "
        "user-turn content, never instruction.",
    )
    max_tokens: int = 4096


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    model_id: str
    input_digest: Digest = Field(
        description="Digest of what was sent. Retained; the content is not (ADR-005)."
    )


@runtime_checkable
class ModelBackend(Plugin, Protocol):
    """One model endpoint. Reached only through the PEP."""

    def residency(self) -> Residency: ...

    def capabilities(self) -> set[ModelCapability]:
        """What this backend can do. Never what it is permitted to do."""
        ...

    def complete(self, request: ModelRequest) -> ModelResponse: ...


@runtime_checkable
class RegistryDriver(Plugin, Protocol):
    """One registry of repositories or standards (FAIRsharing, re3data)."""

    def find_repositories(self, *, discipline: str | None = None, **criteria: Any) -> list[dict[str, Any]]: ...


@runtime_checkable
class DMPSource(Plugin, Protocol):
    """One route to Data Management Plan commitments.

    Implementations: a supplied document, a machine-actionable DMP, and test
    fixtures. Absence of a plan is not an assertion about the data; it means
    there are no commitments to verify against.
    """

    def fetch(self, reference: str) -> ArtefactRef: ...

    def is_machine_actionable(self) -> bool:
        """maDMP commitments are read structurally; document commitments are
        model-extracted and therefore provisional."""
        ...


@runtime_checkable
class IdentityProvider(Plugin, Protocol):
    """Authenticate a human. Identity only; authorisation is held locally."""

    def authorize_url(self, state: str) -> str: ...

    def exchange(self, code: str) -> Orcid: ...
