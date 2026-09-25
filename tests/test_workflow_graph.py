"""The workflow as a graph: what it guarantees that a step list could not.

Four agents were constructed and called from nowhere, and the flat ordered list
had no way to express the question. In a graph an unwired agent is an orphan
node, which is a property of the data rather than something a person must
notice.
"""

from __future__ import annotations

import pytest
from datadirector_contracts import EventKind, SensitivityClass

from datadirector.workflow.definition import build
from datadirector.workflow.graph import (
    ALWAYS, Condition, Edge, Node, WorkflowGraph,
)


@pytest.fixture
def graph():
    return build({})


class _State:
    def __init__(self, **kwargs):
        self.step = kwargs.get("step", "ingestion")
        self.material = kwargs.get("material", [])
        self.classification = kwargs.get("classification")
        self.terminal = kwargs.get("terminal")
        self.job_id = "job-" + "A" * 26


# -- the guarantee ---------------------------------------------------------

def test_the_workflow_has_no_orphans(graph):
    """The failure this graph exists to make visible."""
    assert graph.orphans() == [], (
        f"nodes nothing leads to: {graph.orphans()}")


def test_every_agent_the_runtime_builds_is_in_the_graph(runtime):
    """An agent constructed and never wired is work that will never run,
    however well it is tested."""
    in_graph = set(build({}).nodes)
    # Node names are the workflow's vocabulary, not the runtime's, so the two
    # are related by an explicit map rather than by matching strings.
    for agent, node in [("ingestion", "ingest"), ("declaration", "declaration"),
                        ("media", "media"), ("redaction", "redaction"),
                        ("metadata", "metadata"),
                        ("documentation", "documentation"),
                        ("validation", "validation"), ("dmp", "dmp"),
                        ("repository", "repository"),
                        ("publication", "deposit")]:
        assert agent in runtime.agents, f"{agent} is not built"
        assert node in in_graph, f"{agent} is built but has no node"


def test_an_orphan_is_detected():
    """Proved by building one, so the check is not decoration."""
    orphaned = WorkflowGraph(
        [Node("a", "A"), Node("stranded", "Stranded", serves=("R7",))],
        [Edge(WorkflowGraph.ENTRY, "a")])
    assert orphaned.orphans() == ["stranded"]


def test_a_requirement_claimed_only_by_an_orphan_is_not_served():
    """The deduction that replaces a declaration: a class saying it serves R7
    proves nothing if nothing reaches it."""
    orphaned = WorkflowGraph(
        [Node("a", "A"), Node("stranded", "Stranded", serves=("R7",))],
        [Edge(WorkflowGraph.ENTRY, "a")])
    assert "R7" not in orphaned.serves()
    assert orphaned.claimed_but_unreachable() == {"R7": ["stranded"]}


def test_an_edge_to_a_node_that_does_not_exist_is_refused():
    """A typo would otherwise become a silently unreachable branch — the very
    failure this file removes, reintroduced through the mechanism meant to
    prevent it."""
    with pytest.raises(ValueError, match="not a node"):
        WorkflowGraph([Node("a", "A")],
                      [Edge(WorkflowGraph.ENTRY, "a"), Edge("a", "typo")])


# -- conditions ------------------------------------------------------------

def test_nothing_reads_the_data_before_the_depositor_has_spoken(graph):
    """The section 9.3 ordering, as an edge rather than a convention someone
    must remember."""
    edge = next(e for e in graph.edges
                if e.source == "confirm_declaration"
                and e.target == "classification")
    assert edge.when(_State()) is False
    from datadirector_contracts import Classification
    confirmed = _State(classification=Classification(
        level=SensitivityClass.INTERNAL))
    assert edge.when(confirmed) is True


def test_media_inspection_is_conditional_on_there_being_media(graph):
    edge = next(e for e in graph.edges if e.target == "media")
    assert edge.when(_State(material=["a.csv"])) is False
    assert edge.when(_State(material=["a.csv", "plate.png"])) is True


