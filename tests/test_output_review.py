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
    Event, EventKind, GateItem, GateItemKind, ItemDecision, ModelCapability,
    Orcid, SensitivityClass,
)
from datadirector_contracts.gate import counts_as_validation

from datadirector.agents.base import Capabilities, Invocation, LlmOutput, Outcome
from datadirector.errors import AuthorityError, ConfigurationError
from datadirector.gate import review as review_items
from datadirector.job_handle import target_repository
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

def test_an_output_nobody_may_rewrite_is_not_offered_the_edit_button():
    """The button and the prose used to contradict each other.

    `editable=False` only added an explanatory line: the item still offered
    `edited`, and said in the same breath that a person may not write their own
    level. That is not cosmetic. `counts_as_validation` is true of `EDITED`, so
    the reviewer who pressed it on a classification item attested to a version
    they never wrote and released validation, documentation and the deposit
    behind it. A control whose effect cannot be honoured is the one kind of
    control a gate may not offer.
    """
    item = review_items.review_item(
        "classification/0.1.0", "the sensitivity view and the reasons for it",
        [{"level": "sensitive"}], editable=False,
        edit_note="a person may not write their own level here")
    assert ItemDecision.EDITED not in item.permitted_decisions
    assert ItemDecision.APPROVE in item.permitted_decisions
    assert ItemDecision.REQUEST_RERUN in item.permitted_decisions
    # And the evidence stops pointing at an edit that cannot be made.
    assert not any("edit it" in line for line in item.detail)
    assert item.detail[-1].startswith("a person may not write their own level")


def test_an_editable_draft_still_offers_all_three_answers():
    # The other half of the rule: dropping the button follows from the agent
    # declaring its output uneditable, and is not a new house style.
    assert _item().permitted_decisions == list(review_items.REVIEW_DECISIONS)


def test_reading_a_model_draft_cannot_stand_in_for_validating_it():
    """Having read something is not a judgement about it.

    The screen only renders what `permitted_decisions` holds, so this is not
    reachable by clicking. It is reachable from an item whose declaration
    drifted, or a form built by hand -- and a draft settled by "I have read
    this" would release validation, documentation and the deposit behind it
    on nobody having judged the draft at all.
    """
    item = GateItem(item_id="llm-output:metadata:0123456789ab",
                    kind=GateItemKind.LLM_OUTPUT,
                    summary="Check what metadata wrote: the record",
                    detail=["written by metadata/0.1.0"],
                    permitted_decisions=[ItemDecision.APPROVE,
                                         ItemDecision.ACKNOWLEDGE])
    gate = review_items.Gate()
    gate.add([item])
    with pytest.raises(AuthorityError, match="holds what a model wrote"):
        gate.resolve(item.item_id, ItemDecision.ACKNOWLEDGE, human=REVIEWER)
    assert [i.item_id for i in gate.unresolved()] == [item.item_id]


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


# -- two defects the real logs recorded ------------------------------------
#
# Both reached a live job and were read back off its event log, so they are
# reproduced here from the same fold the running system uses rather than from a
# fresh model of it.


def _draft(pipeline, agent, title):
    """Put one model draft on the log as a review item, and return the item."""
    item = review_items.review_item("{}/0.1.0".format(agent), "the record",
                                    [{"title": title}])
    pipeline.record_gate_items(JOB, [item], agent="{}/0.1.0".format(agent))
    return item


def test_an_item_written_twice_still_asks_once(runtime):
    # The log is append-only and one item had two writers: the orchestrator
    # raised it and a route appended a second copy under its own agent. The
    # researcher saw the same question twice, and a gate that repeats itself is
    # a gate that gets clicked through. The fold collapses them, so the item --
    # and the single decision it asks for -- appears once.
    pipeline = Pipeline(runtime)
    item = _draft(pipeline, "metadata", "Sediment cores from the Tris basin")
    raw = item.model_dump(mode="json")
    pipeline.runtime.store.append(Event(
        sequence=1, job_id=JOB, kind=EventKind.REDACTION_PROPOSED,
        agent="web/0.1.0",
        payload={"marker": "gate.items-added", "items": [raw]}))

    folded = review_items.log_items(pipeline.runtime.store.load(JOB))
    assert [i.item_id for i in folded].count(item.item_id) == 1, (
        "an item written by two agents was folded as two items")
    assert [i.item_id for i in pipeline.pending_reviews(JOB)] == [item.item_id]


