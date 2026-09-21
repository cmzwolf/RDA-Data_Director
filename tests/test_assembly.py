"""Are the parts actually assembled into a system?

An audit found every agent, and most plugins, unreachable from any entry point:
written, tested, documented and never wired in. `ingest` ran the ingestion agent
and stopped.

The failure is invisible from inside a cluster. Each one ended with its
components complete and its tests green, and nothing in that process asks
whether the components have been *connected*. A conformance row can name a
component that exists and cannot be reached, and be wrong in a way no unit test
catches — a subtler version of the R8 failure in Appendix B.5.

These tests ask the question directly.
"""

from __future__ import annotations

import ast
import json
import tempfile
import zipfile
from pathlib import Path

import pytest
from datadirector_contracts import Orcid

from datadirector.config.loader import load_policy, load_wiring, resolve
from datadirector.pipeline import Pipeline
from datadirector.plugins.discovery import PluginRegistry
from datadirector.runtime import Runtime

SRC = Path(__file__).parent.parent / "packages/datadirector/src/datadirector"
ROOT = Path(__file__).parent.parent

# Entry points a person or another system can actually reach.
ENTRY_POINTS = {"cli.py", "runtime.py", "pipeline.py", "api/app.py",
                "api/service.py", "web/views.py"}

# Modules that are legitimately not reachable from an entry point, each with the
# reason. A module may only be here because something about it makes reachability
# the wrong question — not because wiring it is still on a list somewhere.
EXEMPT = {
    "backends/recording.py": "test doubles: replay and recording backends",
    "backends/anthropic.py": "constructed by name from configuration",
    "conformance/report.py": "reached by the CLI conformance command",
}


def _imports(path: Path, package: str) -> set[str]:
    """Targets this module imports, as paths relative to the source root.

    A relative import is resolved against the importing module's own package:
    `from .models import X` inside `api/app.py` means `api/models`, not
    `models`. Getting this wrong made a reachable module look like an orphan,
    which is the same class of error as the thing being detected.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module is None and node.level:
            # `from . import sibling`. The module is None and the target is in
            # the alias list, so a checker keyed on `node.module` misses it
            # entirely — and then reports a module that is plainly imported as
            # unreachable.
            prefix = package
            for alias in node.names:
                out.add(f"{prefix}/{alias.name}" if prefix else alias.name)
            continue
        if not node.module:
            continue
        if node.level:
            base = package.split("/")
            climbed = base[:len(base) - (node.level - 1)] if node.level > 1 \
                else base
            prefix = "/".join(p for p in climbed if p)
            target = node.module.replace(".", "/")
            out.add(f"{prefix}/{target}" if prefix else target)
        elif node.module.startswith("datadirector."):
            out.add(node.module.split("datadirector.", 1)[1].replace(".", "/"))
    return out


def _reachable() -> tuple[set[str], set[str]]:
    modules = {str(p.relative_to(SRC)): p for p in SRC.rglob("*.py")
               if "__pycache__" not in str(p)}
    edges = {rel: _imports(path, str(Path(rel).parent) if Path(rel).parent
                           != Path(".") else "")
             for rel, path in modules.items()}

    def resolve_target(target: str) -> str | None:
        for candidate in (f"{target}.py", f"{target}/__init__.py"):
            if candidate in modules:
                return candidate
        for rel in modules:
            if rel.endswith(f"{target}.py"):
                return rel
        return None

    seen, stack = set(), [e for e in ENTRY_POINTS if e in modules]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for target in edges.get(current, ()):
            found = resolve_target(target)
            if found and found not in seen:
                stack.append(found)
    return set(modules), seen


def test_no_component_is_unreachable_from_an_entry_point():
    """The check that would have caught ten unwired agents.

    A component nobody can reach is not a capability, whatever the conformance
    matrix says about it.
    """
    modules, reachable = _reachable()
    orphans = sorted(
        m for m in modules - reachable
        if not m.endswith("__init__.py") and m not in EXEMPT)
    assert not orphans, (
        "unreachable from any entry point:\n  " + "\n  ".join(orphans)
        + "\n\nEither wire them in, or add them to EXEMPT with the reason.")


def test_every_exemption_names_a_reason():
    """An exemption list without reasons becomes a place to hide things."""
    assert all(reason.strip() for reason in EXEMPT.values())


def test_every_agent_is_built_by_the_runtime(runtime):
    """An agent the composition root does not build cannot run, however well it
    is tested."""
    agent_modules = {p.stem for p in (SRC / "agents").glob("*.py")
                     if p.stem not in ("__init__", "base", "registry")}
    built = {a.name for a in runtime.agent_registry.all()} | {"classification"}  # classification is per-job
    missing = agent_modules - built
    assert not missing, f"agents that exist but are never constructed: {missing}"


# -- the runtime -----------------------------------------------------------

@pytest.fixture
def resolved(resolved_config):
    """Named locally for readability; the construction is shared (conftest)."""
    return resolved_config


def test_the_runtime_reports_what_the_deployment_lacks(resolved, tmp_path):
    """A workflow that silently skips a step it could not run looks identical to
    one that ran it and found nothing."""
    thinned = resolved.model_copy(deep=True)
    thinned.wiring.backends.clear()
    runtime = Runtime(thinned, state_root=tmp_path / "s",
                      working_root=tmp_path / "w")
    missing = " ".join(runtime.unavailable())
    assert "no model backend" in missing
    assert "vision" in missing


def test_an_unknown_backend_kind_is_named_not_swallowed(resolved, tmp_path):
    """A configuration error that produces a quietly smaller deployment is worse
    than one that complains.

    Declarations are frozen, so the broken one is constructed rather than
    mutated — which is the type refusing to be tampered with, and correct.
    """
    from datadirector_contracts.policy import BackendDeclaration

    broken = resolved.model_copy(deep=True)
    broken.wiring.backends.append(BackendDeclaration(
        name="mystery", kind="not-a-kind", residency="on-premise"))
    runtime = Runtime(broken, state_root=tmp_path / "s",
                      working_root=tmp_path / "w")
    reported = " ".join(runtime.unavailable())
    assert "mystery" in reported and "not a backend kind" in reported


def test_classification_is_per_job_not_per_deployment(runtime):
    """A probe executor is bound to one job's budget and one job's material;
    sharing one would let a probe name another submission's artefact."""
    from datadirector.profiling.structural import profile_tree
    profile = profile_tree(runtime.working_root)
    first = runtime.classifier("job-" + "A" * 26, profile)
    second = runtime.classifier("job-" + "B" * 26, profile)
    assert first._executor is not second._executor


