"""Where to deposit: three sources, three levels of authority.

The trust boundary is the point. An instruction from a signed-in depositor is a
directive; the same sentence inside a submitted file carries none.
"""

from __future__ import annotations

import json

import pytest
from datadirector_contracts import (
    Digest, ItemDecision, ModelCapability, ModelResponse, PolicyConfig,
    Residency, SensitivityClass,
)
from datadirector_contracts.payloads import CommitmentKind, DmpCommitment

from datadirector.agents.repository import Candidate
from datadirector.policy.pep import PolicyEnforcementPoint
from datadirector.repository_choice import (
    RepositoryPreference, confirmation_item, divergence_item, from_instruction,
    from_plan,
)


class Scripted:
    name = "scripted"

    def __init__(self, reply):
        self.reply = reply
        self.seen = []

    def residency(self):
        return Residency.ON_PREMISE

    def capabilities(self):
        return {ModelCapability.TEXT_GENERATION}

    def complete(self, request):
        self.seen.append(request)
        return ModelResponse(text=self.reply, model_id=self.name,
                             input_digest=Digest.of_bytes(b""))


def _pep(model):
    return PolicyEnforcementPoint(
        PolicyConfig(backend_by_sensitivity={
            level: ["m"] for level in SensitivityClass}), {"m": model})


def _named(name, confidence=0.9, verbatim=None):
    return json.dumps({"repository": name, "confidence": confidence,
                       "verbatim": verbatim or f"deposit in {name}"})


# -- the plan: deterministic, no model -------------------------------------

def test_a_plan_commitment_needs_no_model():
    """A commitment is a structured field, not a sentence to interpret. Using a
    model here would introduce an error rate for no gain."""
    commitments = [DmpCommitment(kind=CommitmentKind.REPOSITORY,
                                 value="Zenodo", plan_section="host.title")]
    preference = from_plan(commitments)
    assert preference.named == "Zenodo"
    assert preference.confidence == 1.0
    assert preference.source == "plan"


def test_a_silent_plan_names_nothing():
    assert from_plan([]) is None
    assert from_plan([DmpCommitment(kind=CommitmentKind.LICENCE,
                                    value="CC-BY-4.0")]) is None


# -- the depositor's instruction -------------------------------------------

def test_an_instruction_is_carried_as_a_directive_not_as_content():
    """The separation is what stops a submission from choosing its own
    repository."""
    model = Scripted(_named("EUDAT"))
    from_instruction(_pep(model), "Please deposit this in EUDAT.")
    request = model.seen[0]
    assert request.trusted_instructions == "Please deposit this in EUDAT."
    assert "EUDAT" not in request.user_content


def test_a_named_repository_is_read_from_the_instruction():
    preference, decision = from_instruction(_pep(Scripted(_named("EUDAT"))),
                                            "deposit in EUDAT please")
    assert preference.named == "EUDAT"
    assert decision.selected == "EUDAT"


def test_nothing_named_is_reported_as_nothing():
    """A guess is worse than a blank: the depositor will be asked, and asking is
    cheap."""
    model = Scripted(json.dumps({"repository": None, "confidence": 0.0}))
    preference, decision = from_instruction(_pep(model),
                                            "please hurry, the deadline is Friday")
    assert preference is None
    assert any("will be asked" in u for u in decision.undetermined)


def test_an_unreadable_reply_yields_no_preference():
    preference, _ = from_instruction(_pep(Scripted("I'm not sure")), "anything")
    assert preference is None


# -- interpretation is confirmed, never acted on silently ------------------

def test_the_depositors_own_words_are_shown_back():
    """'You asked for EUDAT' is checkable; 'a preference was detected' is not."""
    preference = RepositoryPreference(named="EUDAT",
                                      verbatim="put it in EUDAT this time")
    item = confirmation_item(preference, [Candidate(identifier="r1",
                                                    name="EUDAT B2SHARE")])
    assert any("put it in EUDAT this time" in d for d in item.detail)
    assert "EUDAT" in item.summary


def test_a_repository_the_registry_does_not_know_is_flagged_as_such():
    """A hallucinated repository resolves to nothing rather than to a plausible
    wrong endpoint."""
    preference = RepositoryPreference(named="Zonodo", verbatim="use Zonodo")
    item = confirmation_item(preference, [Candidate(identifier="r1",
                                                    name="Zenodo")])
    assert any("was found in the registry" in d or "no repository called" in d
               for d in item.detail)


def test_confirmation_offers_only_yes_or_no():
    item = confirmation_item(RepositoryPreference(named="EUDAT"), [])
    assert {d for d in item.permitted_decisions} == {ItemDecision.APPROVE,
                                                     ItemDecision.REJECT}


# -- divergence from the plan ----------------------------------------------

def test_asking_for_a_different_repository_than_the_plan_is_flagged():
    """Not an error and not an override: plans are written years before the
    data exist."""
    item = divergence_item(RepositoryPreference(named="EUDAT"),
                           RepositoryPreference(named="Zenodo", source="plan"))
    assert item is not None
    assert "EUDAT" in item.summary and "Zenodo" in item.summary
    assert any("not refused" in d for d in item.detail)
    assert any("funder" in d for d in item.detail)


def test_agreeing_with_the_plan_raises_nothing():
    assert divergence_item(RepositoryPreference(named="Zenodo"),
                           RepositoryPreference(named="Zenodo",
                                                source="plan")) is None


def test_matching_tolerates_formatting():
    """Flagging 'zenodo' against 'Zenodo' would train a reviewer to dismiss the
    flag."""
    for written in ("zenodo", "Zenodo ", "ZENODO"):
        assert divergence_item(
            RepositoryPreference(named=written),
            RepositoryPreference(named="Zenodo", source="plan")) is None