def test_redaction_is_proposed_only_for_sensitive_material(graph):
    from datadirector_contracts import Classification

    edge = next(e for e in graph.edges if e.target == "redaction")
    public = _State(classification=Classification(
        level=SensitivityClass.PUBLIC))
    sensitive = _State(classification=Classification(
        level=SensitivityClass.SENSITIVE))
    assert edge.when(public) is False
    assert edge.when(sensitive) is True


def test_every_condition_can_be_read_as_a_sentence(graph):
    """An edge whose condition is only code cannot be drawn, explained to a
    researcher, or reviewed by anyone who does not read Python."""
    for edge in graph.edges:
        assert edge.when.description
        assert edge.when.description != "always" or edge.when is ALWAYS


# -- human steps -----------------------------------------------------------

def test_steps_needing_a_person_are_never_automatic(graph):
    for name in ("confirm_declaration", "review", "deposit"):
        assert graph.nodes[name].human
        assert not graph.nodes[name].automatic


def test_the_engine_is_offered_only_automatic_nodes(runtime):
    from datadirector.pipeline import Pipeline

    steps = {s.name for s in Pipeline(runtime).engine.steps}
    assert not steps & {"confirm_declaration", "review", "deposit"}
    assert {"classification", "metadata", "documentation", "validation"} <= steps


# -- the drawing -----------------------------------------------------------

def test_the_graph_can_be_drawn(graph):
    """Rendered rather than drawn by hand, so a picture in a paper cannot
    describe a workflow the software does not have."""
    diagram = build(None).to_mermaid()
    assert diagram.startswith("flowchart TD")
    assert "confirm_declaration" in diagram
    assert "the submission contains images or audio" in diagram


def test_a_job_never_reports_that_it_is_waiting_for_nothing(runtime, tmp_path):
    """A status worse than wrong: a researcher reads "waiting for nothing" as
    "done".

    `advance` compared the job's step name against a hand-written list, and a
    job sitting at a step absent from that list reported nothing awaited while
    the graph knew perfectly well that a declaration was outstanding.
    """
    import zipfile

    from datadirector_contracts import Orcid

    from datadirector.pipeline import Pipeline

    archive = tmp_path / "s.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.csv", "x\n1\n")

    pipeline = Pipeline(runtime)
    result = pipeline.ingest(archive, human=Orcid(value="0009-0000-0000-0017"))
    advanced = pipeline.advance(result.job_id)

    assert advanced.awaiting, "a job needing a declaration awaits nothing"
    assert advanced.blocking_node == "confirm_declaration"


def test_what_is_awaited_is_named_as_a_node_not_only_as_prose(runtime,
                                                              tmp_path):
    """So a caller can link to the right screen rather than parsing a sentence."""
    import zipfile

    from datadirector_contracts import Orcid

    from datadirector.pipeline import Pipeline

    archive = tmp_path / "s.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.csv", "x\n1\n")

    pipeline = Pipeline(runtime)
    result = pipeline.ingest(archive, human=Orcid(value="0009-0000-0000-0017"))
    advanced = pipeline.advance(result.job_id)
    assert advanced.blocking_node in pipeline.graph.nodes


