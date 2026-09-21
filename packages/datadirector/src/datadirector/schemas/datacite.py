"""DataCite schema profile.

Cluster 4 Part A2. Projects the canonical record into DataCite's shape and
declares the relation vocabulary for the schema version in use.

The vocabulary is declared here rather than in the core because it differs
between schema versions and because a repository may accept only a subset
(ADR-026). Zenodo in particular takes a subset of DataCite and adds fields of
its own; `ZenodoDataCiteProfile` below states what Zenodo actually accepts
rather than what DataCite defines.
"""

from __future__ import annotations

from typing import Any

from datadirector_contracts import (
    CanonicalRecord, CapabilityManifest, RelationVocabulary,
)

# DataCite 4.6 relationType vocabulary.
DATACITE_46_RELATIONS = frozenset({
    "IsCitedBy", "Cites", "IsSupplementTo", "IsSupplementedBy", "IsContinuedBy",
    "Continues", "IsDescribedBy", "Describes", "HasMetadata", "IsMetadataFor",
    "HasVersion", "IsVersionOf", "IsNewVersionOf", "IsPreviousVersionOf",
    "IsPartOf", "HasPart", "IsPublishedIn", "IsReferencedBy", "References",
    "IsDocumentedBy", "Documents", "IsCompiledBy", "Compiles", "IsVariantFormOf",
    "IsOriginalFormOf", "IsIdenticalTo", "IsReviewedBy", "Reviews",
    "IsDerivedFrom", "IsSourceOf", "IsRequiredBy", "Requires", "IsObsoletedBy",
    "Obsoletes", "Collects", "IsCollectedBy",
})

INVERSE_PAIRS = {
    "IsSupplementTo": "IsSupplementedBy", "Cites": "IsCitedBy",
    "References": "IsReferencedBy", "Documents": "IsDocumentedBy",
    "IsPartOf": "HasPart", "IsVersionOf": "HasVersion",
    "IsNewVersionOf": "IsPreviousVersionOf", "IsDerivedFrom": "IsSourceOf",
    "Compiles": "IsCompiledBy", "Reviews": "IsReviewedBy",
    "Obsoletes": "IsObsoletedBy", "Collects": "IsCollectedBy",
    "Describes": "IsDescribedBy", "Continues": "IsContinuedBy",
    "Requires": "IsRequiredBy", "IsMetadataFor": "HasMetadata",
}

REQUIRED = ["title", "creators", "publication_year", "publisher", "resource_type"]


class DataCiteProfile:
    SERVES = ("R2", "R6", "R7", "R11", "C5", "P6")
    schema_id = "DataCite"
    schema_version = "4.6"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="schema-datacite", version="0.1.0", protocol="SchemaProfile",
            requirements_supported=list(self.SERVES),
            schemas_emitted=[f"{self.schema_id} {self.schema_version}"],
            offline_capable=True, requires_network=False,
        )

    def relation_vocabulary(self) -> RelationVocabulary:
        return RelationVocabulary(
            schema_id=self.schema_id, schema_version=self.schema_version,
            permitted=DATACITE_46_RELATIONS, inverse_pairs=dict(INVERSE_PAIRS))

    def required_fields(self) -> list[str]:
        return list(REQUIRED)

    def missing_required(self, record: CanonicalRecord) -> list[str]:
        """Which required fields are absent. Reported, never filled.

        A profile that supplied a default publisher to satisfy a schema would be
        changing what the human approved, which is the same objection as a
        driver that repairs metadata to satisfy a repository.
        """
        missing = []
        for field in REQUIRED:
            value = getattr(record, field, None)
            if value in (None, "", []):
                missing.append(field)
        return missing

    def project(self, record: CanonicalRecord) -> dict[str, Any]:
        out: dict[str, Any] = {
            "titles": [{"title": record.title}],
            "creators": [self._party(c) for c in record.creators],
            "types": {"resourceTypeGeneral": record.resource_type.value},
        }
        if record.publication_year:
            out["publicationYear"] = str(record.publication_year)
        if record.publisher:
            out["publisher"] = record.publisher
        if record.descriptions:
            out["descriptions"] = [
                {"description": d.text, "descriptionType": d.kind.value}
                for d in record.descriptions
            ]
        if record.subjects:
            out["subjects"] = [self._subject(s) for s in record.subjects]
        if record.contributors:
            out["contributors"] = [
                {**self._party(c),
                 **({"contributorType": c.role} if c.role else {})}
                for c in record.contributors
            ]
        if record.rights:
            entry: dict[str, Any] = {"rightsIdentifier": record.rights.licence_id}
            if record.rights.uri:
                entry["rightsUri"] = record.rights.uri
            if record.rights.statement:
                entry["rights"] = record.rights.statement
            out["rightsList"] = [entry]
        if record.dates:
            out["dates"] = [
                {"date": (f"{d.value.isoformat()}/{d.end.isoformat()}"
                          if d.end else d.value.isoformat()),
                 "dateType": d.kind.value}
                for d in record.dates
            ]
        if record.related:
            out["relatedIdentifiers"] = [
                {"relatedIdentifier": r.identifier,
                 "relatedIdentifierType": r.identifier_type,
                 "relationType": r.relation_type,
                 **({"resourceTypeGeneral": r.resource_type_general}
                    if r.resource_type_general else {})}
                for r in record.related
            ]
        if record.funding:
            out["fundingReferences"] = [
                {"funderName": f.funder_name,
                 **({"funderIdentifier": f.funder_ror,
                     "funderIdentifierType": "ROR"} if f.funder_ror else {}),
                 **({"awardNumber": f.award_number} if f.award_number else {}),
                 **({"awardTitle": f.award_title} if f.award_title else {})}
                for f in record.funding
            ]
        if record.language:
            out["language"] = record.language
        if record.version:
            out["version"] = record.version
        if record.formats:
            out["formats"] = list(record.formats)
        if record.sizes:
            out["sizes"] = list(record.sizes)
        return out

    @staticmethod
    def _party(party) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "name": party.name,
            "nameType": "Organizational" if party.is_organisation else "Personal",
        }
        if party.given_name:
            entry["givenName"] = party.given_name
        if party.family_name:
            entry["familyName"] = party.family_name
        if party.orcid:
            entry["nameIdentifiers"] = [{
                "nameIdentifier": f"https://orcid.org/{party.orcid}",
                "nameIdentifierScheme": "ORCID",
                "schemeUri": "https://orcid.org",
            }]
        if party.affiliations:
            entry["affiliation"] = [
                {"name": a.name,
                 **({"affiliationIdentifier": a.ror,
                     "affiliationIdentifierScheme": "ROR"} if a.ror else {})}
                for a in party.affiliations
            ]
        return entry

    @staticmethod
    def _subject(subject) -> dict[str, Any]:
        entry: dict[str, Any] = {"subject": subject.term}
        if subject.uri:
            entry["valueUri"] = subject.uri
        if subject.scheme:
            entry["subjectScheme"] = subject.scheme
        return entry


