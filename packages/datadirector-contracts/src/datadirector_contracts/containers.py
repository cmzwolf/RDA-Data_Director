"""Container formats: archives that may carry their own metadata.

A deposit often arrives as one archive holding data files, a README and
supplementary material. Unpacking it is deterministic and no model is involved:
extraction is where the archive attack surface lives (path traversal,
decompression bombs, symlinks escaping the root), and those are refused by
rules, not by judgement.

What a model does with the result is interpret the tree: which member is
documentation, which is data, whether this is one dataset or several.

Some containers are not opaque. RO-Crate carries ro-crate-metadata.json; BagIt
carries bag-info.txt and per-file checksums. Where a depositor has already done
descriptive work, it should be read rather than re-derived worse.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .primitives import ArtefactRef, Digest


class MemberRole(StrEnum):
    """What a member appears to be. Proposed by a model, confirmed by a human;
    file extension alone does not settle it."""

    DATA = "data"
    DOCUMENTATION = "documentation"
    METADATA = "metadata"
    CODE = "code"
    SUPPLEMENTARY = "supplementary"
    UNDETERMINED = "undetermined"


class ContainerMember(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str = Field(description="Path within the container, after safety normalisation.")
    digest: Digest
    byte_size: int = Field(ge=0)
    media_type: str | None = None
    role: MemberRole = MemberRole.UNDETERMINED


class ExtractionLimits(BaseModel):
    """Refusal thresholds. Exceeding one halts the workflow with a recorded
    reason rather than being worked around."""

    model_config = ConfigDict(frozen=True)

    max_total_uncompressed_bytes: int = 5 * 1024**3
    max_member_count: int = 10_000
    max_nesting_depth: int = 1
    allow_symlinks: bool = False
    allow_absolute_paths: bool = False


class ContainerProfile(BaseModel):
    """The structural profile of an unpacked container.

    Both digests are retained: the archive digest is what we can prove was
    submitted, the member digests are what we can prove was deposited. Since
    members are deposited expanded under one identifier, this lineage lives in
    the provenance chain and not in a relatedIdentifier: the archive has no
    identifier for one to point at.
    """

    model_config = ConfigDict(frozen=True)

    archive: ArtefactRef
    format_id: str = Field(description="zip | tar | bagit | ro-crate | ...")
    members: list[ContainerMember]
    declared_metadata: dict | None = Field(
        default=None,
        description="Metadata the container carried itself (RO-Crate JSON-LD, "
        "BagIt bag-info). Read rather than inferred where present.",
    )
    checksums_verified: bool | None = Field(
        default=None,
        description="For formats supplying their own checksums, whether ours agree. "
        "None where the format supplies none.",
    )


@runtime_checkable
class ContainerFormat(Protocol):
    """One container format. Detection and extraction are deterministic."""

    def detects(self, path: Path) -> bool:
        """Whether this plugin recognises the container. Marker-file based."""
        ...

    def extract(self, path: Path, destination: Path, limits: ExtractionLimits) -> ContainerProfile:
        """Unpack safely, or raise.

        Implementations MUST resolve every member path and refuse any that
        escapes `destination`, refuse symlinks unless limits permit them, and
        stop on exceeding any limit. An implementation that trusts member paths
        turns a dropped file into an arbitrary file write.
        """
        ...
