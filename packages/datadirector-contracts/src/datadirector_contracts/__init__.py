"""Executable contracts for the Data Director.

These types are the single source of truth for the interfaces described in the
architecture document. Where the document and this package disagree, this
package is correct and the document has a bug.

Reading order for a newcomer:
  primitives   identifiers, digests, references
  sensitivity  the classification lattice and its tightening asymmetry
  assertions   the shared envelope for claims extracted from documents
  payloads     the three claim types: declaration, DMP, instruction
  events       the append-only log and its hash chain
  decisions    structured explanation
  provenance   PROV-O with confidentiality partitions
  policy       the Policy Enforcement Point contract
  relations    related resources and how a relation type is arrived at
  containers   archives, safe extraction, self-describing formats
  plugins      the nine extension points
"""

from .primitives import ArtefactRef, Digest, MaterialClass, Orcid, Residency
from .sensitivity import Classification, SensitivityClass
from .assertions import Assertion, AssertionSet, AuthorityState, Channel, Evidence
from .payloads import DeclarationClaim, DmpCommitment, Instruction, InstructionKind
from .events import Event, EventKind, HUMAN_ACTS, verify_chain
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
from .policy import PolicyConfig, PolicyEnforcementPoint, PolicyHalt
from .plugins import (
    CapabilityManifest,
    DMPSource,
    IdentityProvider,
    ModelBackend,
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
