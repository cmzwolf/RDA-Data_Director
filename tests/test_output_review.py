"""Nothing a model wrote may reach the next step before a person has read it.

The workflow used to treat a model's prose and a depositor's own words as the
same kind of fact. The validation agent checked an abstract for a missing year,
not for a year that was invented; the documentation agent filled the record's
Description from a README it had written seconds earlier. Each hop made a draft
look more settled, and `human_follows` — the field claiming that a person
confirmed an agent's output — was declared by nine agents and read by nothing,
which is the same defect as a conformance row naming a component that cannot be
reached.

These tests are about the difference between a draft somebody has validated and
a draft nobody has. The important one is the middle case: a person asked for
another attempt, and the workflow carried on as though that were an approval.
"""

from __future__ import annotations

import pytest
from datadirector_contracts import (
    Event, EventKind, GateItemKind, ItemDecision, ModelCapability, Orcid,
    SensitivityClass,
)
from datadirector_contracts.gate import counts_as_validation

from datadirector.agents.base import Capabilities, Invocation, LlmOutput, Outcome
from datadirector.errors import AuthorityError, ConfigurationError
from datadirector.gate import review as review_items
from datadirector.pipeline import Pipeline
from datadirector.state.projection import fold

JOB = "job-01JBQ7X9ABCDEFGHJKMNPQRSTV"
REVIEWER = Orcid(value="0000-0002-1825-0097")


class _Handle:
    # Just enough job handle for an agent that only needs its job id.

    def __init__(self, job_id):
        self.job_id = job_id


class _DraftingAgent:
    # A stand-in for an agent whose model writes prose. What matters is what it
    # declares, not what it writes: the orchestrator works from the declaration
    # on the capabilities, so an agent that forgets to hand a draft back is
    # still held at the gate.

    def __init__(self, name="metadata", claim=None, events=None):
        self.name = name
        self.version = "0.1.0"
        self.claim = claim
        self.events = events

    @property
    def identity(self):
        return "{}/{}".format(self.name, self.version)

    def capabilities(self):
        return Capabilities(name=self.name, summary="writes a draft",
                             needs_backend=ModelCapability.TEXT_GENERATION,
                             llm_output=self.claim,
                             sensitivity_ceiling=SensitivityClass.SENSITIVE)

    def run(self, invocation):
        events = self.events if self.events is not None else [
            Event(sequence=1, job_id=invocation.job.job_id,
                   kind=EventKind.METADATA_DRAFTED, agent=self.identity,
                   payload={"title": "Sediment cores from the Tris basin"})]
        return Outcome(job_id=invocation.job.job_id, events=events)


def _item(title="Invented abstract", agent="metadata"):
    return review_items.review_item("{}/0.1.0".format(agent), "the record",
                                     [{"title": title}])


def test_an_unvalidated_draft_is_named_by_what_the_model_wrote():
    # Content-addressed, so approving one draft cannot silence the next.
    first = _item("Sediment cores")
    second = _item("Sediment cores, revised")
    assert first.item_id != second.item_id
    assert first.item_id.startswith("llm-output:metadata:")
    assert first.permitted_decisions == list(review_items.REVIEW_DECISIONS)


def test_asking_for_another_attempt_is_not_a_validation():
    # The defect this exists to remove: every recorded decision used to count
    # as a validation, so a reviewer who pressed "try again" had the rejected
    # draft flowing downstream while they waited for the second one.
    assert counts_as_validation(ItemDecision.APPROVE)
    assert counts_as_validation(ItemDecision.EDITED)
    assert not counts_as_validation(ItemDecision.REQUEST_RERUN)


def test_a_requested_rerun_leaves_the_draft_waiting(runtime):
    # Read from the log, so a restart cannot forget the objection.
    pipeline = Pipeline(runtime)
    item = _item()
    pipeline.record_gate_items(JOB, [item], agent="metadata/0.1.0")
    assert [i.item_id for i in pipeline.pending_reviews(JOB)] == [item.item_id]

    pipeline.resolve_review(JOB, item.item_id, ItemDecision.REQUEST_RERUN,
                             human=REVIEWER,
                             reason="the abstract names the village")
    assert [i.item_id for i in pipeline.pending_reviews(JOB)] == [item.item_id]

    pipeline.resolve_review(JOB, item.item_id, ItemDecision.APPROVE,
                             human=REVIEWER)
    assert pipeline.pending_reviews(JOB) == []


def test_a_review_decision_the_item_does_not_offer_is_refused(runtime):
     # The command line cannot approve what the browser would refuse, because
     # both go through the same `Gate.resolve`.
    pipeline = Pipeline(runtime)
    item = _item()
    pipeline.record_gate_items(JOB, [item], agent="metadata/0.1.0")
    with pytest.raises(AuthorityError):
        pipeline.resolve_review(JOB, item.item_id, ItemDecision.REJECT,
                                 human=REVIEWER, reason="no")


def test_a_rerun_without_words_about_what_is_wrong_is_refused(runtime):
     # A retry with no note is the same procedure run again, and the reviewer
     # would be waiting for a correction nobody asked for.
    pipeline = Pipeline(runtime)
    item = _item()
    pipeline.record_gate_items(JOB, [item], agent="metadata/0.1.0")
    with pytest.raises(AuthorityError, match="saying what was wrong"):
        pipeline.request_rerun(JOB, "metadata", human=REVIEWER, note="     ")


def test_an_agent_that_never_describes_its_own_prose_is_refused():
     # A claim that cannot be checked is not a claim. The ordinary version of
     # this failure is a declaration that drifted from the code, which is what
     # `human_follows` became: nine agents said a person followed their output,
     # and nothing enforced it.
    agent = _DraftingAgent(claim=LlmOutput("the record", produces=()))
    outcome = agent.run(Invocation(job=_Handle(JOB), instruction=None))
    with pytest.raises(ConfigurationError, match="cannot be checked"):
        review_items.claimed_review(agent, JOB, outcome)


