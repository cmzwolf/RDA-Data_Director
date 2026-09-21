"""JSON Schema validation of a projected metadata record.

Requirement R4. The schema is supplied by the deployment rather than fetched:
a validator that downloads its own rules at deposit time can pass on Monday and
fail on Friday for reasons nobody chose, and R4's point is conformance to a
stated standard rather than to whatever the standard happens to be today.

Where a repository publishes its own rules, those are preferred over community
ones: the repository is the party that will actually refuse the deposit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from datadirector_contracts import CapabilityManifest, ValidationFinding


class JsonSchemaValidator:
    SERVES = ("R4",)
    def __init__(self, schemas: dict[str, dict] | None = None,
                 schema_dir: Path | str | None = None) -> None:
        self._schemas = dict(schemas or {})
        if schema_dir:
            for path in Path(schema_dir).glob("*.json"):
                try:
                    self._schemas[path.stem] = json.loads(
                        path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="validator-jsonschema", version="0.1.0",
            protocol="ValidatorDriver", requirements_supported=list(self.SERVES),
            schemas_emitted=sorted(self._schemas),
            offline_capable=True, requires_network=False,
        )

    def knows(self, schema_id: str) -> bool:
        return schema_id in self._schemas

    def validate(self, record: dict[str, Any], *,
                 schema_id: str) -> list[ValidationFinding]:
        schema = self._schemas.get(schema_id)
        if schema is None:
            # Reported, not silent. A validator asked for rules it does not have
            # and returning "no problems" would be indistinguishable from having
            # checked, which is the failure the media tier taught us to avoid.
            return [ValidationFinding(
                severity="info", field=None,
                message=(f"no JSON Schema is configured for {schema_id!r}; "
                         "this record was not schema-validated"),
                rule="validator-unavailable")]

        try:
            import jsonschema
        except ImportError:
            return [ValidationFinding(
                severity="info", field=None,
                message="jsonschema is not installed; this record was not "
                        "schema-validated (pip install 'datadirector[validate]')",
                rule="validator-unavailable")]

        validator = jsonschema.Draft202012Validator(schema)
        findings = []
        for error in sorted(validator.iter_errors(record),
                            key=lambda e: list(e.absolute_path)):
            field = ".".join(str(p) for p in error.absolute_path) or None
            findings.append(ValidationFinding(
                severity="error", field=field, message=error.message,
                rule=f"jsonschema:{schema_id}"))
        return findings


# A minimal DataCite-shaped schema, sufficient to catch the structural errors a
# repository refuses on. Not the full DataCite schema: that is large, versioned,
# and better supplied by a deployment than embedded here where it would go stale.
DATACITE_MINIMAL: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["titles", "creators", "publicationYear", "types"],
    "properties": {
        "titles": {
            "type": "array", "minItems": 1,
            "items": {"type": "object", "required": ["title"],
                      "properties": {"title": {"type": "string", "minLength": 1}}},
        },
        "creators": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object", "required": ["name"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "nameType": {"enum": ["Personal", "Organizational"]},
                    "nameIdentifiers": {
                        "type": "array",
                        "items": {"type": "object",
                                  "required": ["nameIdentifier",
                                               "nameIdentifierScheme"]},
                    },
                },
            },
        },
        "publicationYear": {"type": "string", "pattern": "^[0-9]{4}$"},
        "types": {"type": "object", "required": ["resourceTypeGeneral"]},
        "relatedIdentifiers": {
            "type": "array",
            "items": {"type": "object",
                      "required": ["relatedIdentifier", "relatedIdentifierType",
                                   "relationType"]},
        },
        "rightsList": {"type": "array", "items": {"type": "object"}},
    },
}
