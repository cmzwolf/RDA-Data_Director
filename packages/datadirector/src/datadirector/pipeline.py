"""Driving a job from ingestion to the approval gate.

The audit that produced `runtime.py` found every agent unreachable. This is the
other half: something that runs them in order and stops where a person is
required.

**It stops rather than waits.** Each call advances the job as far as it can and
returns; it does not block on a human. A workflow that held a process open
waiting for a researcher to come back from lunch would be a workflow that loses
its place when the process dies, and the whole state design exists so that it
does not.

**It stops at the first thing it cannot do.** A missing capability, an
unresolved gate item and an unanswered declaration all halt the run, and each
halt says what is wanted. Skipping a step that could not run would produce a job
that looks complete and was not inspected — the failure the media tier is built
around, arriving at the level of the workflow.

Stepping and halting are `WorkflowEngine`'s job, not this module's. A first
version of this file reimplemented them, which left the engine unreachable and
gave the system two answers to "what runs next". The engine keeps the halt and
compensation semantics; the pipeline supplies the steps and the entry point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from datadirector_contracts import (
    Event, EventKind, GateItem, Orcid, SensitivityClass,
)

from .care.referral import detect as detect_care
from datadirector_contracts import GateItemKind
from .gate.items import care_referral_item, discrepancy_item
from .repository_choice import (
    confirmation_item, divergence_item, from_instruction, from_plan,
)
from . import schema_choice
from .profiling.structural import profile_tree
from .runtime import Runtime
from .state.projection import fold
from .agents.base import Authority, Instruction, Invocation
from .job_handle import (JobHandle, latest_draft_was_a_persons,
                       log_items, stage_submission)
from .workflow.definition import build as build_graph
from .workflow.engine import Step, WorkflowEngine


@dataclass
class Advance:
    """What one call to `advance` did, and what it is waiting for."""

    job_id: str
    ran: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    gate_items: list[GateItem] = field(default_factory=list)
    awaiting: str | None = None
    # The graph node a person must act on, so a caller can link to the right
    # screen rather than parsing prose.
    blocking_node: str | None = None
    unavailable: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.awaiting is not None


class _GraphStep:
    """One graph node, presented to the engine as a step.

    The node names the agent that performs it, the registry provides the
    agent, and the handle narrows the job to what that agent's stated
    capabilities entitle it to. Runnability is asked of the graph rather
    than of a predicate held here: the condition belongs to the edge, where
    it can be read and drawn.
    """

    def __init__(self, pipeline: "Pipeline", node) -> None:
        self._pipeline = pipeline
        self._node = node
        self.name = node.name
        self._agent = pipeline.runtime.agent_registry.get(
            node.agent or node.name)

    def runnable(self, state) -> bool:
        node = self._node
        if node.human:
               # What happens there is the person's own act, recorded at
               # the entry point that authenticated it, never run here.
            return False
        done = self._pipeline.completed(state.job_id)
        if node.name in done:
            return False
        candidate = self._pipeline.graph.next_node(state, done=done)
        if candidate is None or candidate.name != node.name:
            return False
        return all(condition(state)
                    for condition in self._agent.capabilities().requires)

    def run(self, state):
        handle = self._pipeline.handle_for(self._agent, state.job_id)
        outcome = self._agent.run(
            Invocation(job=handle, instruction=self._instruction(state)))
        events = list(outcome.events)
        known = {item.item_id for item
                  in log_items(self._pipeline.runtime.store.load(
                         state.job_id))}
        items = [item for item in outcome.gate_items
                  if item.item_id not in known]
        if items:
               # The agent reports what it would assert; the orchestrator
               # is the only thing that appends, and it appends on the
               # agent's behalf with the agent's identity attached.
            events.append(Event(
                sequence=1, job_id=state.job_id,
                kind=EventKind.REDACTION_PROPOSED,
                agent=self._agent.identity,
                payload={"marker": "gate.items-added",
                          "items": [json.loads(i.model_dump_json())
                                    for i in items]}))
        return events, outcome.decision

    def _instruction(self, state):
        """The depositor's instruction, from the entry point that
        authenticated it. The same words arriving any other way - inside a
        submitted document, say - carry no authority and never become an
        `Instruction`.
        """
        for event in reversed(self._pipeline.runtime.store.load(
                state.job_id)):
            if (event.kind is EventKind.INSTRUCTIONS_RECEIVED
                    and event.payload.get("channel")
                      == "authenticated-depositor"
                    and event.payload.get("instruction")):
                return Instruction(
                    text=event.payload["instruction"],
                    author=Authority.DEPOSITOR,
                    recorded=event.sequence)
        return None


class Pipeline:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.graph = build_graph(runtime.agent_registry)
        self.engine = WorkflowEngine(runtime.store, runtime.recorder,
                                     self._graph_steps())

    def handle_for(self, agent, job_id: str) -> JobHandle:
        """A handle narrowed to one agent's stated capabilities.

        Built here, before the agent is entered, because after it there is
        nothing left to check: what an agent was entitled to ask for is
        fixed by what it declared before it ran.
        """
        runtime = self.runtime
        capabilities = agent.capabilities()
        return JobHandle(
            job_id, store=runtime.store, working_root=runtime.working_root,
            ceiling=capabilities.sensitivity_ceiling,
            inspects_material=capabilities.inspects_material,
            probe_executor_factory=runtime.probe_executor,
            dmp_source_factory=runtime.plan_source,
            ledger=runtime.ledger)

    def _graph_steps(self) -> list[Step]:
        """The graph's automatic nodes, as steps the engine can run.

        The engine keeps its halt, retry and compensation semantics; the graph
        decides what may run and in what order. Two mechanisms would be one too
        many, and the engine is the one with the failure handling.
        """
        return [_GraphStep(self, node) for node in self.graph.nodes.values()
                if node.automatic]

    def completed(self, job_id: str) -> set[str]:
        """Graph nodes this job has already been through.

        Derived from the events each node produces, so a restarted process
        resumes at the same place. A node that produces nothing is considered
        done once a later node has produced something, which is why `produces`
        is declared on the node rather than inferred.
        """
        events = self.runtime.store.load(job_id)
        seen = {e.kind for e in events}

        # A step that ran is done, whether or not it emitted anything of its
        # own. Inferring completion from produced events alone left nodes that
        # produce none — redaction, media with nothing to inspect — permanently
        # incomplete, and the traversal returned them forever.
        done = {e.payload.get("step") for e in events
                if e.kind is EventKind.STEP_COMPLETED and e.payload.get("step")}

        for name, node in self.graph.nodes.items():
            if node.produces and all(kind in seen for kind in node.produces):
                done.add(name)
        return {name for name in done if name}


    # -- entry -------------------------------------------------------------

    def ingest(self, source: Path | str, *, human: Orcid,
               dmp_reference: str | None = None,
               instruction: str | None = None,
               preference=None) -> Advance:
        """Create a job and take in the material.

        The data management plan is resolved here rather than later, because
        what it commits to shapes what follows: a plan naming a repository is
        the first candidate the repository agent should show.
        """
        runtime = self.runtime
        job_id = _new_job_id()
        runtime.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.WORKFLOW_CREATED,
            agent="pipeline/0.1.0",
            payload={"config_digest": str(runtime.config.digest()),
                     "created_by": human.value}))

        stage_submission(source, runtime.working_root, job_id)
        agent = runtime.agent_registry.get("ingestion")
        handle = self.handle_for(agent, job_id)
        outcome = agent.run(Invocation(job=handle))
        events = list(outcome.events)
        for event in events:
            runtime.store.append(event)

        result = Advance(job_id=job_id, ran=["ingestion"], events=events,
                         unavailable=runtime.unavailable())
        commitments = self._resolve_dmp(job_id, dmp_reference, result)
        if preference is not None:
            self._record_preference(job_id, preference, human=human)
        result.gate_items += self._care_referral(job_id)
        result.gate_items += self._repository_choice(job_id, instruction,
                                                     commitments, human)
        result.gate_items += self._schema_choice(job_id, instruction,
                                                 commitments, human)
        return result

    def _repository_choice(self, job_id: str, instruction: str | None,
                           commitments: list, human: Orcid) -> list[GateItem]:
        """Where to deposit, from the depositor, the plan, or neither.

        The depositor's instruction is recorded before it is interpreted, so
        what they actually wrote survives independently of what was made of it.
        """
        runtime = self.runtime
        items: list[GateItem] = []
        committed = from_plan(commitments)
        instructed = None

        if instruction:
            runtime.store.append(Event(
                sequence=1, job_id=job_id,
                kind=EventKind.INSTRUCTIONS_RECEIVED, agent="pipeline/0.1.0",
                human=human,
                payload={"instruction": instruction[:2000],
                         "channel": "authenticated-depositor"}))
            try:
                instructed, _ = from_instruction(
                    runtime.pep, instruction,
                    classification=SensitivityClass.SENSITIVE)
            except Exception:
                # A model that cannot be reached must not silently discard what
                # the depositor asked for: the instruction is on the record and
                # they will be asked at the gate.
                instructed = None

        # The registry is *not* consulted here. Shortlisting reaches a remote
        # service, and ingestion should not wait on a network call: taking in a
        # file is local work, and a researcher whose upload hangs because a
        # registry is slow has been failed by an ordering decision, not by a
        # registry. The shortlist belongs to the repository node, which runs
        # later and can halt and retry like any other step.
        if instructed:
            items.append(confirmation_item(instructed, candidates=[]))
            if committed:
                divergence = divergence_item(instructed, committed)
                if divergence:
                    items.append(divergence)
        return items

    def chosen_standard(self, job_id: str) -> str:
        """The metadata standard settled on for this job, from the log."""
        for event in reversed(self.runtime.store.load(job_id)):
            if event.payload.get("metadata_standard"):
                return event.payload["metadata_standard"]
        return schema_choice.DEFAULT

    def _schema_choice(self, job_id: str, instruction: str | None,
                       commitments: list, human: Orcid) -> list[GateItem]:
        """Which standard to project into: your instruction, the plan, or the
        default.

        The profile was hard-coded, so a plan committing to DataCite was parsed
        and discarded and "use RO-Crate" was read past — both invisibly, because
        DataCite is the default and asking for the default looks like being
        obeyed.
        """
        runtime = self.runtime
        items: list[GateItem] = []
        committed = schema_choice.from_plan(commitments)
        instructed = None

        if instruction:
            try:
                instructed, _ = schema_choice.from_instruction(
                    runtime.pep, instruction)
            except Exception:
                instructed = None

        chosen = schema_choice.resolve(instructed, committed)
        runtime.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.INSTRUCTIONS_RECEIVED,
            agent="pipeline/0.1.0", human=human,
            payload={"channel": "metadata-standard",
                     "metadata_standard": chosen.standard,
                     "chosen_because": chosen.source}))

        for candidate in (instructed, committed):
            if candidate is not None and not candidate.recognised:
                items.append(schema_choice.unrecognised_item(candidate))
        if instructed and committed and instructed.recognised \
                and committed.recognised:
            divergent = schema_choice.divergence_item(instructed, committed)
            if divergent:
                items.append(divergent)
        return items

    def _record_preference(self, job_id: str, preference, *,
                           human: Orcid) -> None:
        """Keep what the depositor asked for, and apply it to this job.

        Recorded as well as applied, because a deposit whose classification was
        made by a model the researcher chose should carry that fact: "the model
        said sensitive" and "the model they picked said sensitive" are different
        claims.
        """
        self.runtime.pep.default_preference = preference
        self.runtime.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.INSTRUCTIONS_RECEIVED,
            agent="pipeline/0.1.0", human=human,
            payload={"channel": "backend-preference",
                     "residency_at_most": (preference.residency_at_most.value
                                           if preference.residency_at_most
                                           else None),
                     "backend_names": list(preference.backend_names),
                     "note": "a preference narrows what policy permits; it can "
                             "never widen it"}))

    def _care_referral(self, job_id: str) -> list[GateItem]:
        """Detect whether CARE may apply, from the material's own vocabulary.

        Runs at ingestion rather than at classification because it needs no
        model and no inspection: it reads file and column names, which are
        already known. A referral raised early is one the researcher can act on
        while there is still time to consult someone.
        """
        root = self.runtime.unpacked_root(job_id)
        if not root.exists():
            return []
        names = [str(p.relative_to(root)) for p in root.rglob("*")
                 if p.is_file()]
        profile = profile_tree(root)
        columns = [c.name for entry in getattr(profile, "files", [])
                   for c in getattr(entry, "columns", [])]
        assessment = detect_care(field_names=names + columns)
        item = care_referral_item(assessment)
        return [item] if item else []

    # -- the declaration ---------------------------------------------------

    def record_statement(self, job_id: str, statement: str, *,
                         human: Orcid) -> None:
        """Keep what the researcher wrote, before anything reads it.

        Verbatim and separately, so the statement survives independently of the
        claims made from it. If the parse is wrong, the evidence of what was
        actually said is still there.
        """
        self.runtime.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.INSTRUCTIONS_RECEIVED,
            agent="pipeline/0.1.0", human=human,
            payload={"statement": statement[:20000],
                     "channel": "responsibility-and-compliance-statement"}))

    def parse_declaration(self, job_id: str, statement: str):
        """Read the statement into proposed claims.

        The claims carry no authority: they are a reading, and they become
        assertions only when a person confirms them (section 9.2).
        """
        runtime = self.runtime

        agent = runtime.agent_registry.get("declaration")
        handle = self.handle_for(agent, job_id)
        outcome = agent.run(Invocation(job=handle))
        proposed = outcome.result or []
        events = list(outcome.events)
        decision = outcome.decision
        for event in events:
            runtime.store.append(event)
        return proposed, decision

    def revalidate_metadata(self, job_id: str):
        """Re-check the record against the chosen standard, offline.

        The same validation node the graph runs, run again on demand. It reads
        the record back from the log rather than being handed it, so what it
        reports is the record that will actually be deposited — and re-running
        it after a researcher's revision is what lets a blocking finding be
        cleared without a second, web-only notion of what "valid" means. No
        model is involved: this is schema conformance and cross-source
        consistency, so it is cheap and deterministic to repeat.

        The findings are appended as a new `VALIDATION_COMPLETED`, the newest of
        which the deposit reads. Reporting them is the whole job; deciding what
        blocks is the gate's and the person's, never this method's.
        """
        runtime = self.runtime
        agent = runtime.agent_registry.get("validation")
        handle = self.handle_for(agent, job_id)
        outcome = agent.run(Invocation(job=handle))
        for event in outcome.events:
            runtime.store.append(event)
        return outcome.result or []

    def redraft_metadata(self, job_id: str):
        """Run the drafting agents again over whatever the log now holds.

        An empty Description is not a failure the system can see: nothing was
        refused, nothing retried, and the blank stood as though it had been
        chosen. The agents draft from what the log records, so anything the
        depositor has said, confirmed, or supplied since the first draft is
        material the first draft never saw. Running them again is how that
        material reaches the record without the depositor having to write the
        abstract by hand; and a new draft is appended, never a rewritten one, so
        the earlier answer stays on the log to be compared against.

        Metadata first and documentation second, because documentation fills
        the fields the record still lacks from the README it is about to write.
        """
        runtime = self.runtime
        outcomes = []
         # An agent re-drafts an agent's draft. A record a person last
         # wrote stays theirs: the newest draft is what the review screen
         # reads, so a fresh machine draft would push their wording out of
         # view because a button was pressed. The documentation still runs,
         # because a README is worth having whatever the record says.
        revised = latest_draft_was_a_persons(runtime.store.load(job_id))
        names = ["documentation"] if revised else [
             "metadata", "documentation"]
        for name in names:
            agent = runtime.agent_registry.get(name)
            handle = self.handle_for(agent, job_id)
            outcome = agent.run(Invocation(job=handle))
            for event in outcome.events:
                runtime.store.append(event)
            outcomes.append(outcome)
        return outcomes

    def proposed_claims(self, job_id: str) -> list:
        """The claims last proposed, read back from the log."""
        from datadirector_contracts import DeclarationClaim

        for event in reversed(self.runtime.store.load(job_id)):
            if event.kind is EventKind.DECLARATION_PARSED:
                claims = event.payload.get("claims") or []
                out = []
                for raw in claims:
                    try:
                        out.append(DeclarationClaim.model_validate(raw))
                    except Exception:
                        continue
                return out
        return []

    # -- the plan ----------------------------------------------------------

    def _resolve_dmp(self, job_id: str, reference: str | None,
                     result: Advance) -> list:
        """Three routes to a plan, and a fourth outcome that is not permission.

        An explicit reference wins: the depositor said where it is. Otherwise
        the submitted material is searched, because in practice the plan arrives
        in the same archive as the data. A candidate that is only *likely*
        becomes a question rather than an assumption — verifying a deposit
        against another project's commitments is worse than verifying against
        none.
        """
        runtime = self.runtime
        agent = runtime.agent_registry.get("dmp")
        if reference:
              # Stage the reference so the handle's supplied_plan() finds
              # it; the agent resolves its own source from the handle.
            plan_dir = runtime.working_root / job_id / "plan"
            plan_dir.mkdir(parents=True, exist_ok=True)
            (plan_dir / "reference.txt").write_text(
                reference, encoding="utf-8")
        handle = self.handle_for(agent, job_id)
        outcome = agent.run(Invocation(job=handle))
        for event in outcome.events:
            runtime.store.append(event)
        result.gate_items += outcome.gate_items
        return outcome.result if outcome.result else []

    def _plan_discrepancies(self, job_id: str, state) -> list[GateItem]:
        """Compare the plan against what is about to be deposited.

        Runs at advance rather than at ingestion because the comparison needs a
        record and a repository, and neither exists yet when the material
        arrives.
        """
        runtime = self.runtime
        events = runtime.store.load(job_id)
        read = [e for e in events if e.kind is EventKind.DMP_COMMITMENTS_READ]
        if not read or not read[-1].payload.get("commitments"):
            return []

        reference = read[-1].payload.get("reference")
        if not reference:
            return []
        agent = runtime.agent_registry.get("dmp")
        handle = self.handle_for(agent, job_id)
        try:
            outcome = agent.run(Invocation(job=handle))
        except Exception:
            return []
        commitments = outcome.result
        if not commitments:
            return []
        structured = (outcome.events[0].payload.get("structured", False)
                      if outcome.events else False)

        chosen = next((e.payload.get("repository") for e in reversed(events)
                       if e.kind is EventKind.REPOSITORY_SELECTED), None)
        record = _drafted_record(events)
        if chosen is None and record is None:
              # Nothing to compare against yet. Not a discrepancy: a comparison
              # that has not been made must not look like one that found nothing.
            return []

        discrepancies, _, _ = agent.verify(state, commitments, record,
                                           repository=chosen,
                                           structured=structured)
        return _plan_discrepancy_items(discrepancies)

    # -- advancing ---------------------------------------------------------

    def advance(self, job_id: str, *, human: Orcid | None = None) -> Advance:
        """Run what can be run, and say what is wanted when it cannot.

        What is wanted comes from the **graph**, not from the job's step name. An
        earlier version compared `state.step` against a hand-written list of
        names, and a job sitting at a step absent from that list reported that it
        was waiting for nothing — a status worse than wrong, because a researcher
        reads it as "done".
        """
        runtime = self.runtime
        state = fold(runtime.store.load(job_id))
        result = Advance(job_id=job_id, unavailable=runtime.unavailable())
        result.gate_items += self._plan_discrepancies(job_id, state)

        before = state.step
        state = self.engine.run_until_blocked(job_id)
        if state.step != before:
            result.ran.append(before)

        if state.halted_reason:
            result.awaiting = state.halted_reason
            return result

        blocking = self.graph.blocking_human_node(
            state, done=self.completed(job_id))
        if blocking is not None:
            result.awaiting = blocking.label
            result.blocking_node = blocking.name
        return result

def _compact(profile) -> dict:
    """A profile small enough to put in a prompt.

    Falls back to an empty summary rather than sending the whole tree: a
    profile that cannot be compacted is one we should not be pasting into a
    model call.
    """
    compact = getattr(profile, "compact", None)
    if compact is None:
        return {}
    try:
        return compact()
    except Exception:
        return {}


def _has(runtime, state, kind: EventKind) -> bool:
    return any(e.kind is kind for e in runtime.store.load(state.job_id))


def _drafted_record(events):
    """The canonical record as last drafted, or nothing.

    Read from the log rather than held, so a comparison made after a restart
    uses the same record a person approved.
    """
    from datadirector_contracts import CanonicalRecord
    for event in reversed(events):
        if event.kind is EventKind.METADATA_DRAFTED and event.payload.get("record"):
            try:
                return CanonicalRecord.model_validate(event.payload["record"])
            except Exception:
                return None
    return None


def _plan_discrepancy_items(discrepancies) -> list[GateItem]:
    """Differences between the plan and the deposit, as things a person decides.

    `discrepancy_item` existed and was called from nowhere, so the DMP agent
    found divergences and nothing put them in front of anybody. Flagged, never
    enforced: plans are written years before the data exist.
    """
    return [
        discrepancy_item(
            item_id=f"dmp-discrepancy:{d.kind.value}",
            summary=(f"The plan commits to {d.committed!r} for "
                     f"{d.kind.value}; the deposit has {d.actual!r}."),
            detail=([d.plan_section] if d.plan_section else [])
            + (["this commitment was extracted from prose rather than read "
                "from a structured field, so it is a reading of the plan"]
               if d.provisional else []),
            kind=GateItemKind.DMP_DISCREPANCY)
        for d in discrepancies
    ]


def _new_job_id() -> str:
    import random
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "job-" + "".join(random.choice(alphabet) for _ in range(26))
