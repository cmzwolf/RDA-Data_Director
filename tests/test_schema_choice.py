"""Which metadata standard a deposit is projected into.

The profile was hard-coded, so a plan committing to DataCite was parsed and
discarded, and "use RO-Crate" in an instruction was read past. Both failed
invisibly: DataCite is the default, and asking for the default looks exactly
like being obeyed.
"""

from __future__ import annotations

import json

import pytest
from datadirector_contracts import (
    Digest, ItemDecision, ModelCapability, ModelResponse, PolicyConfig,
    Residency, SensitivityClass,
)
from datadirector_contracts.payloads import CommitmentKind, DmpCommitment

from datadirector import schema_choice
from datadirector.policy.pep import PolicyEnforcementPoint


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


# -- the plan: a structured field, so no model ------------------------------

def test_a_plan_commitment_is_read_not_interpreted():
    commitments = [DmpCommitment(kind=CommitmentKind.METADATA_STANDARD,
                                 value="DataCite")]
    choice = schema_choice.from_plan(commitments)
    assert choice.standard == "DataCite"
    assert choice.source == "plan"
    assert choice.recognised


def test_a_plan_naming_something_unknown_is_not_silently_replaced():
    """Depositing in a standard nobody chose is harder to notice than a deposit
    that stopped and asked."""
    commitments = [DmpCommitment(kind=CommitmentKind.METADATA_STANDARD,
                                 value="Frobnicate 2.1")]
    choice = schema_choice.from_plan(commitments)
    assert choice.recognised is False
    item = schema_choice.unrecognised_item(choice)
    assert "cannot produce" in item.summary
    assert any("DataCite" in d for d in item.detail)


def test_a_plan_silent_on_the_standard_names_nothing():
    assert schema_choice.from_plan([]) is None


# -- the instruction: a directive -------------------------------------------

def test_an_instruction_is_carried_as_a_directive():
    """The same sentence inside a submitted file would carry no authority."""
    model = Scripted(json.dumps({"standard": "RO-Crate",
                                 "verbatim": "use RO-Crate"}))
    schema_choice.from_instruction(_pep(model), "Please use RO-Crate.")
    assert model.seen[0].trusted_instructions == "Please use RO-Crate."
    assert "RO-Crate" not in model.seen[0].user_content


def test_a_named_standard_is_recognised_however_it_is_spelled():
    for spelling in ("DataCite", "data-cite", "data cite", "DATACITE"):
        assert schema_choice.normalise(spelling) == "DataCite"
    for spelling in ("RO-Crate", "rocrate", "ro crate"):
        assert schema_choice.normalise(spelling) == "RO-Crate"


def test_nothing_named_means_the_default():
    model = Scripted(json.dumps({"standard": None}))
    choice, decision = schema_choice.from_instruction(_pep(model), "hurry up")
    assert choice is None
    assert any("default applies" in u for u in decision.undetermined)


# -- resolution -------------------------------------------------------------

def test_the_instruction_outranks_the_plan():
    instructed = schema_choice.SchemaChoice(standard="RO-Crate",
                                            source="instruction")
    committed = schema_choice.SchemaChoice(standard="DataCite", source="plan")
    assert schema_choice.resolve(instructed, committed).standard == "RO-Crate"


def test_the_plan_applies_when_nothing_was_instructed():
    committed = schema_choice.SchemaChoice(standard="RO-Crate", source="plan")
    assert schema_choice.resolve(None, committed).standard == "RO-Crate"


def test_silence_everywhere_is_the_default():
    resolved = schema_choice.resolve(None, None)
    assert resolved.standard == "DataCite"
    assert resolved.source == "default"


def test_an_unrecognised_name_does_not_win():
    """It is put to a person rather than applied, and the deposit proceeds on
    something this deployment can actually emit."""
    instructed = schema_choice.SchemaChoice(standard="Frobnicate",
                                            source="instruction",
                                            recognised=False)
    assert schema_choice.resolve(instructed, None).standard == "DataCite"


def test_disagreeing_with_the_plan_is_flagged_not_refused():
    item = schema_choice.divergence_item(
        schema_choice.SchemaChoice(standard="RO-Crate", source="instruction"),
        schema_choice.SchemaChoice(standard="DataCite", source="plan"))
    assert item is not None
    assert "not refused" in " ".join(item.detail)
    assert {d for d in item.permitted_decisions} == {ItemDecision.APPROVE,
                                                     ItemDecision.REJECT}


def test_agreeing_with_the_plan_raises_nothing():
    same = schema_choice.SchemaChoice(standard="DataCite", source="plan")
    assert schema_choice.divergence_item(same, same) is None


# -- it reaches the deposit --------------------------------------------------

def test_the_runtime_can_produce_every_standard_it_offers(runtime):
    """A name the choice recognises and the runtime cannot emit would be a
    promise nothing keeps."""
    for standard in set(schema_choice.KNOWN.values()):
        assert runtime.profile_for(standard) is not None
        assert runtime.profile_for(standard).schema_id


def test_choosing_ro_crate_changes_what_is_produced(runtime, tmp_path,
                                                    researcher):
    """The point of the whole exercise: the choice must change the output."""
    from datadirector_contracts import CanonicalRecord, Creator

    record = CanonicalRecord(title="T", creators=[Creator(name="A")],
                             publication_year=2026)
    datacite = runtime.profile_for("DataCite").project(record)
    crate = runtime.profile_for("RO-Crate").project(record)
    assert "creators" in datacite and "@graph" in crate
