"""Retention: deciding what working material may be deleted.

Working copies are class-1 material and are meant to be deleted once a deposit
is published (§7.3). Nothing did the deleting, and `retention_days` sat in the
policy configuration with nothing reading it.

One fact governs the design. **Our working copy may be the only copy.**
Ingestion copies from the watched folder; a researcher who then clears their own
folder has nothing else. Deleting on a guess does not free disk, it loses data.

So eligibility is derived from the event log rather than from the filesystem. A
modification time tells you about a file, not about a job: a researcher who left
work at the approval gate for three weeks is idle, not abandoned, and their
material is live.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .primitives import Digest, Orcid, utc_now


class RetentionCategory(StrEnum):
    RELEASED = "released"
    """Published, with a persistent identifier. The only category that is safe
    automatically, and only because a repository holds the material."""

    CLOSED_NOT_SHARED = "closed-not-shared"
    """A deliberate decision not to publish. Never deleted automatically: this
    is precisely the case where the institutional copy matters most, because
    nothing else holds it."""

    IDLE = "idle"
    """A live job with no recent activity. Reported, never acted on. Idle is not
    abandoned, and the difference is invisible from outside."""

    UNATTRIBUTED = "unattributed"
    """A directory with no job in the log. Reported only: we do not know what it
    is, and that is a reason for caution rather than for deletion."""

    ACTIVE = "active"
    """Recent activity. Not a candidate at all."""


AUTOMATIC = frozenset({RetentionCategory.RELEASED})
"""Categories a scheduled run may delete without a person deciding."""


class SurvivalCheck(BaseModel):
    """Whether the material still exists somewhere else.

    A persistent identifier in our log is our belief; a response from the
    repository is evidence. The distinction matters because a withdrawn record
    turns a safe deletion into a permanent loss.
    """

    model_config = ConfigDict(frozen=True)

    checked: bool
    survives: bool | None = Field(
        default=None,
        description="None where the repository could not be asked. An unasked "
        "question is not a negative answer, and neither is it a positive one.")
    detail: str = ""


class RetentionCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str | None
    path: str
    category: RetentionCategory
    byte_size: int = 0
    file_count: int = 0
    last_event_at: datetime | None = None
    pid: str | None = None
    reason: str
    survival: SurvivalCheck | None = None

    @property
    def deletable_automatically(self) -> bool:
        """Safe for a scheduled run, with no person in the loop.

        Requires the category to permit it *and* positive evidence that the
        material survives elsewhere. Either alone is not enough: the category
        says what we believe, the check says what is true.
        """
        return (self.category in AUTOMATIC
                and self.survival is not None
                and self.survival.survives is True)


class RetentionMark(BaseModel):
    """A deletion scheduled but not yet performed.

    Written into the directory as a tombstone before the grace period, so an
    operator who notices has a window and a researcher who returns finds an
    explanation rather than an absence.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str | None
    category: RetentionCategory
    reason: str
    marked_at: datetime = Field(default_factory=utc_now)
    delete_after: datetime
    pid: str | None = None
    marked_by: Orcid | None = Field(
        default=None,
        description="Set where a person ordered it rather than a schedule.")


class DeletionRecord(BaseModel):
    """What was removed, recorded after the material is gone.

    Digests and counts, never content: the audit trail describes the deletion
    without reconstructing what was deleted, which is the same discipline the
    provenance graph keeps (§7.5).
    """

    model_config = ConfigDict(frozen=True)

    job_id: str | None
    path: str
    category: RetentionCategory
    file_count: int
    byte_size: int
    digests: list[Digest] = Field(
        default_factory=list,
        description="Of the files removed, so a later question about what was "
        "deposited can still be answered against what was held.")
    pid: str | None = None
    deleted_at: datetime = Field(default_factory=utc_now)
    ordered_by: Orcid | None = None
