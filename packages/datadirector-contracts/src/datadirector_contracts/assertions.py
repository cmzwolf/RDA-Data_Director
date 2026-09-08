"""Assertions extracted from documents, and the authority they carry.

Document A handles three document types through what looked like three similar
mechanisms: the declaration statement (section 9.2), the Data Management Plan
(section 8.3), and the instruction file (section 8.5). Each is parsed into
structured claims, confirmed by a human, and only then acted upon.

Writing the contracts confirms they are one mechanism with three payloads. The
generic envelope is AssertionSet[T]; the payload types differ, and what a
confirmed set is permitted to affect differs, but the extraction, authority and
confirmation machinery is identical.

The rule this module enforces (commitment C-4, ADR-024): authority derives from
the channel an assertion arrived through, not from its content.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from .primitives import ArtefactRef, Orcid, utc_now


class Channel(StrEnum):
    """Where an assertion came from. This determines its authority."""

    WATCHED_FOLDER = "watched-folder"
    """Unauthenticated. A file here is material, whatever its filename."""

    AUTHENTICATED_API = "authenticated-api"
    """Token-bound to an ORCID."""

    PROMPT_WINDOW = "prompt-window"
    """Session-bound to an authenticated ORCID."""

    PARSED_DOCUMENT = "parsed-document"
    """Extracted by a model from a supplied document. Provisional by construction."""

    @property
    def is_authenticated(self) -> bool:
        return self in (Channel.AUTHENTICATED_API, Channel.PROMPT_WINDOW)


class AuthorityState(StrEnum):
    PROPOSED = "proposed"
    """Extracted but not yet confirmed. Has no effect on system behaviour."""

    CONFIRMED = "confirmed"
    """A human reviewed and accepted it. Bound to an ORCID."""

    AUTHENTICATED = "authenticated"
    """Arrived through an authenticated channel; no separate confirmation needed."""

    REJECTED = "rejected"


class Evidence(BaseModel):
    """Where in the source document a claim came from.

    Required for every extracted claim: a human asked to confirm a claim must
    be able to see what it was derived from, or the confirmation is theatre.
    """

    model_config = ConfigDict(frozen=True)

    source: ArtefactRef
    locator: str = Field(description="Page, line range, or offset within the source.")
    excerpt_digest: str | None = Field(
        default=None,
        description="Digest of the supporting passage. The passage itself may be "
        "confidential and is held in the restricted store.",
    )


PayloadT = TypeVar("PayloadT", bound=BaseModel)


class Assertion(BaseModel, Generic[PayloadT]):
    """One extracted claim, with its confidence and its evidence."""

    model_config = ConfigDict(frozen=True)

    payload: PayloadT
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)


class AssertionSet(BaseModel, Generic[PayloadT]):
    """A set of claims and the authority it carries.

    The invariant: only CONFIRMED or AUTHENTICATED sets have effect. Consumers
    call effective_payloads(), which refuses to return anything otherwise, so
    acting on unconfirmed claims is not expressible.
    """

    model_config = ConfigDict(frozen=True)

    channel: Channel
    state: AuthorityState
    assertions: list[Assertion[PayloadT]]
    author: Orcid | None = None
    confirmed_by: Orcid | None = None
    confirmed_at: datetime | None = None

    def model_post_init(self, _context: object) -> None:
        if self.state is AuthorityState.AUTHENTICATED:
            if not self.channel.is_authenticated:
                raise ValueError(
                    f"channel {self.channel} cannot yield AUTHENTICATED authority; "
                    "it does not identify an author"
                )
            if self.author is None:
                raise ValueError("an authenticated assertion set must name its author")
        if self.state is AuthorityState.CONFIRMED and self.confirmed_by is None:
            raise ValueError("a confirmed assertion set must name the confirming human")

    @property
    def has_effect(self) -> bool:
        return self.state in (AuthorityState.CONFIRMED, AuthorityState.AUTHENTICATED)

    def effective_payloads(self) -> list[PayloadT]:
        """The claims the system may act on.

        Raises rather than returning an empty list, because silently doing
        nothing is the failure mode that would let a proposed set be treated as
        confirmed without anyone noticing.
        """
        if not self.has_effect:
            raise PermissionError(
                f"assertion set is {self.state}; it carries no authority. "
                "Confirm it with a human before acting on it."
            )
        return [a.payload for a in self.assertions]

    def confirm(self, *, human: Orcid, accepted: list[int] | None = None) -> "AssertionSet[PayloadT]":
        """Convert a proposal into a confirmed set.

        `accepted` selects claim indices; None means all. Per-item selection is
        required by the redaction workflow (section 9.5, no bulk accept) and is
        available to every assertion type for consistency.
        """
        if self.state is not AuthorityState.PROPOSED:
            raise ValueError(f"only PROPOSED sets may be confirmed; this is {self.state}")
        keep = (
            self.assertions
            if accepted is None
            else [a for i, a in enumerate(self.assertions) if i in set(accepted)]
        )
        return AssertionSet[PayloadT](
            channel=self.channel,
            state=AuthorityState.CONFIRMED,
            assertions=keep,
            author=self.author,
            confirmed_by=human,
            confirmed_at=utc_now(),
        )