# -- the data management plan, three routes --------------------------------

def _submission(tmp_path, members: dict) -> Path:
    archive = tmp_path / "submission.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return archive


MADMP = json.dumps({"dmp": {"dataset": [{"distribution": [
    {"data_access": "open", "host": {"title": "Zenodo"},
     "license": [{"license_ref": "CC-BY-4.0"}]}]}]}})


def _dmp_events(runtime, job_id):
    return [e for e in runtime.store.load(job_id)
            if e.kind.value == "dmp.commitments-read"]


def test_a_plan_inside_the_submission_is_found(runtime, tmp_path, researcher):
    """The likeliest case in practice: exported from DMPonline, dropped beside
    the data, zipped together."""
    archive = _submission(tmp_path, {"observations.csv": "a,b\n1,2\n",
                                     "maDMP.json": MADMP})
    result = Pipeline(runtime).ingest(archive, human=researcher)
    payload = _dmp_events(runtime, result.job_id)[-1].payload
    assert payload["route"] == "found-in-submission"
    assert payload["structured"] is True
    assert payload["commitments"] >= 2


def test_a_supplied_reference_wins(runtime, tmp_path, researcher):
    """The depositor said where it is; searching would be second-guessing."""
    plan = tmp_path / "plan.json"
    plan.write_text(MADMP)
    archive = _submission(tmp_path, {"observations.csv": "a,b\n1,2\n"})
    result = Pipeline(runtime).ingest(archive, human=researcher,
                                      dmp_reference=str(plan))
    assert _dmp_events(runtime, result.job_id)[-1].payload["route"] == "supplied"


def test_no_plan_is_recorded_as_no_plan(runtime, tmp_path, researcher):
    """Not permission (ADR-023), and visible in the job rather than inferred
    from silence."""
    archive = _submission(tmp_path, {"observations.csv": "a,b\n1,2\n"})
    result = Pipeline(runtime).ingest(archive, human=researcher)
    payload = _dmp_events(runtime, result.job_id)[-1].payload
    assert payload["route"] == "none-found"
    assert payload["commitments"] == 0
    assert "not a statement that the data may be shared freely" in payload["note"]