def test_every_runner_can_actually_be_called(runtime, tmp_path):
    """The suite stayed green through `classify() takes 2 positional arguments
    but 3 were given`, because no test ever called a graph runner.

    Checked by signature rather than by running them: running needs a model.
    Still enough to catch an argument count, which is what broke.
    """
    import inspect
    import zipfile

    from datadirector_contracts import Orcid

    from datadirector.pipeline import Pipeline

    archive = tmp_path / "s.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.csv", "x\n1\n")

    pipeline = Pipeline(runtime)
    result = pipeline.ingest(archive, human=Orcid(value="0009-0000-0000-0017"))
    state = runtime.store.load(result.job_id)

    from datadirector.state.projection import fold
    folded = fold(state)

    step_by_name = {step.name: step for step in pipeline.engine.steps}
    for name, node in pipeline.graph.nodes.items():
        if node.human:
            continue
        step = step_by_name.get(name)
        assert step is not None, (
            f"the automatic node {name!r} has no step for the engine to run")
        signature = inspect.signature(step.run)
        required = [p for p in signature.parameters.values()
                    if p.default is inspect.Parameter.empty
                    and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        assert len(required) == 1, (
            f"the runner for {name!r} takes {len(required)} required "
               "arguments; the engine passes exactly one (the job state)")
        try:
            signature.bind(folded)
        except TypeError as exc:
            pytest.fail(f"the runner for {name!r} cannot be called with a "
                        f"job state: {exc}")


def test_a_review_is_not_done_while_items_are_outstanding(tmp_path):
    """The phase turned green at the moment work appeared: raising a gate item
    produced a REDACTION_PROPOSED event, which the review phase counted as
    evidence of its own completion."""
    from datadirector_contracts import Event, EventKind
    from datadirector.state.projection import fold
    from datadirector.state.store import EventStore
    from datadirector.web.progress import PhaseState, phases_for

    store = EventStore(tmp_path / "state")
    job_id = "job-" + "V" * 26
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.WORKFLOW_CREATED, agent="t/1.0"))
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.REDACTION_PROPOSED, agent="t/1.0",
                       payload={"marker": "gate.items-added", "items": []}))

    events = store.load(job_id)
    review = next(p for p in phases_for(events, fold(events), unresolved=1)
                  if p.key == "review")
    assert review.state is PhaseState.WAITING_ON_YOU
    assert "still to decide" in (review.detail or "")


def test_a_step_that_emits_nothing_does_not_stop_the_workflow(tmp_path):
    """Two bugs met here, and between them metadata, documentation and
    validation never ran in a workflow whose every component was wired and
    tested.

    The engine returned as soon as a step left the state unchanged, and steps
    legitimately do: media emits no event when there are no images, redaction
    emits none at all because its output is gate items. And completion was
    inferred from the events a step produces, so a node producing none could
    never be marked done and the traversal returned it forever.
    """
    from datadirector_contracts import DecisionRecord, Event, EventKind
    from datadirector.provenance.recorder import Recorder
    from datadirector.state.store import EventStore
    from datadirector.workflow.engine import WorkflowEngine

    store = EventStore(tmp_path / "state")
    job_id = "job-" + "S" * 26
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.WORKFLOW_CREATED, agent="t/1.0"))
    order = []

    class Silent:
        """Runs, changes nothing. Entirely legitimate."""

        name = "silent"

        def runnable(self, state):
            return "silent" not in order

        def run(self, state):
            order.append("silent")
            return [], DecisionRecord(agent="t/1.0", step="silent",
                                      selection_basis="nothing to do")

    class Later:
        name = "later"

        def runnable(self, state):
            return "silent" in order and "later" not in order

        def run(self, state):
            order.append("later")
            return ([Event(sequence=1, job_id=job_id,
                           kind=EventKind.VALIDATION_COMPLETED,
                           agent="t/1.0")],
                    DecisionRecord(agent="t/1.0", step="later",
                                   selection_basis="ran"))

    engine = WorkflowEngine(store, Recorder(store, tmp_path / "prov"),
                            [Silent(), Later()], sleep=lambda _: None)
    engine.run_until_blocked(job_id)

    assert order == ["silent", "later"], (
        "a step that emitted nothing stopped everything after it")


