"""Exposure accounting: what payload was released to a model, and how much.

Document A §7.3 and cluster 3 Part B. Every release of class-1 material to a
model is an exposure and is recorded. The ledger exists so that two questions
have answers after the fact:

  - what did a model see?
  - was any single job able to drain the dataset through repeated small reads?

The second is the reason for a budget. A model steered by injected content
should be able to exhaust its allowance, not exfiltrate without limit. Bounding
the probe vocabulary bounds what *kind* of read is possible; the budget bounds
*how much*, which the vocabulary alone cannot.

Only a description and a digest of the released content are retained, never the
content (class 3, ADR-005). The ledger can prove what was exposed without
holding the exposed material.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .primitives import Digest, Orcid, Residency, utc_now
from .sensitivity import SensitivityClass


class ReleaseKind(StrEnum):
    """What sort of release this was. Coded, so the ledger is queryable."""

    STRUCTURAL_PROFILE = "structural-profile"
    """Derived description only: column names, types, shapes. No values."""

    FIELD_SAMPLE = "field-sample"
    """Values from one named field."""

    AGGREGATE = "aggregate"
    """Counts or distributions. No values."""

    CONTENT_CHUNK = "content-chunk"
    """A span of a document that cannot be profiled structurally."""

    FULL_DOCUMENT = "full-document"
    """An entire artefact. The largest release the system can make."""

    MEDIA_CONTENT = "media-content"
    """An image or audio artefact sent to a multimodal backend."""


class Exposure(BaseModel):
    """One release of payload to a model.

    `content_digest` is the digest of what was sent, so a later dispute about
    what a model saw is decidable. The content itself is not kept.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    artefact_uri: str
    kind: ReleaseKind
    byte_count: int = Field(ge=0)
    content_digest: Digest
    classification: SensitivityClass
    backend: str
    residency: Residency
    detail: str | None = Field(
        default=None,
        description="Field name, chunk index, or probe that caused the release. "
        "A label, never content.",
    )
    authorised_by: Orcid | None = Field(
        default=None,
        description="Set where the release required an explicit human act: a "
        "whole artefact leaving on-premise infrastructure.",
    )
    occurred_at: datetime = Field(default_factory=utc_now)

    def model_post_init(self, _context: object) -> None:
        if self.detail is not None and len(self.detail) > 256:
            raise ValueError(
                "exposure detail is a label, not content; "
                f"{len(self.detail)} characters is too long to be a label"
            )


class ExposureBudget(BaseModel):
    """Limits on release, per job and per artefact.

    Two scopes because they fail differently. A per-artefact limit stops one
    file being read in its entirety through repeated sampling; a per-job limit
    stops the same trick spread across many files.
    """

    model_config = ConfigDict(frozen=True)

    max_bytes_per_job: int = 2 * 1024 * 1024
    max_bytes_per_artefact: int = 256 * 1024
    max_releases_per_job: int = 200
    max_sample_values: int = Field(
        default=20,
        description="Cap on values returned by a single field sample. A request "
        "above this is clamped and the clamping recorded, not refused: a "
        "truncated answer is still a useful answer.",
    )


class BudgetExceeded(Exception):
    """A release would exceed the budget.

    Halts the workflow with a recorded reason rather than truncating silently:
    a model that has stopped receiving data without being told will conclude the
    data is absent, which is a worse failure than stopping.
    """
