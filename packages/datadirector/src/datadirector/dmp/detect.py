"""Finding a data management plan in what the researcher submitted.

A plan reaches the system three ways, and only one was previously supported.

  **A reference.** A path or a URL, supplied explicitly.
  **Inside the container.** The researcher exports from DMPonline or Argos,
  drops the file next to their data and zips the lot. This is the likeliest
  case in practice and was not handled at all.
  **Not at all.** Which is not permission (ADR-023).

Detection **proposes**; it does not conclude. The cost of being wrong is
verifying a deposit against commitments from somebody else's project, which is
worse than verifying against none, so anything short of certainty becomes a
question for the depositor.

Certainty has one form here: a file that parses as JSON and carries the RDA
Common Standard's structure is a maDMP, because nothing else looks like that. A
file merely *named* like a plan is a candidate.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

NAME_HINTS = re.compile(
    r"\b(dmp|data[\s_-]?management[\s_-]?plan|plan[\s_-]?de[\s_-]?gestion|"
    r"datenmanagementplan|piano[\s_-]?di[\s_-]?gestione)\b", re.IGNORECASE)

READABLE_SUFFIXES = {".json", ".pdf", ".docx", ".txt", ".md", ".rtf", ".odt"}

MAX_PROBE_BYTES = 5 * 1024 * 1024


class Confidence(StrEnum):
    CERTAIN = "certain"
    """Parses as a machine-actionable plan. Nothing else has that structure."""

    LIKELY = "likely"
    """Named like a plan, in a format a plan comes in. A guess, and treated as
    one: the depositor confirms before any commitment is read from it."""


class DmpCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    member: str = Field(description="Name as it appeared in the submission.")
    confidence: Confidence
    machine_actionable: bool
    why: str

    @property
    def needs_confirmation(self) -> bool:
        """Only certainty proceeds unasked.

        A likely candidate is put to the depositor, because verifying against
        the wrong project's commitments is worse than verifying against none.
        """
        return self.confidence is not Confidence.CERTAIN


def looks_machine_actionable(payload: bytes) -> bool:
    """Whether this is an RDA Common Standard plan.

    Checked by structure rather than by filename or by a schema fetch: the
    structure is distinctive, and a schema fetch at ingestion time would make
    detection depend on a network.
    """
    try:
        document = json.loads(payload.decode("utf-8", errors="strict"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(document, dict):
        return False
    plan = document.get("dmp")
    if not isinstance(plan, dict):
        return False
    # `dataset` is the field commitments are read from; a document with a `dmp`
    # key and nothing under it is not usable as a plan even if it is one.
    return isinstance(plan.get("dataset"), list)


def detect(root: Path | str, *, max_bytes: int = MAX_PROBE_BYTES
           ) -> list[DmpCandidate]:
    """Find plan candidates among extracted material.

    Returns every candidate rather than a best guess. Where two files could be
    the plan, that is a fact the depositor should see, not one for this function
    to resolve.
    """
    root = Path(root)
    if not root.exists():
        return []

    candidates: list[DmpCandidate] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        suffix = path.suffix.lower()
        if suffix not in READABLE_SUFFIXES:
            continue
        member = str(path.relative_to(root))
        size = path.stat().st_size
        if size == 0 or size > max_bytes:
            continue

        if suffix == ".json":
            try:
                payload = path.read_bytes()
            except OSError:
                continue
            if looks_machine_actionable(payload):
                candidates.append(DmpCandidate(
                    path=str(path), member=member,
                    confidence=Confidence.CERTAIN, machine_actionable=True,
                    why=("parses as an RDA Common Standard maDMP: a 'dmp' "
                         "object carrying a dataset list")))
                continue

        if NAME_HINTS.search(member):
            candidates.append(DmpCandidate(
                path=str(path), member=member, confidence=Confidence.LIKELY,
                machine_actionable=False,
                why=(f"named like a data management plan ({member}), but its "
                     "contents do not identify it as one")))

    # Certain candidates first: a structured plan read from named fields beats a
    # prose plan read by a model, and the caller should see that ordering.
    return sorted(candidates, key=lambda c: c.confidence is not Confidence.CERTAIN)


def gate_item_for(candidates: list[DmpCandidate]):
    """A candidate the depositor must confirm, as a gate item.

    "I think this is your data management plan" is exactly the kind of judgement
    the system does not make alone.
    """
    from datadirector_contracts import GateItem, GateItemKind, ItemDecision

    unconfirmed = [c for c in candidates if c.needs_confirmation]
    if not unconfirmed:
        return None
    first = unconfirmed[0]
    return GateItem(
        item_id=f"dmp-candidate:{first.member}",
        kind=GateItemKind.DMP_DISCREPANCY,
        artefact=first.member,
        summary=(f"{first.member} may be this project's data management plan. "
                 "Confirm before its commitments are checked against the "
                 "deposit."),
        detail=[first.why,
                "If this is not the plan, say so: verifying against another "
                "project's commitments is worse than verifying against none."]
        + [f"also found: {c.member} ({c.why})" for c in unconfirmed[1:]],
        permitted_decisions=[ItemDecision.APPROVE, ItemDecision.NOT_APPLICABLE])