def test_a_merely_likely_plan_becomes_a_question(runtime, tmp_path, researcher):
    """Verifying against another project's commitments is worse than verifying
    against none."""
    archive = _submission(tmp_path, {
        "observations.csv": "a,b\n1,2\n",
        "Data Management Plan.txt": "We will deposit in PANGAEA under CC-BY."})
    result = Pipeline(runtime).ingest(archive, human=researcher)
    assert result.gate_items, "a guess was acted on without asking"
    item = result.gate_items[0]
    assert "confirm" in item.summary.lower()
    assert not any(d.value == "consulted" for d in item.permitted_decisions)


def test_a_structured_plan_beats_a_named_one(runtime, tmp_path, researcher):
    """Structured commitments are facts about the plan; extracted ones are
    readings of it."""
    archive = _submission(tmp_path, {
        "observations.csv": "a,b\n1,2\n",
        "maDMP.json": MADMP,
        "Data Management Plan.pdf": "%PDF-1.7 whatever"})
    result = Pipeline(runtime).ingest(archive, human=researcher)
    assert _dmp_events(runtime, result.job_id)[-1].payload["structured"] is True
    assert not result.gate_items


def test_ingestion_requires_a_named_depositor(runtime, tmp_path):
    """Nothing here acts anonymously."""
    import inspect
    signature = inspect.signature(Pipeline.ingest)
    assert signature.parameters["human"].default is inspect.Parameter.empty


# ==========================================================================
# The workflow: deterministic orchestration, model-driven agents
# ==========================================================================

def test_no_model_decides_what_runs_next(runtime):
    """Orchestration is deterministic. An agent may use a model; nothing asks a
    model which agent to run.
    """
    from datadirector.pipeline import Pipeline
    engine = Pipeline(runtime).engine
    source = Path(__file__).parent.parent / (
        "packages/datadirector/src/datadirector/workflow/engine.py")
    text = source.read_text()
    for forbidden in ("ModelRequest", "complete(", "resolve_backend"):
        assert forbidden not in text, (
            f"the engine references {forbidden!r}: step selection must not "
            "consult a model")
    assert engine.steps, "the engine has no steps: the workflow is a skeleton"


def test_the_step_list_covers_the_automatic_work(runtime):
    """A deterministic engine with one step in it is a skeleton, which is what
    this was before the audit."""
    from datadirector.pipeline import Pipeline
    names = {s.name for s in Pipeline(runtime).engine.steps}
    assert {"classification", "metadata", "documentation", "validation"} <= names


def test_human_steps_are_absent_rather_than_waiting(runtime):
    """A step that is never runnable is clearer than one that runs and waits."""
    from datadirector.pipeline import Pipeline
    names = {s.name for s in Pipeline(runtime).engine.steps}
    assert not names & {"confirm_declaration", "review", "deposit"}


def test_a_transient_failure_is_retried(tmp_path, researcher):
    """A local model that timed out is the common case, and halting for a human
    on the first timeout demands attention the situation does not need."""
    from datadirector_contracts import DecisionRecord, Event, EventKind
    from datadirector.errors import ExternalServiceError
    from datadirector.provenance.recorder import Recorder
    from datadirector.state.store import EventStore
    from datadirector.workflow.engine import WorkflowEngine

    store = EventStore(tmp_path / "state")
    job_id = "job-" + "A" * 26
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.WORKFLOW_CREATED, agent="test/1.0"))

    attempts = []

    class Flaky:
        name = "flaky"

        def runnable(self, state):
            return len(attempts) < 2

        def run(self, state):
            attempts.append(1)
            if len(attempts) < 2:
                raise ExternalServiceError("the model timed out")
            return ([Event(sequence=1, job_id=job_id,
                           kind=EventKind.VALIDATION_COMPLETED,
                           agent="test/1.0")],
                    DecisionRecord(agent="test/1.0", step="flaky",
                                   selection_basis="recovered"))

    slept = []
    engine = WorkflowEngine(store, Recorder(store, tmp_path / "prov"), [Flaky()],
                            sleep=slept.append)
    engine.advance(job_id)

    assert len(attempts) == 2, "the step was not retried"
    assert slept and slept[0] > 0, "retry did not wait"
    assert not engine.state(job_id).halted_reason