def test_a_later_validated_draft_releases_the_one_it_replaced(runtime):
    # The defect a live job was stuck on. A reviewer asked the agent to write
    # again; that left the first draft open, correctly, while the second was
    # produced. They then edited a *third* draft and validated it, and nothing
    # retired the never-validated first one. It held the workflow and the
    # deposit forever, for prose nobody would ever publish -- the latest draft
    # is the only one the deposit reads. Asking again still holds the draft
    # being replaced; it is the replacement, once validated, that frees it.
    pipeline = Pipeline(runtime)
    first = _draft(pipeline, "classification", "first abstract")
    pipeline.resolve_review(JOB, first.item_id, ItemDecision.REQUEST_RERUN,
                            human=REVIEWER, reason="the year is invented")
    _draft(pipeline, "classification", "second abstract")
    third = _draft(pipeline, "classification", "third abstract")
    pipeline.resolve_review(JOB, third.item_id, ItemDecision.EDITED,
                            human=REVIEWER)

    assert pipeline.pending_reviews(JOB) == [], (
        "a validated later draft must retire the draft it superseded")


def test_an_unvalidated_latest_draft_still_holds_its_predecessors(runtime):
    # The boundary of the rule above, so the fix cannot be read as "several
    # drafts, keep the newest": supersession only reaches back from a draft
    # somebody has actually stood behind. Until the newest draft is validated,
    # every earlier unvalidated one is still owed a reading, and the workflow
    # stays stopped exactly as the rerun rule requires.
    pipeline = Pipeline(runtime)
    first = _draft(pipeline, "classification", "first abstract")
    pipeline.resolve_review(JOB, first.item_id, ItemDecision.REQUEST_RERUN,
                            human=REVIEWER, reason="the year is invented")
    second = _draft(pipeline, "classification", "second abstract")
    third = _draft(pipeline, "classification", "third abstract")

    # Nothing validated yet: the first held by its rerun request, the second
    # and third by never having been read at all.
    assert [i.item_id for i in pipeline.pending_reviews(JOB)] == [
        first.item_id, second.item_id, third.item_id]

    pipeline.resolve_review(JOB, second.item_id, ItemDecision.APPROVE,
                            human=REVIEWER)
    # The second validates, but the third -- later still -- is unvalidated, so
    # the second is the newest validated draft and only what precedes it is
    # released. The third still holds; the first, before the second, goes.
    assert [i.item_id for i in pipeline.pending_reviews(JOB)] == [third.item_id]



# ==========================================================================
# Where the job is going, read off the log
#
# The metadata agent populates the publisher from a registry of the second
# kind, and it can only do that for a repository the log already names. The
# item id is spelled out here rather than imported from repository_choice on
# purpose: a prefix that drifts in one of the two places should fail a test,
# not quietly read as "nobody named a repository".
# ==========================================================================

CREATED = Event(sequence=1, job_id=JOB, kind=EventKind.WORKFLOW_CREATED,
                 agent="pipeline/0.1.0", payload={})


def _added(item):
    """The entry the orchestrator appends when an agent raises an item."""
    return Event(sequence=1, job_id=JOB, kind=EventKind.REDACTION_PROPOSED,
                 agent="pipeline/0.1.0",
                 payload={"marker": "gate.items-added",
                             "items": [item.model_dump(mode="json")]})


def _preference_item(name="Zenodo"):
    return GateItem(item_id="repository-preference:" + name,
                    kind=GateItemKind.DMP_DISCREPANCY,
                    summary="Deposit to " + name + "?", detail=[],
                    permitted_decisions=[ItemDecision.APPROVE])


def test_the_repository_named_on_the_log_is_the_one_read_off_it():
    """The preference the gate asked the depositor to confirm is where the
    job is headed for the metadata agent, because that is the only thing the
    log says so far. The repository node has not run yet."""
    assert target_repository([CREATED, _added(_preference_item())]) == {
        "name": "Zenodo", "identifier": None, "source": "preference"}


def test_a_confirmed_selection_outranks_the_preference_it_came_from():
    """Both names are on the log. The selection is the one a person made at
    the repository node, so it is the destination; reading the preference
    instead would populate the publisher from a repository the depositor
    walked away from."""
    events = [_added(_preference_item("Zenodo")),
              Event(sequence=1, job_id=JOB, kind=EventKind.REPOSITORY_SELECTED,
                    agent="repository/0.1.0", human=REVIEWER,
                    payload={"repository": "Dryad",
                             "identifier": "10.5063/FICZ6A"})]
    assert target_repository(events) == {"name": "Dryad",
                                         "identifier": "10.5063/FICZ6A",
                                         "source": "selected"}


def test_nothing_named_is_reported_as_nothing_named():
    """Not "no repository exists", not "the registry has none": the log says
    nothing, which is a different finding and calls for a different next act."""
    assert target_repository([CREATED]) is None


def test_another_kind_of_item_is_not_read_as_a_repository():
    """The fold looks for one item id and no other. An LLM-output item is a
    draft a person has to read, not a destination, and reading one as a
    destination would name a model's output as where the data is going."""
    item = GateItem(item_id="llm-output:classification:d61e2871445a",
                    kind=GateItemKind.LLM_OUTPUT, summary="the abstract",
                    detail=[], permitted_decisions=[ItemDecision.APPROVE])
    assert target_repository([_added(item)]) is None

