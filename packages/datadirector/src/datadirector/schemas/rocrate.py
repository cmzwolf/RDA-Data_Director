"""RO-Crate schema profile.

Cluster 4 Part A2. The second profile exists to test the claim that the internal
record is genuinely schema-independent. A canonical record that can only be
projected one way is a DataCite record wearing a different name, and that would
only become apparent at the second repository.

RO-Crate is JSON-LD over schema.org, so the projection is structurally different
from DataCite rather than a renaming: entities are separate nodes in a graph
with their own identifiers, not nested objects.
"""

from __future__ import annotations

from typing import Any

from datadirector_contracts import CanonicalRecord, CapabilityManifest

CONTEXT = "https://w3id.org/ro/crate/1.1/context"

# schema.org has no closed relation vocabulary in the DataCite sense. The
# mapping covers the relations that have a natural schema.org property; anything
# outside it is emitted as a generic mention rather than silently dropped.
RELATION_TO_PROPERTY = {
    "IsPartOf": "isPartOf", "HasPart": "hasPart",
    "IsSupplementTo": "isBasedOn", "Cites": "citation",
    "References": "citation", "IsDerivedFrom": "isBasedOn",
    "IsNewVersionOf": "isBasedOn", "IsDocumentedBy": "subjectOf",
}


class RoCrateProfile:
    SERVES = ("R2", "R5", "R6", "C5", "P6")
    schema_id = "RO-Crate"
    schema_version = "1.1"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="schema-ro-crate", version="0.1.0", protocol="SchemaProfile",
            requirements_supported=list(self.SERVES),
            schemas_emitted=["RO-Crate 1.1"],
            offline_capable=True, requires_network=False,
        )

    def required_fields(self) -> list[str]:
        return ["title"]

    def missing_required(self, record: CanonicalRecord) -> list[str]:
        return [] if record.title else ["title"]

    def project(self, record: CanonicalRecord) -> dict[str, Any]:
        graph: list[dict[str, Any]] = [
            {"@id": "ro-crate-metadata.json",
             "@type": "CreativeWork",
             "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"},
             "about": {"@id": "./"}},
        ]

        root: dict[str, Any] = {"@id": "./", "@type": "Dataset",
                                "name": record.title}
        abstracts = [d.text for d in record.descriptions
                     if d.kind.value == "Abstract"]
        if abstracts:
            root["description"] = abstracts[0]
        if record.publication_year:
            root["datePublished"] = str(record.publication_year)
        if record.language:
            root["inLanguage"] = record.language
        if record.version:
            root["version"] = record.version
        if record.subjects:
            root["keywords"] = [s.term for s in record.subjects]

        # People become graph nodes with their own identifiers, which is the
        # structural difference from DataCite: an ORCID is a node, not a nested
        # attribute of a creator object.
        authors = []
        for creator in record.creators:
            node_id = (f"https://orcid.org/{creator.orcid}" if creator.orcid
                       else f"#{creator.name.replace(' ', '-')}")
            authors.append({"@id": node_id})
            person: dict[str, Any] = {
                "@id": node_id,
                "@type": "Organization" if creator.is_organisation else "Person",
                "name": creator.name,
            }
            if creator.given_name:
                person["givenName"] = creator.given_name
            if creator.family_name:
                person["familyName"] = creator.family_name
            if creator.affiliations:
                person["affiliation"] = [
                    {"@id": a.ror or f"#{a.name.replace(' ', '-')}"}
                    for a in creator.affiliations
                ]
                for affiliation in creator.affiliations:
                    graph.append({
                        "@id": affiliation.ror or f"#{affiliation.name.replace(' ', '-')}",
                        "@type": "Organization", "name": affiliation.name,
                    })
            graph.append(person)
        if authors:
            root["author"] = authors

        if record.rights:
            licence_id = record.rights.uri or f"#licence-{record.rights.licence_id}"
            root["license"] = {"@id": licence_id}
            graph.append({"@id": licence_id, "@type": "CreativeWork",
                          "name": record.rights.licence_id})

        for related in record.related:
            prop = RELATION_TO_PROPERTY.get(related.relation_type, "mentions")
            root.setdefault(prop, []).append({"@id": related.identifier})
            graph.append({"@id": related.identifier, "@type": "CreativeWork",
                          "identifier": related.identifier,
                          "description": f"related as {related.relation_type}"})

        for funding in record.funding:
            node_id = funding.funder_ror or f"#{funding.funder_name.replace(' ', '-')}"
            root.setdefault("funder", []).append({"@id": node_id})
            graph.append({"@id": node_id, "@type": "Organization",
                          "name": funding.funder_name})

        graph.insert(1, root)
        return {"@context": CONTEXT, "@graph": graph}
