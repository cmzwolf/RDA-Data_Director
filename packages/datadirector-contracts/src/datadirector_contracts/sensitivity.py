"""Sensitivity classification and the tightening asymmetry.

This module carries the single most important invariant in the system
(Document A section 9.3): an automated component may raise the sensitivity of
material, never lower it. Lowering always requires a human act.

The invariant is enforced here, in the type, rather than in the agents that
use it. An agent cannot lower a classification because there is no method that
would let it.
"""

from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel, ConfigDict

from .primitives import Orcid, utc_now


class SensitivityClass(IntEnum):
    """A total order. Higher means more restrictive.

    IntEnum rather than StrEnum specifically so that comparison is defined and
    'more restrictive' is a fact about the type rather than a convention.
    """

    PUBLIC = 0
    INTERNAL = 1
    SENSITIVE = 2

    @property
    def label(self) -> str:
        return self.name.lower()


class Classification(BaseModel):
    """The current sensitivity of a job's material, and how it got there."""

    model_config = ConfigDict(frozen=True)

    level: SensitivityClass
    established_by: Orcid | None = None
    established_by_agent: str | None = None
    rationale: str | None = None

    def tighten(self, level: SensitivityClass, *, agent: str, rationale: str) -> "Classification":
        """Raise the classification. Available to automated components.

        Refuses to lower or to leave unchanged, so a call to tighten() that
        does not tighten is a programming error rather than a silent no-op.
        """
        if level <= self.level:
            raise ValueError(
                f"tighten() may only raise: {self.level.label} -> {level.label} refused. "
                "Lowering a classification requires relax_with_human_authority()."
            )
        return Classification(
            level=level,
            established_by=self.established_by,
            established_by_agent=agent,
            rationale=rationale,
        )

    def relax_with_human_authority(
        self, level: SensitivityClass, *, human: Orcid, rationale: str
    ) -> "Classification":
        """Lower the classification. Requires an identified human.

        The signature is the enforcement: there is no way to call this without
        supplying an ORCID, so a relaxation without an accountable person is
        not expressible.
        """
        if not rationale.strip():
            raise ValueError("relaxing a classification requires a recorded rationale")
        return Classification(
            level=level,
            established_by=human,
            established_by_agent=None,
            rationale=rationale,
        )
