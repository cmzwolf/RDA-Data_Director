"""Executable contracts for the Data Director.

These types are the single source of truth for the interfaces described in the
architecture document. Where the document and this package disagree, this
package is correct and the document has a bug.

Reading order for a newcomer:
  primitives   identifiers, digests, references
  sensitivity  the classification lattice and its tightening asymmetry
  exposure     what payload was released to a model, and the budget on it
  probing      the closed vocabulary of what a model may ask to see
  media        images and audio, and the recorded state of not having looked
  gate         everything awaiting a human decision before deposit
  ownership    who may see and act on a job; append-only
  retention    which working material may be deleted, and on what evidence
  assertions   the shared envelope for claims extracted from documents
  payloads     the three claim types: declaration, DMP, instruction
  events       the append-only log and its hash chain
  decisions    structured explanation
  provenance   PROV-O with confidentiality partitions
  policy       the Policy Enforcement Point contract
  relations    related resources and how a relation type is arrived at
  record       the canonical metadata record, projected to schemas at binding time
  containers   archives, safe extraction, self-describing formats
  plugins      the nine extension points
"""

from .primitives import ArtefactRef, Digest, MaterialClass, Orcid, Residency
from .sensitivity import Classification, SensitivityClass
from .ownership import (
    AccessRole,
    Ownership,
    OwnershipGrant,
)
from .retention import (
    AUTOMATIC,
    DeletionRecord,
    RetentionCandidate,
    RetentionCategory,
    RetentionMark,
    SurvivalCheck,
)
from .gate import (
    GateItem,
    GateItemKind,
    GateState,
    ItemDecision,
    RedactionProposal,
    Resolution,
    Treatment,
)
from .media import (
    InspectionTier,
    MediaFinding,
    UninspectedReason,
)
from .probing import (
    ProbeKind,
    ProbeRefused,
    ProbeRequest,
    ProbeResult,
)
from .exposure import (
    BudgetExceeded,
    Exposure,
    ExposureBudget,
    ReleaseKind,
)
from .assertions import Assertion, AssertionSet, AuthorityState, Channel, Evidence
from .payloads import DeclarationClaim, DmpCommitment, Instruction, InstructionKind
from .events import (
    EffectKind,
    Event,
    EventKind,
    HUMAN_ACTS,
    Reconciliation,
    StepIntent,
    StepOutcome,
    verify_chain,
)
from .decisions import DecisionRecord, FieldOrigin, FieldProvenance
from .provenance import ProvActivity, ProvenanceRecorder, ReasonCode, Visibility
from .containers import (
    ContainerFormat,
    ContainerMember,
    ContainerProfile,
    ExtractionLimits,
    MemberRole,
)
from .relations import RelatedResource, RelationOrigin, RelationVocabulary
from .record import (
    Affiliation,
    CanonicalRecord,
    Contributor,
    Creator,
    DateEntry,
    DateKind,
    Description,
    DescriptionKind,
    Funding,
    ResourceType,
    Rights,
    Subject,
)
from .policy import (
    BackendPreference,
    CapabilityUnavailable,
    PolicyConfig,
    PolicyEnforcementPoint,
    PolicyHalt,
    PreferenceUnsatisfiable,
)
from .plugins import (
    CapabilityManifest,
    DMPSource,
    IdentityProvider,
    ImageAttachment,
    ModelBackend,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    ValidationFinding,
    VocabularyTerm,
    DepositReceipt,
    RegistryDriver,
    RepositoryDriver,
    SchemaProfile,
    ValidatorDriver,
    VocabularyProvider,
)

__all__ = [n for n in dir() if not n.startswith("_")]
