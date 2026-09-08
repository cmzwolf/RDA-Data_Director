"""Identifiers, digests and references.

Nothing in this module knows about workflows. It exists so that every other
contract module refers to material, people and artefacts in one way.

Design rule (Document A, commitment C-3): confidential material is handled by
reference. A reference carries an identifier and a digest, never payload.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------

JobId = Annotated[str, Field(pattern=r"^job-[0-9A-HJKMNP-TV-Z]{26}$")]
DatasetId = Annotated[str, Field(pattern=r"^ds-[0-9A-HJKMNP-TV-Z]{26}$")]
VersionId = Annotated[str, Field(pattern=r"^dsv-[0-9A-HJKMNP-TV-Z]{26}$")]

_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def orcid_check_digit(first_fifteen: str) -> str:
    """ISO 7064 MOD 11-2 check digit, as used by ORCID.

    Present so that a mistyped identifier is caught at construction rather than
    at the first call to an external service. An ORCID is the subject of every
    accountability claim in this system; a typo in one silently misattributes
    a human act.
    """
    total = 0
    for ch in first_fifteen.replace("-", ""):
        total = (total + int(ch)) * 2
    remainder = total % 11
    result = (12 - remainder) % 11
    return "X" if result == 10 else str(result)


class Orcid(BaseModel):
    """A researcher's persistent identifier.

    Principle P5 requires human identifiers to be persistent identifiers.
    Every consequential act in the system is bound to one of these.
    """

    model_config = ConfigDict(frozen=True)

    value: str

    @field_validator("value")
    @classmethod
    def _check(cls, v: str) -> str:
        if not _ORCID_RE.match(v):
            raise ValueError(f"not a well-formed ORCID: {v!r}")
        if orcid_check_digit(v[:-1]) != v[-1]:
            raise ValueError(f"ORCID checksum failure: {v!r} (probable typo)")
        return v

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"https://orcid.org/{self.value}"


class Digest(BaseModel):
    """A SHA-256 digest, used for tamper evidence and for reference-by-hash."""

    model_config = ConfigDict(frozen=True)

    algorithm: str = "sha256"
    value: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def of_bytes(cls, data: bytes) -> "Digest":
        return cls(value=hashlib.sha256(data).hexdigest())

    @classmethod
    def of_canonical_json(cls, obj: Any) -> "Digest":
        """Digest of a canonical JSON serialisation.

        Canonical means: sorted keys, no insignificant whitespace, UTF-8. Two
        structurally equal objects must produce the same digest on any machine,
        or the hash chain is not portable and tamper evidence is worthless.
        """
        payload = json.dumps(
            obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )
        return cls.of_bytes(payload.encode("utf-8"))

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.algorithm}:{self.value}"


# --------------------------------------------------------------------------
# Material
# --------------------------------------------------------------------------


class MaterialClass(StrEnum):
    """The three lifecycles of Document A section 7.3 (ADR-005).

    DATA is deleted after deposit and replaced by a PID. METADATA is retained.
    DERIVED covers anything transmitted to a model: only a description and a
    digest are retained, never the artefact, so the system can prove what was
    exposed without holding the exposed material.
    """

    DATA = "data"
    METADATA = "metadata"
    DERIVED = "derived"


class ArtefactRef(BaseModel):
    """A reference to material. Never the material itself.

    This type is what makes commitment C-3 mechanical rather than aspirational:
    the event log and the provenance graph are typed to hold references, so
    payload cannot enter them by accident.
    """

    model_config = ConfigDict(frozen=True)

    uri: str = Field(description="Opaque locator, e.g. wrk://job-.../table-a.csv")
    material_class: MaterialClass
    digest: Digest
    media_type: str | None = None
    byte_size: int | None = Field(default=None, ge=0)
    description: str | None = Field(
        default=None,
        description="For DERIVED artefacts, what it was, since the artefact itself is not kept.",
    )


class Residency(StrEnum):
    """Where a model backend processes what is sent to it (principle P10).

    Defined here rather than in policy.py because both the policy layer and the
    plugin layer need it, and policy must not depend on plugins.
    """

    ON_PREMISE = "on-premise"
    IN_JURISDICTION = "in-jurisdiction"
    EXTRA_JURISDICTION = "extra-jurisdiction"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