def test_a_permanent_failure_is_not_retried(tmp_path):
    """A malformed record will be malformed again; repeating only delays the
    halt."""
    from datadirector_contracts import Event, EventKind
    from datadirector.provenance.recorder import Recorder
    from datadirector.state.store import EventStore
    from datadirector.workflow.engine import WorkflowEngine

    store = EventStore(tmp_path / "state")
    job_id = "job-" + "B" * 26
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.WORKFLOW_CREATED, agent="test/1.0"))
    attempts = []

    class Broken:
        name = "broken"

        def runnable(self, state):
            return not state.halted_reason

        def run(self, state):
            attempts.append(1)
            raise ValueError("the record is malformed")

    engine = WorkflowEngine(store, Recorder(store, tmp_path / "prov"),
                            [Broken()], sleep=lambda _: None)
    state = engine.advance(job_id)
    assert len(attempts) == 1
    assert "malformed" in (state.halted_reason or "")


def test_backoff_is_capped(tmp_path):
    """A delay long enough to look like a hang is worse than a halt someone can
    act on."""
    from datadirector.provenance.recorder import Recorder
    from datadirector.state.store import EventStore
    from datadirector.workflow.engine import WorkflowEngine

    store = EventStore(tmp_path / "state")
    engine = WorkflowEngine(store, Recorder(store, tmp_path / "prov"), [])
    assert engine._backoff(1) < engine._backoff(3)
    assert engine._backoff(20) <= 30.0


def test_an_instruction_reaches_the_log_before_it_is_interpreted(runtime,
                                                                 tmp_path,
                                                                 researcher):
    """What the depositor actually wrote survives independently of what was
    made of it."""
    from datadirector_contracts import EventKind
    archive = _submission(tmp_path, {"observations.csv": "a,b\n1,2\n"})
    result = Pipeline(runtime).ingest(archive, human=researcher,
                                      instruction="deposit this in EUDAT")
    received = [e for e in runtime.store.load(result.job_id)
                if e.kind is EventKind.INSTRUCTIONS_RECEIVED]
    assert received
    assert received[0].human == researcher
    assert received[0].payload["instruction"] == "deposit this in EUDAT"
    assert received[0].payload["channel"] == "authenticated-depositor"


def test_nested_archives_are_reported_not_silently_kept(runtime, tmp_path,
                                                        researcher):
    """A security-shaped function that returned in both branches asserted
    nothing and was called from nowhere; archives inside archives now surface."""
    import zipfile
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("hidden.csv", "x\n1\n")
    archive = _submission(tmp_path, {"observations.csv": "a,b\n1,2\n"})
    with zipfile.ZipFile(archive, "a") as zf:
        zf.write(inner, "inner.zip")

    result = Pipeline(runtime).ingest(archive, human=researcher)
    registered = [e for e in result.events
                  if e.kind.value == "material.registered"]
    assert registered
    assert "inner.zip" in registered[0].payload.get("nested_archives", [])


def test_a_halted_job_does_not_look_like_a_waiting_one(tmp_path, researcher):
    """The rule this system keeps arriving at: something that did not happen
    must not resemble something that did."""
    from datadirector_contracts import Event, EventKind
    from datadirector.state.projection import fold
    from datadirector.state.store import EventStore
    from datadirector.web.progress import PhaseState, phases_for

    store = EventStore(tmp_path / "state")
    job_id = "job-" + "H" * 26
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.WORKFLOW_CREATED, agent="t/1.0"))
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.MATERIAL_REGISTERED, agent="t/1.0",
                       payload={"artefacts": []}))
    waiting = phases_for(store.load(job_id), fold(store.load(job_id)))

    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.WORKFLOW_HALTED, agent="t/1.0",
                       payload={"reason": "the model could not be reached"}))
    halted = phases_for(store.load(job_id), fold(store.load(job_id)))

    assert PhaseState.HALTED not in {p.state for p in waiting}
    assert PhaseState.HALTED in {p.state for p in halted}
    assert any("could not be reached" in (p.detail or "") for p in halted)


def test_retries_are_visible_in_the_phase(tmp_path):
    """A job that recovered after two failures is not the same as one that ran
    cleanly, and only the record distinguishes them."""
    from datadirector_contracts import Event, EventKind
    from datadirector.state.projection import fold
    from datadirector.state.store import EventStore
    from datadirector.web.progress import phases_for

    store = EventStore(tmp_path / "state")
    job_id = "job-" + "R" * 26
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.WORKFLOW_CREATED, agent="t/1.0"))
    for attempt in (1, 2):
        store.append(Event(sequence=1, job_id=job_id,
                           kind=EventKind.STEP_FAILED, agent="t/1.0",
                           payload={"step": "classification",
                                    "attempt": attempt,
                                    "idempotency_key": f"k{attempt}"}))
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.CLASSIFICATION_COMPLETED, agent="t/1.0",
                       payload={"sensitivity": 1}))

    phases = phases_for(store.load(job_id), fold(store.load(job_id)))
    classification = next(p for p in phases if p.key == "classification")
    assert classification.attempts_failed == 2