def test_prose_the_depositor_wrote_is_not_asked_back_for_approval():
     # Asking a person to validate their own words yields a click that means
     # nothing, and teaches the reviewer to click without reading.
    own = [Event(sequence=1, job_id=JOB, kind=EventKind.METADATA_DRAFTED,
                   agent="metadata/0.1.0", human=REVIEWER,
                   payload={"description": "written by the depositor"})]
    agent = _DraftingAgent(
         claim=LlmOutput("the record", produces=(EventKind.METADATA_DRAFTED,)),
         events=own)
    outcome = agent.run(Invocation(job=_Handle(JOB), instruction=None))
    assert review_items.claimed_review(agent, JOB, outcome) is None


def test_the_draft_is_held_whatever_the_agent_chose_to_return():
     # The content comes from the log, not from the agent's description of it.
     # An agent that reads its own prose back to the orchestrator decides what
     # a reviewer sees; reading it off the appended events means the digest
     # names what actually landed.
    agent = _DraftingAgent(
         claim=LlmOutput("the record", produces=(EventKind.METADATA_DRAFTED,)))
    outcome = agent.run(Invocation(job=_Handle(JOB), instruction=None))
    item = review_items.claimed_review(agent, JOB, outcome)
    assert item.kind is GateItemKind.LLM_OUTPUT
    expected = review_items.item_id_for(
         "metadata", review_items.digest_of([outcome.events[0].payload]))
    assert item.item_id == expected



def test_a_model_draft_upstream_stops_everything_behind_it(runtime):
      # The block is a property of the edges. A reviewer who objects to the
      # abstract must stop validation, documentation and the deposit, because
      # each of them reads what the metadata agent wrote. The names come from
      # the graph, so an agent added later falls under the same rule without
      # anyone remembering to update a list.
    pipeline = Pipeline(runtime)
    for event in [
         Event(sequence=1, job_id=JOB, kind=EventKind.WORKFLOW_CREATED,
                 agent="pipeline/0.1.0", payload={"config_digest": "sha256:x"}),
         Event(sequence=1, job_id=JOB, kind=EventKind.MATERIAL_REGISTERED,
                 agent="ingestion/0.1.0", payload={"artefacts": ["a.csv"]}),
         Event(sequence=1, job_id=JOB, kind=EventKind.METADATA_DRAFTED,
                 agent="metadata/0.1.0", payload={"title": "Draft"})]:
        pipeline.runtime.store.append(event)
    item = _item("Draft")
    pipeline.record_gate_items(JOB, [item], agent="metadata/0.1.0")

    step = next(s for s in pipeline.engine.steps if s.name == "validation")
    assert step._held_by_upstream(JOB, "validation") == {"metadata"}

    pipeline.resolve_review(JOB, item.item_id, ItemDecision.APPROVE,
                             human=REVIEWER)
    assert step._held_by_upstream(JOB, "validation") == set()


def test_a_review_note_does_not_become_the_depositor_instruction(runtime):
      # One is quoted verbatim and is nobody's editing target. The reviewer's
      # complaint reaches the model as feedback on a draft; the instruction
      # stays what the depositor said at the start. Merging them would let a
      # review comment silently redirect the job — the same class of failure as
      # an instruction found inside a submitted file.
    pipeline = Pipeline(runtime)
    pipeline.runtime.store.append(Event(
        sequence=1, job_id=JOB, kind=EventKind.INSTRUCTIONS_RECEIVED,
        agent="pipeline/0.1.0", human=REVIEWER,
        payload={"instruction": "deposit this in Zenodo",
                    "channel": "authenticated-depositor"}))
    pipeline.runtime.store.append(review_items.review_note_event(
        JOB, "metadata", "the abstract names the village", REVIEWER,
        "metadata/0.1.0"))

    state = fold(pipeline.runtime.store.load(JOB))
    step = next(s for s in pipeline.engine.steps if s.name == "metadata")
    assert step._instruction(state).text == "deposit this in Zenodo"
    assert review_items.review_note(
         pipeline.runtime.store.load(JOB)) == "the abstract names the village"


def test_the_agent_that_writes_prose_declares_it():
      # The declaration is what the orchestrator enforces, so an agent that asks
      # a model for prose and declares nothing would slip the gate entirely,
      # and nothing outside this test would notice.
    from datadirector.agents import (classification, documentation, dmp, media,
                                      metadata)

    for agent, kind in [(metadata.MetadataAgent, EventKind.METADATA_DRAFTED),
                         (documentation.DocumentationAgent,
                          EventKind.DOCUMENTATION_DRAFTED),
                         (classification.ClassificationAgent,
                          EventKind.CLASSIFICATION_COMPLETED),
                         (media.MediaAgent, EventKind.CLASSIFICATION_COMPLETED),
                         (dmp.DmpAgent, EventKind.DMP_COMMITMENTS_READ)]:
        claim = agent.capabilities().llm_output
        assert claim is not None, f"{agent.__name__} declares no model output"
        assert kind in claim.produces, (
            f"{agent.__name__} does not name the event carrying its prose")


def test_an_output_already_reviewed_item_by_item_declares_nothing():
      # A second, coarser approval is not more review. Redaction proposals and
      # proposed claims each reach a person on their own item; a coarse item
      # over individually-reviewed ones would add a click, not a reader.
    from datadirector.agents.declaration import DeclarationAgent
    from datadirector.agents.redaction import RedactionAgent

    for agent in (RedactionAgent, DeclarationAgent):
        assert agent.capabilities().llm_output is None

