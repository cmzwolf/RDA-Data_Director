"""Which metadata standard to project into.

The profile was hard-coded. A plan committing to DataCite was parsed, stored and
never acted on; a depositor writing "use RO-Crate" was read past. Both appeared
to work, because DataCite is the default and asking for the default looks like
being obeyed.

The same three sources as the repository choice, with the same authority, and
for the same reasons.

  **The depositor's instruction** is a directive: they are accountable for the
  deposit. Carried through the authenticated channel, so the identical sentence
  inside a submitted file carries none.

  **The plan** commits to a standard in a structured field
  (`metadata_standard_id`), so matching it needs no model at all.

  **A default**, when both are silent.

An unknown standard is reported, never guessed. "Use Frobnicate 2.1" produces a
question, not a silent fall back to DataCite, because a deposit in the wrong
standard is harder to notice than a deposit that stopped and asked.
"""

from __future__ import annotations

import json
import re

from datadirector_contracts import (
    DecisionRecord, GateItem, GateItemKind, ItemDecision, ModelCapability,
    ModelRequest, SensitivityClass,
)
from datadirector_contracts.payloads import CommitmentKind
from pydantic import BaseModel, ConfigDict, Field

# What the deployment can actually emit, by the names people use for them.
KNOWN = {
    "datacite": "DataCite",
    "data-cite": "DataCite",
    "data cite": "DataCite",
    "ro-crate": "RO-Crate",
    "rocrate": "RO-Crate",
    "ro crate": "RO-Crate",
}

DEFAULT = "DataCite"

EXTRACT_PROMPT = """\
The depositor has given instructions about their deposit. Identify whether they
named a metadata standard to use.

Report only what they asked for. "Not DataCite" and "we used RO-Crate last time"
are not requests to use those standards, and reporting them as such would
produce a deposit in a format nobody chose.

If no standard is named, say so. A guess is worse than a blank: the depositor
will be asked, and asking is cheap.

Return ONLY a JSON object:
{"standard": string|null, "verbatim": "the words they used, or null"}
"""


class SchemaChoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    standard: str
    source: str = Field(description="instruction | plan | default")
    verbatim: str | None = None
    recognised: bool = Field(
        default=True,
        description="False where the name means nothing to this deployment. "
        "Reported rather than replaced: a deposit in the wrong standard is "
        "harder to notice than one that stopped and asked.")


def normalise(name: str | None) -> str | None:
    if not name:
        return None
    return KNOWN.get(re.sub(r"[^a-z0-9 -]", "", name.strip().lower()))


def from_plan(commitments) -> SchemaChoice | None:
    """Read the standard from plan commitments. No model involved.

    A commitment is a structured field, not a sentence to interpret.
    """
    for commitment in commitments or []:
        if commitment.kind is CommitmentKind.METADATA_STANDARD \
                and commitment.value:
            resolved = normalise(commitment.value)
            return SchemaChoice(
                standard=resolved or commitment.value, source="plan",
                verbatim=commitment.value, recognised=resolved is not None)
    return None


def from_instruction(pep, instruction: str, *,
                     classification: SensitivityClass = SensitivityClass.SENSITIVE
                     ) -> tuple[SchemaChoice | None, DecisionRecord]:
    backend = pep.resolve_backend(classification,
                                  ModelCapability.TEXT_GENERATION)
    response = backend.complete(ModelRequest(
        system=EXTRACT_PROMPT,
        user_content="Identify the metadata standard, if one was named.",
        trusted_instructions=instruction))

    named = _parse(response.text).get("standard")
    choice = None
    if named and str(named).strip().lower() not in ("null", "none", ""):
        resolved = normalise(str(named))
        choice = SchemaChoice(standard=resolved or str(named)[:80],
                              source="instruction",
                              verbatim=_parse(response.text).get("verbatim"),
                              recognised=resolved is not None)

    return choice, DecisionRecord(
        agent="schema-choice/0.1.0", step="read-metadata-standard",
        selected=choice.standard if choice else None,
        selection_basis=(
            "read from the depositor's own instruction, which carries directive "
            "authority; the same words inside submitted material would carry "
            "none"),
        undetermined=([] if choice else
                      ["no metadata standard was named; the default applies"]),
        model_used=response.model_id)


def resolve(instructed: SchemaChoice | None,
            committed: SchemaChoice | None) -> SchemaChoice:
    """The standard to use, in order of authority."""
    for candidate in (instructed, committed):
        if candidate is not None and candidate.recognised:
            return candidate
    return SchemaChoice(standard=DEFAULT, source="default")


def unrecognised_item(choice: SchemaChoice) -> GateItem:
    """A named standard this deployment cannot emit, as a question."""
    return GateItem(
        item_id=f"metadata-standard:{choice.verbatim or choice.standard}",
        kind=GateItemKind.DMP_DISCREPANCY,
        summary=(f"This installation cannot produce "
                 f"{choice.verbatim or choice.standard!r} metadata."),
        detail=[
            f"asked for in your {choice.source}",
            f"it can produce: {', '.join(sorted(set(KNOWN.values())))}",
            "Depositing in a standard nobody chose is harder to notice than a "
            "deposit that stopped and asked, so this is being put to you.",
        ],
        permitted_decisions=[ItemDecision.APPROVE, ItemDecision.REJECT])


def divergence_item(instructed: SchemaChoice,
                    committed: SchemaChoice) -> GateItem | None:
    """You asked for one standard, the plan promised another."""
    if instructed.standard == committed.standard:
        return None
    return GateItem(
        item_id=f"metadata-standard-divergence:{instructed.standard}",
        kind=GateItemKind.DMP_DISCREPANCY,
        summary=(f"You asked for {instructed.standard} metadata; the plan "
                 f"commits to {committed.standard}."),
        detail=["Flagged, not refused: plans are written long before the data "
                "exist and reality legitimately diverges.",
                "If the change is deliberate, your funder may expect the plan "
                "to be updated."],
        permitted_decisions=[ItemDecision.APPROVE, ItemDecision.REJECT])


def _parse(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