def test_no_percentage_is_invented(tmp_path):
    """Steps are conditional, so a completion figure would be made up."""
    from datadirector.web.progress import Phase
    invented = {"percent", "percentage", "progress_ratio", "completion"}
    assert not invented & set(Phase.model_fields)


# ==========================================================================
# The two reports must agree
# ==========================================================================

def test_check_and_conformance_do_not_contradict_each_other(runtime):
    """`check` reported NOT AVAILABLE for every requirement while
    `conformance` reported full coverage: two commands in one tool giving
    opposite answers about the same deployment.

    The cause was that `check` read from entry-point discovery, and the
    built-in components are not entry points. They are different questions —
    what this deployment builds, versus what the source tree contains — but
    they may not disagree in the direction that matters: a requirement the
    runtime serves cannot be one the matrix calls absent.
    """
    from datadirector.conformance.report import generate

    built = set(runtime.coverage())
    report = generate(Path(__file__).parent.parent / "docs" / "architecture.md")

    contradictions = [
        requirement for requirement in built
        if report.claims.get(requirement) in ("Not implemented", "Out of scope")
    ]
    assert not contradictions, (
        "the runtime builds components for requirements the matrix records as "
        f"absent: {sorted(contradictions)}")


def test_the_runtime_reports_what_it_actually_constructed(resolved, tmp_path):
    """Not what is installed. A deployment with no repository plugin does not
    serve R7, however much code is present."""
    thinned = resolved.model_copy(deep=True)
    thinned.wiring.plugins.clear()
    runtime = Runtime(thinned, state_root=tmp_path / "s",
                      working_root=tmp_path / "w")

    coverage = runtime.coverage()
    servers = coverage.get("R7", [])
    assert "ZenodoDriver" not in servers, (
        "a repository driver that was never built is reported as serving R7")


def test_coverage_names_components_not_just_a_count(runtime):
    """'R7: available' invites the next question; naming the component answers
    it."""
    coverage = runtime.coverage()
    assert coverage["R7"], "R7 is served by nothing"
    assert all(isinstance(name, str) and name for name in coverage["R7"])


def test_every_must_requirement_is_served_by_the_example_configuration(runtime):
    """The configuration shipped with the project should produce a deployment
    that can do the mandatory work; if it cannot, the example is misleading."""
    coverage = runtime.coverage()
    must = ["R2", "R3", "R4", "R5", "R6", "R7", "R10"]
    unserved = [r for r in must if not coverage.get(r)]
    assert not unserved, (
        f"the example configuration serves none of {unserved}")


def test_probes_resolve_against_the_same_place_ingestion_writes(runtime,
                                                                tmp_path,
                                                                researcher):
    """Ingestion writes to `<working>/<job>/unpacked`; the probe executor was
    handed the bare working root, so every path it resolved was missing the job
    directory. Classification failed with "no such file" on a file that was
    plainly there.
    """
    import zipfile

    archive = tmp_path / "s.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("data/participants.csv", "id,name\n1,x\n")

    result = Pipeline(runtime).ingest(archive, human=researcher)
    unpacked = runtime.unpacked_root(result.job_id)
    assert unpacked.exists(), "ingestion did not write where the runtime says"

    from datadirector.profiling.structural import profile_tree
    executor = runtime.probe_executor(result.job_id, profile_tree(unpacked))
    assert Path(executor.root) == unpacked, (
        "the probe executor resolves against a different directory than "
        "ingestion writes to")


def test_one_place_knows_the_working_layout(runtime):
    """The path was assembled in six places. The seventh spelled it
    differently, which is how this class of bug happens."""
    import re
    from pathlib import Path as _P

    src = _P("packages/datadirector/src/datadirector")
    offenders = []
    for path in list(src.glob("*.py")) + list(src.glob("*/*.py")):
        if path.name in ("runtime.py", "ingestion.py", "publication.py"):
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r'working_root\s*/\s*\w+\s*/\s*"unpacked"', text):
            offenders.append(str(path))
    assert not offenders, (
        "these build the working path themselves instead of asking the "
        f"runtime: {offenders}")