# Zenodo accepts a subset of DataCite relation types. This list is our current
# belief and is one of the things the live deposit run exists to check: a
# divergence between what we declare here and what the API accepts is a finding,
# not a bug to paper over.
ZENODO_RELATIONS = frozenset({
    "IsCitedBy", "Cites", "IsSupplementTo", "IsSupplementedBy", "IsContinuedBy",
    "Continues", "IsDescribedBy", "Describes", "IsNewVersionOf",
    "IsPreviousVersionOf", "IsPartOf", "HasPart", "IsReferencedBy", "References",
    "IsDocumentedBy", "Documents", "IsCompiledBy", "Compiles",
    "IsVariantFormOf", "IsOriginalFormOf", "IsIdenticalTo", "IsReviewedBy",
    "Reviews", "IsDerivedFrom", "IsSourceOf", "Requires", "IsRequiredBy",
    "IsObsoletedBy", "Obsoletes",
})


class ZenodoDataCiteProfile(DataCiteProfile):
    """What Zenodo actually accepts, as distinct from what DataCite defines.

    Found in live testing against the sandbox: DataCite requires `publisher` and
    Zenodo assigns it, so inheriting the requirement made preflight demand a
    field no depositor supplies and the repository would ignore. A profile that
    is stricter than its target is not safe, it is merely obstructive: it
    refuses deposits the repository would have accepted.
    """

    # DataCite's required set, minus what the repository supplies itself.
    REQUIRED_BY_ZENODO = ["title", "creators", "publication_year", "resource_type"]

    schema_id = "DataCite (Zenodo profile)"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="schema-datacite-zenodo", version="0.1.0",
            protocol="SchemaProfile", requirements_supported=["R2", "R6", "R7"],
            schemas_emitted=["DataCite 4.6 (Zenodo subset)"],
            offline_capable=True, requires_network=False,
        )

    def required_fields(self) -> list[str]:
        return list(self.REQUIRED_BY_ZENODO)

    def missing_required(self, record: CanonicalRecord) -> list[str]:
        return [field for field in self.REQUIRED_BY_ZENODO
                if getattr(record, field, None) in (None, "", [])]

    def relation_vocabulary(self) -> RelationVocabulary:
        return RelationVocabulary(
            schema_id=self.schema_id, schema_version=self.schema_version,
            permitted=ZENODO_RELATIONS,
            inverse_pairs={k: v for k, v in INVERSE_PAIRS.items()
                           if k in ZENODO_RELATIONS and v in ZENODO_RELATIONS})