def test_running_a_step_is_recorded(tmp_path):
    """So completion survives a restart and does not depend on what a step
    happened to emit."""
    from datadirector_contracts import DecisionRecord, Event, EventKind
    from datadirector.provenance.recorder import Recorder
    from datadirector.state.store import EventStore
    from datadirector.workflow.engine import WorkflowEngine

    store = EventStore(tmp_path / "state")
    job_id = "job-" + "T" * 26
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.WORKFLOW_CREATED, agent="t/1.0"))
    done = []

    class Once:
        name = "once"

        def runnable(self, state):
            return not done

        def run(self, state):
            done.append(1)
            return [], DecisionRecord(agent="t/1.0", step="once",
                                      selection_basis="ran")

    engine = WorkflowEngine(store, Recorder(store, tmp_path / "prov"),
                            [Once()], sleep=lambda _: None)
    engine.advance(job_id)

    recorded = [e for e in store.load(job_id)
                if e.kind is EventKind.STEP_COMPLETED
                and e.payload.get("step") == "once"]
    assert recorded, "the engine did not record that the step ran"


def test_the_whole_automatic_chain_runs(runtime, tmp_path, monkeypatch):
    """End to end with a stubbed model: everything between the declaration and
    the review should run without a person."""
    import json
    import zipfile

    from datadirector_contracts import (
        Digest, Event, EventKind, ItemDecision, ModelCapability, ModelResponse, Orcid,
        PolicyConfig, Residency, SensitivityClass,
    )

    from datadirector.pipeline import Pipeline
    from datadirector.policy.pep import PolicyEnforcementPoint

    class Stub:
        name = "stub"

        def residency(self):
            return Residency.ON_PREMISE

        def capabilities(self):
            return {ModelCapability.TEXT_GENERATION}

        def complete(self, request):
            return ModelResponse(
                text=json.dumps({
                    "sensitivity": "sensitive", "indicators": [],
                    "keywords": ["air temperature"], "title": "T",
                    "abstract": "A", "resource_type": "Dataset",
                    "language": "en", "uncertain": [],
                    "readme_sections": [{"heading": "O", "body": "b"}],
                    "variables": [], "gaps": [], "proposals": [], "claims": []}),
                model_id="stub", input_digest=Digest.of_bytes(b""))

    runtime.backends = {"stub": Stub()}
    runtime.pep = PolicyEnforcementPoint(
        PolicyConfig(backend_by_sensitivity={
            level: ["stub"] for level in SensitivityClass}), runtime.backends)
    # No vocabulary and no registry: both reach the internet, which unit tests
    # do not. Their absence is reported rather than fatal — keywords come back
    # ungrounded and no shortlist is offered — which is exactly what a
    # deployment without them should do.
    runtime.vocabulary = None
    runtime.registry = None
    runtime.agents = runtime._build_agents()

    archive = tmp_path / "s.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.csv", "id,name\n1,M\n")

    pipeline = Pipeline(runtime)
    result = pipeline.ingest(archive, human=Orcid(value="0009-0000-0000-0017"))
    runtime.store.append(Event(
        sequence=1, job_id=result.job_id, kind=EventKind.DECLARATION_CONFIRMED,
        agent="t/1.0", human=Orcid(value="0009-0000-0000-0017"),
        payload={"sensitivity": 2}))

    pipeline.advance(result.job_id)

       # Everything between the declaration and the review runs without a
       # person — provided a person has read what the model wrote first. The
       # loop is the workflow as it now works: an agent whose model wrote prose
       # is held until a named person validates it, so the chain is walked by
       # alternating between running and reading. Had this been left asserting
       # the old behaviour, the review gate would have had to be weakened to
       # keep the test green.
    for _ in range(12):
        pending = pipeline.pending_reviews(result.job_id)
        if not pending:
            break
        for item in pending:
            pipeline.resolve_review(result.job_id, item.item_id,
                                     ItemDecision.APPROVE, human=Orcid(value="0009-0000-0000-0017"))
        pipeline.advance(result.job_id)

    completed = pipeline.completed(result.job_id)
    for node in ("classification", "metadata", "documentation", "validation"):
        assert node in completed, f"{node} never ran"
