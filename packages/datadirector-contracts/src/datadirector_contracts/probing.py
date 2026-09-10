"""The probe vocabulary: what a model may ask to see.

Cluster 3 Part C. The probe interface is the one place a steered model could
become an exfiltration channel, so its expressive power is bounded by
construction rather than by validating whatever is requested.

**Ownership: the system, and only the system.** This vocabulary is compiled into
the contracts package. It is not configuration and not user input. Each
alternative owner fails differently:

  - the researcher's channel *is* the injected document, so a directive adding
    a `read_file(path)` probe would make exfiltration trivial;
  - the deployment administrator has no workflow authority by design (§10), and
    a probe added once for convenience is a permanent widening nobody revisits.

A probe vocabulary is a capability boundary, and capability boundaries are never
safely configurable outward. Deployments may narrow: cap sample sizes, set byte
budgets, disable individual probes. Widening requires a code change, a test and
a version bump, which lands in the provenance chain through the resolved
configuration digest. Adding a probe requires an ADR recording what it reveals.

**Every argument names something already in job state.** Field names come from
the structural profile; artefacts come from registered material. Bounding the
verbs while leaving arguments free would leave the injection route open through
the arguments.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProbeKind(StrEnum):
    SAMPLE_FIELD = "sample_field"
    """Up to n values from one profiled column. The only probe returning values
    from a structured source."""

    DISTINCT_COUNT = "distinct_count"
    """Cardinality only. Reveals whether a column is an identifier without
    revealing an identifier."""

    VALUE_SHAPES = "value_shapes"
    """Character-class shapes. No values."""

    NULL_PATTERN = "null_pattern"
    """Null distribution. No values."""

    CROSS_TAB = "cross_tab"
    """Co-occurrence counts between two columns. No values.

    The probe that answers re-identification questions: how many respondents
    share a village and an occupation. Small cell counts are the finding."""

    READ_CHUNK = "read_chunk"
    """One chunk of a registered artefact.

    Takes an artefact reference, never a path: a path argument invites traversal
    and a chunk of an arbitrary file is close to unbounded read."""


VALUE_RETURNING: frozenset[ProbeKind] = frozenset({
    ProbeKind.SAMPLE_FIELD, ProbeKind.READ_CHUNK,
})
"""Probes that return payload and are therefore charged against the budget."""


class ProbeRequest(BaseModel):
    """One probe, as requested by a model. Validated before it runs."""

    model_config = ConfigDict(frozen=True)

    kind: ProbeKind
    field: str | None = None
    field_b: str | None = Field(default=None, description="Second field for cross_tab.")
    artefact: str | None = Field(
        default=None, description="Artefact URI from job state, for read_chunk.")
    index: int = Field(default=0, ge=0, description="Chunk index for read_chunk.")
    n: int = Field(default=10, ge=1, description="Sample size; clamped by policy.")
    because: str = Field(
        description="Why this probe. Recorded in the decision record so a "
        "reviewer can see what the agent was trying to establish, not merely "
        "what it looked at.",
    )


class ProbeResult(BaseModel):
    """What a probe returned, and what it cost."""

    model_config = ConfigDict(frozen=True)

    request: ProbeRequest
    returned: Any = Field(description="Values, counts or text, per probe kind.")
    byte_count: int = Field(ge=0)
    clamped_from: int | None = Field(
        default=None,
        description="Set when a sample size above the cap was reduced. Clamped "
        "and recorded rather than refused: a truncated answer is still useful.",
    )


class ProbeRefused(Exception):
    """A probe was refused, with the reason.

    Refusal is never silence: an empty result is indistinguishable from a
    genuine empty result, and a model told nothing will infer the wrong thing.
    """
