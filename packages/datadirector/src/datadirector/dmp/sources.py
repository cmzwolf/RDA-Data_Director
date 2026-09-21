"""Data Management Plan sources.

Requirement R8. Three ways a plan reaches the system, in decreasing order of how
much can be trusted about the result:

  maDMP      a machine-actionable plan following the RDA Common Standard. The
             commitments are structured, so they are read rather than inferred.
  document   a PDF, Word or text plan at a path or URL. Commitments are
             extracted by a model and are provisional by construction.
  fixture    plans shipped with the tests, so the verification logic can be
             exercised without a plan service or a network.

The distinction matters downstream: a commitment read from a structured field is
a fact about the plan, and one extracted from prose is a reading of it. The DMP
agent treats them differently (§8.3).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import httpx
from datadirector_contracts import CapabilityManifest
from datadirector_contracts.payloads import CommitmentKind, DmpCommitment
from datadirector_contracts.primitives import ArtefactRef, Digest, MaterialClass

from ..errors import ExternalServiceError

MAX_PLAN_BYTES = 20 * 1024 * 1024


class DocumentDmpSource:
    SERVES = ("R8",)
    """A plan as a document, at a local path or a URL."""

    format_id = "dmp-document"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="dmp-document", version="0.1.0", protocol="DMPSource",
            requirements_supported=list(self.SERVES), offline_capable=True,
            requires_network=True,
        )

    def is_machine_actionable(self) -> bool:
        return False

    def fetch(self, reference: str) -> ArtefactRef:
        parsed = urlparse(reference)
        if parsed.scheme in ("http", "https"):
            try:
                response = httpx.get(reference, timeout=30.0,
                                     follow_redirects=True)
                response.raise_for_status()
                payload = response.content[:MAX_PLAN_BYTES]
            except Exception as exc:
                raise ExternalServiceError(
                    f"the data management plan at {reference} could not be "
                    f"retrieved ({type(exc).__name__}). A plan that cannot be "
                    "read is not a plan that permits everything: the workflow "
                    "proceeds without commitments to verify against, and says so."
                ) from None
            name = Path(parsed.path).name or "plan"
        else:
            path = Path(reference)
            if not path.exists():
                raise FileNotFoundError(f"no data management plan at {path}")
            payload = path.read_bytes()[:MAX_PLAN_BYTES]
            name = path.name

        return ArtefactRef(
            uri=f"dmp://{name}", material_class=MaterialClass.METADATA,
            digest=Digest.of_bytes(payload), byte_size=len(payload),
            description="data management plan (document)",
        )


class MaDmpSource:
    SERVES = ("R8",)
    """A machine-actionable plan following the RDA Common Standard.

    Commitments are read from named fields rather than extracted from prose, so
    they carry no extraction uncertainty. That is the whole reason the standard
    exists and the reason this source is preferred where a plan offers both.
    """

    format_id = "dmp-madmp"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="dmp-madmp", version="0.1.0", protocol="DMPSource",
            requirements_supported=list(self.SERVES), schemas_emitted=["RDA maDMP 1.1"],
            offline_capable=True, requires_network=True,
        )

    def is_machine_actionable(self) -> bool:
        return True

    def fetch(self, reference: str) -> ArtefactRef:
        parsed = urlparse(reference)
        if parsed.scheme in ("http", "https"):
            try:
                response = httpx.get(reference, timeout=30.0,
                                     follow_redirects=True)
                response.raise_for_status()
                payload = response.content
            except Exception as exc:
                raise ExternalServiceError(
                    f"the maDMP at {reference} could not be retrieved "
                    f"({type(exc).__name__})") from None
        else:
            payload = Path(reference).read_bytes()
        return ArtefactRef(
            uri=f"dmp://{Path(parsed.path or reference).name}",
            material_class=MaterialClass.METADATA,
            digest=Digest.of_bytes(payload), byte_size=len(payload),
            description="data management plan (machine-actionable)",
        )

    def commitments(self, reference: str) -> list[DmpCommitment]:
        """Read commitments from the structured fields.

        Only fields the standard defines are read. A plan that says nothing
        about licensing yields no licensing commitment, rather than a default
        that would later be verified against and appear to have been promised.
        """
        parsed = urlparse(reference)
        raw = (httpx.get(reference, timeout=30.0, follow_redirects=True).text
               if parsed.scheme in ("http", "https")
               else Path(reference).read_text(encoding="utf-8"))
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{reference} is not valid JSON: {exc}") from None

        dmp = document.get("dmp", document)
        out: list[DmpCommitment] = []
        for dataset in dmp.get("dataset", []) or []:
            for distribution in dataset.get("distribution", []) or []:
                host = distribution.get("host") or {}
                if host.get("title"):
                    out.append(DmpCommitment(
                        kind=CommitmentKind.REPOSITORY, value=host["title"],
                        plan_section="dataset.distribution.host.title"))
                for licence in distribution.get("license", []) or []:
                    if licence.get("license_ref"):
                        out.append(DmpCommitment(
                            kind=CommitmentKind.LICENCE,
                            value=str(licence["license_ref"]),
                            plan_section="dataset.distribution.license"))
                    if licence.get("start_date"):
                        out.append(DmpCommitment(
                            kind=CommitmentKind.EMBARGO,
                            value=str(licence["start_date"]),
                            plan_section="dataset.distribution.license.start_date"))
                if distribution.get("data_access"):
                    out.append(DmpCommitment(
                        kind=CommitmentKind.SHARING_INTENT,
                        value=str(distribution["data_access"]),
                        plan_section="dataset.distribution.data_access"))
            for metadatum in dataset.get("metadata", []) or []:
                standard = (metadatum.get("metadata_standard_id") or {}).get(
                    "identifier")
                if standard:
                    out.append(DmpCommitment(
                        kind=CommitmentKind.METADATA_STANDARD,
                        value=str(standard),
                        plan_section="dataset.metadata.metadata_standard_id"))
            if dataset.get("preservation_statement"):
                out.append(DmpCommitment(
                    kind=CommitmentKind.RETENTION,
                    value=str(dataset["preservation_statement"])[:200],
                    plan_section="dataset.preservation_statement"))
        return out


class FixtureDmpSource:
    SERVES = ("R8",)
    """Plans shipped with the tests, for exercising verification offline."""

    format_id = "dmp-fixture"

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="dmp-fixture", version="0.1.0", protocol="DMPSource",
            requirements_supported=list(self.SERVES), offline_capable=True,
            requires_network=False,
        )

    def is_machine_actionable(self) -> bool:
        return True

    def fetch(self, reference: str) -> ArtefactRef:
        path = self.directory / reference
        payload = path.read_bytes()
        return ArtefactRef(
            uri=f"dmp://{path.name}", material_class=MaterialClass.METADATA,
            digest=Digest.of_bytes(payload), byte_size=len(payload),
            description="data management plan (test fixture)")

    def commitments(self, reference: str) -> list[DmpCommitment]:
        return MaDmpSource().commitments(str(self.directory / reference))
