"""The canonical metadata record.

Cluster 4 Part A. One internal representation, projected to a target schema at
binding time rather than generated directly in one.

Generating DataCite directly would be simpler and collapses on the second
repository. R6 requires the same dataset to be expressible in several standards,
and late binding is also what makes the crosswalk visible to the user rather
than implicit in the code.

This type lives in the contracts package because `SchemaProfile` plugins consume
it: a third party writing a profile for their institutional repository needs the
record definition and nothing else.

**Provenance is not optional.** Every populated field carries an entry in
`provenance` saying where the value came from and who contributed it. A record
in which nobody can say where a value originated fails C14, and the failure is
invisible unless the type insists.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .decisions import FieldProvenance
from .relations import RelatedResource


class ResourceType(StrEnum):
    DATASET = "Dataset"
    SOFTWARE = "Software"
    TEXT = "Text"
    IMAGE = "Image"
    COLLECTION = "Collection"
    OTHER = "Other"


class DescriptionKind(StrEnum):
    ABSTRACT = "Abstract"
    METHODS = "Methods"
    TECHNICAL_INFO = "TechnicalInfo"
    SERIES_INFORMATION = "SeriesInformation"
    OTHER = "Other"


class DateKind(StrEnum):
    COLLECTED = "Collected"
    CREATED = "Created"
    ISSUED = "Issued"
    UPDATED = "Updated"
    AVAILABLE = "Available"


class Affiliation(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    ror: str | None = Field(default=None, description="ROR identifier, if known.")


class Creator(BaseModel):
    """A person or organisation credited with the dataset.

    `orcid` is separate from the name because P5 wants persistent identifiers for
    people, and because two researchers share a name more often than anyone
    expects.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Display form, e.g. 'Aroa, Miriam'.")
    given_name: str | None = None
    family_name: str | None = None
    orcid: str | None = None
    affiliations: list[Affiliation] = Field(default_factory=list)
    is_organisation: bool = False


class Contributor(Creator):
    role: str | None = Field(
        default=None,
        description="Contributor type from the target schema's vocabulary, or a "
        "CRediT role. Projected per profile.",
    )


class Description(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    kind: DescriptionKind = DescriptionKind.ABSTRACT


class Subject(BaseModel):
    """A keyword, ideally from a controlled vocabulary.

    `uri` and `scheme` are what distinguish a term from a word. R3 asks for
    vocabulary and ontology suggestions; a subject with no URI is a suggestion
    that was not grounded, and the record says so rather than implying it was.
    """

    model_config = ConfigDict(frozen=True)

    term: str
    uri: str | None = None
    scheme: str | None = None

    @property
    def is_controlled(self) -> bool:
        return bool(self.uri and self.scheme)


class Rights(BaseModel):
    model_config = ConfigDict(frozen=True)

    licence_id: str = Field(description="SPDX identifier where one exists.")
    uri: str | None = None
    statement: str | None = None


class DateEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: DateKind
    value: date
    end: date | None = None


class Funding(BaseModel):
    model_config = ConfigDict(frozen=True)

    funder_name: str
    funder_ror: str | None = None
    award_number: str | None = None
    award_title: str | None = None


class CanonicalRecord(BaseModel):
    """The internal record. Frozen: revision produces a new one.

    Freezing is what lets the approval gate compare two states rather than trust
    that nothing changed between display and deposit.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    creators: list[Creator] = Field(default_factory=list)
    publication_year: int | None = None
    publisher: str | None = None
    resource_type: ResourceType = ResourceType.DATASET

    descriptions: list[Description] = Field(default_factory=list)
    subjects: list[Subject] = Field(default_factory=list)
    contributors: list[Contributor] = Field(default_factory=list)
    rights: Rights | None = None
    dates: list[DateEntry] = Field(default_factory=list)
    related: list[RelatedResource] = Field(default_factory=list)
    funding: list[Funding] = Field(default_factory=list)

    language: str | None = None
    version: str | None = None
    formats: list[str] = Field(default_factory=list)
    sizes: list[str] = Field(default_factory=list)

    provenance: dict[str, FieldProvenance] = Field(
        default_factory=dict,
        description="Field path to its origin. A record where nobody can say "
        "where a value came from fails C14.",
    )

    def origin_of(self, field_path: str) -> FieldProvenance | None:
        return self.provenance.get(field_path)

    def ungrounded_subjects(self) -> list[Subject]:
        """Subjects with no vocabulary URI.

        Surfaced rather than dropped: R2 requires the system to state openly
        where no controlled vocabulary exists, and silently discarding an
        ungrounded term would hide that finding instead of reporting it.
        """
        return [s for s in self.subjects if not s.is_controlled]

    def fields_without_provenance(self) -> list[str]:
        """Populated fields whose origin was not recorded.

        Checked rather than assumed, because the omission is invisible in the
        record itself and only becomes visible when a reviewer asks why a value
        is there.
        """
        missing = []
        for name in ("title", "creators", "publication_year", "publisher",
                     "descriptions", "subjects", "rights", "language", "version"):
            value = getattr(self, name)
            if value in (None, [], ""):
                continue
            if name not in self.provenance:
                missing.append(name)
        return missing
