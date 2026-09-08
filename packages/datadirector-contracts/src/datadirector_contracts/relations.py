"""Related resources, and how a relation type is arrived at.

DataCite's relationType is a closed controlled vocabulary, and which value is
correct is three different kinds of question depending on the relation:

  - Structural relations are facts about our own data model. The link between a
    version and its predecessor follows from Dataset/DatasetVersion (ADR-021).
    Asking a model to infer these would be asking it to guess something we
    already know.
  - Semantic relations to external resources require judgement. Whether a
    dataset IsSupplementTo a paper, or the paper Documents the dataset, is not
    derivable from either object's structure. Here a model proposes and a human
    approves.
  - Nothing is ever free text. The vocabulary is closed.

The vocabulary itself is NOT hardcoded here. It is version-specific (4.5, 4.6
and 4.7 differ) and repository-specific (a repository may accept a subset), so
it is supplied by the SchemaProfile plugin and validated at binding time.
Freezing it in this package would silently pin every deployment to whichever
schema version happened to be current when this file was written.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .primitives import Orcid


class RelationOrigin(StrEnum):
    """How this relation's type was arrived at. Recorded per relation, because
    a reviewer asking 'why IsSupplementTo rather than Cites' deserves to know
    whether a human chose it or a model proposed it."""

    DERIVED = "derived"
    """A fact about our data model. Deterministic; no model involved."""

    MODEL_PROPOSED = "model-proposed"
    """Proposed by a model from the schema's vocabulary; requires approval."""

    RESEARCHER_SUPPLIED = "researcher-supplied"
    """Stated by the depositor, via instruction or at an approval gate."""


class RelatedResource(BaseModel):
    """One entry destined for a relatedIdentifier in the emitted record.

    `relation_type` is a plain string, deliberately: it is validated against the
    vocabulary the SchemaProfile plugin declares for its schema version, not
    against an enum frozen in this package.
    """

    model_config = ConfigDict(frozen=True)

    identifier: str = Field(description="The related resource's identifier value.")
    identifier_type: str = Field(description="DOI, URL, Handle, arXiv, SWHID, ...")
    relation_type: str = Field(
        description="A value from the target schema's relationType vocabulary. "
        "Validated at binding time; never free text."
    )
    resource_type_general: str | None = None
    origin: RelationOrigin
    proposed_by_model: str | None = None
    approved_by: Orcid | None = None
    rationale: str | None = Field(
        default=None, description="Why this relation type rather than a near neighbour."
    )

    def model_post_init(self, _context: object) -> None:
        if self.origin is RelationOrigin.MODEL_PROPOSED and self.approved_by is None:
            raise ValueError(
                "a model-proposed relation must be approved by a named human before "
                "it enters a record; direction errors (IsSupplementTo vs "
                "IsSupplementedBy) are not detectable downstream"
            )
        if self.origin is RelationOrigin.DERIVED and self.proposed_by_model is not None:
            raise ValueError("a derived relation is deterministic; no model may be credited for it")


class RelationVocabulary(BaseModel):
    """The permitted relation types for one schema version, supplied by a
    SchemaProfile plugin.

    `inverse_pairs` exists because direction is the most common error in this
    field and the one least visible after the fact: a record saying the dataset
    IsSupplementTo the paper and a record saying the reverse are both
    well-formed, and only one is true.
    """

    model_config = ConfigDict(frozen=True)

    schema_id: str
    schema_version: str
    permitted: frozenset[str]
    inverse_pairs: dict[str, str] = Field(
        default_factory=dict,
        description="e.g. IsSupplementTo -> IsSupplementedBy. Used to render the "
        "relation in plain language at the approval gate, so a human reads "
        "'this dataset supplements the paper' rather than a bare token.",
    )

    def validate_relation(self, relation_type: str) -> None:
        if relation_type not in self.permitted:
            raise ValueError(
                f"{relation_type!r} is not in the {self.schema_id} "
                f"{self.schema_version} relationType vocabulary"
            )
