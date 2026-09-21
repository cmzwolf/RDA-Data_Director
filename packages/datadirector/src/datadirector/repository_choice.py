"""Choosing where to deposit, from three sources with different authority.

A repository is named in three ways, and conflating them is how a system either
ignores what a researcher asked for or obeys a sentence it found in a CSV.

  **The depositor's instruction.** "Publish this to EUDAT", typed by a signed-in
  person. This is a *directive*: they are accountable for the deposit, and the
  system's job is to do what they said or explain why it cannot.

  **The data management plan.** A promise made to a funder. Matched
  deterministically — a plan naming Zenodo needs no model to interpret.

  **A ranking, when both are silent.** Discipline fit, identifier support,
  certification, access conditions. This is judgement, and the only part a model
  contributes.

The trust boundary is the whole point. An instruction arrives through an
authenticated channel and is carried in `trusted_instructions`, structurally
separate from ingested material (ADR-024). The identical sentence inside a data
file is content and carries no authority at all — which is what stops a
submission from choosing its own repository.

**Extraction is never acted on unconfirmed.** A model reading "we should
probably avoid EUDAT this time" can readily produce `EUDAT`. So an extracted
name is put back to the depositor, and it is matched against the registry
shortlist rather than handed to a driver: a hallucinated repository then
resolves to nothing instead of to a plausible wrong endpoint.
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

EXTRACT_PROMPT = """\
The depositor has given instructions about their deposit. Identify whether they
named a repository to publish to.

Report only what they asked for. "Not EUDAT", "anywhere but Zenodo" and "I used
Dryad last time" are not requests to use those repositories, and reporting them
as such would send a deposit somewhere nobody chose.

If no repository is named, say so. A guess here is worse than a blank: the
depositor will be asked, and asking is cheap.

Return ONLY a JSON object:
{"repository": string|null,
 "verbatim": "the words they used, or null",
 "confidence": 0.0-1.0}
"""


class RepositoryPreference(BaseModel):
    """What the depositor appears to have asked for."""

    model_config = ConfigDict(frozen=True)

    named: str | None = None
    verbatim: str | None = Field(
        default=None,
        description="The depositor's own words. Shown back to them when "
        "confirming, because 'you asked for EUDAT' is checkable and 'a "
        "repository preference was detected' is not.")
    confidence: float = 0.0
    source: str = Field(default="instruction",
                        description="instruction | plan | ranking")


def from_plan(commitments) -> RepositoryPreference | None:
    """Read the repository from plan commitments. No model involved.

    A commitment is a structured field, not a sentence to interpret, so this is
    a lookup. Using a model here would introduce an error rate for no gain.
    """
    for commitment in commitments or []:
        if commitment.kind is CommitmentKind.REPOSITORY and commitment.value:
            return RepositoryPreference(named=commitment.value,
                                        verbatim=commitment.value,
                                        confidence=1.0, source="plan")
    return None


def from_instruction(pep, instruction: str, *,
                     classification: SensitivityClass = SensitivityClass.SENSITIVE
                     ) -> tuple[RepositoryPreference | None, DecisionRecord]:
    """Read a repository preference out of what the depositor wrote.

    The instruction is passed as `trusted_instructions` rather than as content:
    it came from an authenticated person, and the separation is what keeps the
    same words found in a file from carrying the same weight.
    """
    backend = pep.resolve_backend(classification,
                                  ModelCapability.TEXT_GENERATION)
    response = backend.complete(ModelRequest(
        system=EXTRACT_PROMPT,
        user_content="Identify the repository, if one was named.",
        trusted_instructions=instruction))

    data = _parse(response.text)
    named = data.get("repository")
    preference = None
    if named and str(named).strip().lower() not in ("null", "none", ""):
        try:
            confidence = min(1.0, max(0.0, float(data.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        preference = RepositoryPreference(
            named=str(named)[:120], verbatim=data.get("verbatim"),
            confidence=confidence, source="instruction")

    decision = DecisionRecord(
        agent="repository-choice/0.1.0", step="read-repository-preference",
        selected=preference.named if preference else None,
        selection_basis=(
            "read from the depositor's own instruction, which carries directive "
            "authority because they are accountable for the deposit; the same "
            "words inside submitted material would carry none"),
        undetermined=([] if preference else
                      ["no repository was named; the depositor will be asked"]),
        model_used=response.model_id)
    return preference, decision


def confirmation_item(preference: RepositoryPreference,
                      candidates: list) -> GateItem:
    """Put the interpretation back to the depositor before acting on it.

    Shows their own words next to what was understood, because "you asked for
    EUDAT" is something a person can check and "a preference was detected" is
    not.

    An empty candidate list means **the registry was not consulted**, not that
    it was consulted and found nothing. Saying "no repository called 'Zenodo'
    was found in the registry" when no registry had been asked is the failure
    this project keeps meeting from the other side: an unasked question
    reported as a negative answer, and here in a message someone reads while
    deciding.
    """
    detail = [f"you wrote: {preference.verbatim}"] if preference.verbatim else []
    if not candidates:
        detail.append(
            "The repository registry has not been consulted yet — that happens "
            "later in the workflow — so this name has not been checked against "
            "it.")
    elif _match(preference.named, candidates):
        detail.append(f"this matches {_match(preference.named, candidates)} in "
                      "the repository registry")
    else:
        detail.append(
            f"the registry was consulted and holds no repository called "
            f"{preference.named!r}. It may be spelled differently, or it may "
            "not have been what you meant.")
    return GateItem(
        item_id=f"repository-preference:{preference.named}",
        kind=GateItemKind.DMP_DISCREPANCY,
        summary=f"Deposit to {preference.named}?",
        detail=detail,
        permitted_decisions=[ItemDecision.APPROVE, ItemDecision.REJECT])


def divergence_item(instructed: RepositoryPreference,
                    committed: RepositoryPreference) -> GateItem | None:
    """The depositor asked for one repository and the plan promised another.

    Not an error and not an override: a plan is written at grant application,
    sometimes years before the data exist, and a researcher may have good reason
    to diverge. Flagged for the same reason every other plan discrepancy is
    flagged, and enforced for none of them.
    """
    if _same(instructed.named, committed.named):
        return None
    return GateItem(
        item_id=f"repository-divergence:{instructed.named}",
        kind=GateItemKind.DMP_DISCREPANCY,
        summary=(f"You asked to deposit in {instructed.named}; the data "
                 f"management plan commits to {committed.named}."),
        detail=[
            "Plans are written long before the data exist and reality "
            "legitimately diverges, so this is not refused.",
            "If the change is deliberate, your funder may expect the plan to be "
            "updated.",
        ],
        permitted_decisions=[ItemDecision.APPROVE, ItemDecision.REJECT])


def _same(left: str | None, right: str | None) -> bool:
    def normalise(value: str | None) -> str:
        return re.sub(r"[^a-z0-9]", "", (value or "").lower())
    a, b = normalise(left), normalise(right)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def _match(named: str | None, candidates: list) -> str | None:
    """Resolve a named repository against the registry shortlist.

    Matching rather than trusting: a name the registry does not know resolves to
    nothing, so a hallucinated repository cannot become an endpoint.
    """
    for candidate in candidates or []:
        if _same(named, getattr(candidate, "name", None)):
            return candidate.name
    return None


def _parse(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
