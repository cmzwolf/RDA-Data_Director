# Specification: a common agent interface

**Status:** proposed, not implemented.
**Baseline:** the repository as released (636 tests passing).
**Scope:** the orchestration layer. The domain model is not in scope and should
not be rewritten.

---

## 1. Why

Eleven agents exist. Seven are driven by the workflow graph; four — ingestion,
the plan reader, the declaration and publication — are invoked by name from
`Pipeline.ingest`, the web route and `JobService`. Earlier, *none* of the eleven
was reachable from any entry point, and nothing detected it.

The cause is that **"agent" is a category in the architecture and not a type in
the code**. Each has its own method name (`parse`, `classify`, `draft`,
`inspect_all`, `propose`, `shortlist`, `deposit`), its own signature and its own
return shape. The graph therefore cannot invoke an agent directly:
`Pipeline._runners()` holds a hand-written closure per agent to adapt it. An
agent without a closure is simply absent, and nothing says so.

An abstraction nothing has to satisfy cannot tell you when a member is unused.
That is why `ClassificationAgent.classify()` could be called with an argument it
does not take, and why metadata, documentation and validation could sit wired,
reachable and never running.

The aim is to make forgetting to wire an agent **impossible** rather than
detectable. Detection is what exists now, and the four hand-called agents are
what detection misses.

---

## 2. What is not in scope

Do not rewrite, and do not change the semantics of:

- material classes and the retention rules that follow from them
- `stated_sensitivity` / `inferred_sensitivity` and the tightening-only rule
  (ADR-027)
- gate items, decisions and the nodal points
- the event store, its hash chain, and the projection
- provenance, including the restricted partition
- the exposure ledger
- the Policy Enforcement Point and its ordering (policy → capability →
  preference → residency)
- the repository drivers, schema profiles, vocabulary provider, registry,
  validators

None of these was implicated in any of the twelve defects found during
development. Every defect was in orchestration or assembly.

---

## 3. The interface

### 3.1 What an agent receives

```
Invocation
  job          JobHandle       the job, not a list of paths
  instruction  Instruction | None
```

**Material is resolved through the job, never passed in.** An agent asks the
handle for the material it may see; it is not handed paths by its caller. This
is deliberate: when a caller chooses what an agent sees, the policy ceiling is
only as trustworthy as the caller, and a future composer may be a model. The
handle enforces the ceiling, so no caller — hand-written, user-composed or
model-composed — can widen what an agent is exposed to.

`JobHandle` must offer, at minimum:

- `job_id`
- `state` — the folded projection, read-only
- `material()` — artefacts this agent may see, filtered by the job's
  classification against the agent's declared ceiling
- `open(artefact)` — a reader for one artefact, charged to the exposure ledger
- `working(name)` — a path inside this job's working area, for outputs

The handle is the only route to files. An agent that takes a `Path` argument is
a defect, and the review that lands this change should reject one.

### 3.2 The instruction, and who wrote it

```
Instruction
  text      str
  author    Authority   depositor | operator | composer
  recorded  EventId     where the original text lives in the log
```

The trust boundary currently distinguishes two authorities: text from an
authenticated depositor is a **directive**; text inside a submitted file is
**content** and carries none. A single untyped `instruction` string collapses
that distinction the moment anything other than a person fills it in.

`author` therefore travels with the text. A `composer` instruction — written by
a model when automatic composition arrives — must never be treated as a
depositor directive. Adding this later is not possible without auditing every
call site, so it goes in now even though only `depositor` is used at first.

Agents pass `Instruction.text` to a model as `trusted_instructions` **only**
where `author is depositor`. Everything else goes in the content position.

### 3.3 What an agent returns

```
Outcome
  job_id        str
  events        list[Event]        for the history; may be empty
  gate_items    list[GateItem]     decisions a person must make; may be empty
  result        AgentResult        typed per agent, structured, not prose
  artefacts     list[str]          files produced, as job-relative names
  message       str                one or two sentences a researcher can read
  decision      DecisionRecord     what was chosen and on what basis
```

Four notes.

**`gate_items` is a named field, not part of `result`.** Some agents produce
findings; others produce decisions a human must make. If those arrive inside a
generic result, the human checkpoint becomes a convention the orchestrator must
remember to look for, and a composed pipeline can drop it by accident.

It is also the **only channel by which an agent may block a job** (§6). An agent
raises gate items and thereby restrains; it has no way to release. Validation
findings are deliberately *not* gate items — see §6.7.

**`events` may be empty and that is not "nothing happened".** The engine
previously stopped at the first step that emitted no events, because it measured
progress by state change. Progress is measured in steps run; see §5.

**`message` is for a person.** The history page narrates events into sentences
today (`web/narrate.py`) because the raw log was being rendered to users. An
agent that knows what it did can say so better than a narrator reconstructing it
afterwards. Narration remains as a fallback for events with no message.

**`artefacts` are job-relative names, never absolute paths**, for the same
reason material is resolved rather than passed.

### 3.4 Capabilities

```
Capabilities
  name             str
  summary          str            what it does, for a person
  requires         list[Condition]  what must hold before it may run
  establishes      list[Condition]  what holds afterwards, if it succeeds
  sensitivity_ceiling  SensitivityClass
  needs_backend    ModelCapability | None
  inspects_material bool          may it read the researcher's data? see §6.5
  human_follows    bool           a decision is required after this
  serves           tuple[str, ...]  requirement identifiers
```

`requires` and `establishes` are the important part and the reason this is not
merely a description. Half the defects during development were **unspecified
relationships between correctly specified components**: the architecture
document has sections for what each thing is and does, and none for what calls
what. Preconditions and postconditions put the edges where they can be checked.

They must be `Condition` objects carrying a sentence as well as a predicate, as
the workflow graph's edges already do. An edge readable only as code cannot be
drawn, explained to a researcher, or reviewed by anyone who does not read
Python.

`sensitivity_ceiling` is what `JobHandle.material()` filters against. An agent
declaring `INTERNAL` never sees sensitive artefacts, whoever invokes it.

---

## 4. The registry

Agents register themselves; nothing holds a hand-written list.

```
registry.register(agent)
registry.get(name)
registry.all()
registry.capabilities()      -> {name: Capabilities}
```

`Runtime` builds the registry. `Runtime._build_agents()` — the current
hand-written dictionary — goes away, along with the eleven-entry literal that an
added agent must remember to join.

**Composition remains hand-written for now.** The registry exists so that
user-composed and model-composed pipelines become possible later; neither is in
this scope. What matters now is that composition draws from a registry rather
than from a literal, so the later modes need no further change to the agents.

---

## 5. Orchestration

The workflow graph stays. Its nodes bind to registry names instead of to
closures, and every node has an agent — including ingestion, the plan, the
declaration and the deposit.

```
Node
  name       str          also the registry key
  human      bool         a person acts here; no agent runs
  label      str
```

Delete `Pipeline._runners()` entirely. A node is either a registry name or a
human checkpoint. There is no third kind, and no adapter layer.

**`WorkflowGraph` must refuse a node that names no registered agent and is not a
human checkpoint.** This is the whole point: an unwired agent becomes a graph
that will not build, rather than a test finding that must be written and
maintained.

Retain from the current engine, which was arrived at through failures:

- progress measured in **steps run**, not in state changed
- a `step.completed` event per run, so completion is durable and independent of
  what a step emitted
- the loop guard "this step ran and is still asking to run"
- `ExternalServiceError` retried with capped backoff; everything else halts at
  once

### 5.1 One vocabulary for position

`JobState.step` — a string projected from the log — and graph nodes are two
vocabularies for where a job is. They overlap, they disagree, and both are
consulted: this produced a job reporting it awaited nothing while the graph knew
a declaration was outstanding.

Pick one. The recommendation is the graph node, with `JobState.step` removed and
`web/progress.py` reading nodes directly. If `step` is kept for compatibility it
must be *derived from* the node and never consulted independently.

### 5.2 No workflow state outside the log

`Pipeline._pending_items` accumulates gate items on the Pipeline object. Kill
the process and they are lost. This contradicts the event-sourced design
outright and is the one existing violation this work must remove: gate items
returned in an `Outcome` are appended to the log by the orchestrator before it
moves on.

---

## 6. Safety: the job is blocked until a person says otherwise

The current system is safe partly because the hand-written pipeline contains the
right steps in the right order. That property does not survive composition, and
the dangerous failure of a composed pipeline is **omission**, not misordering: a
pipeline that simply leaves classification out looks perfectly well-formed.

Enforcement therefore moves into the object every agent must hold. A job carries
an execution status, and an agent that inspects material **cannot run on a
blocked job at all**. A malformed pipeline does not produce a bad deposit; it
stalls at the first agent after the missing step, where the failure is legible.

### 6.1 Two states, and no new stored state

```
JobStatus = blocked | unblocked
```

`blocked` is **derived from the event log**, not stored:

> A job is blocked if no declaration has been confirmed, or if any gate item is
> unresolved.

Both are already projected. Nothing new is persisted, and there is no status
field for an agent to write to — which is what makes the next property hold
without needing to be policed.

### 6.2 Agents restrain; only people release

An agent can raise gate items, which blocks. An agent cannot resolve a gate item
or confirm a declaration: both are human acts recorded against an ORCID, at the
nodal points the architecture already defines.

So the monotonicity you would otherwise have to enforce falls out of the
existing decision model. An agent can tighten and **structurally cannot loosen**,
because the only transitions that loosen are ones no agent can perform. There is
no rule to check, because there is no code path to misuse.

### 6.3 Born blocked

A job is blocked at creation, before anything has run. This is what closes
omission.

If the status were raised *by* classification, a pipeline omitting classification
would never raise it, and every later agent would run happily: a gate that is
real, and that nothing ever shut. Starting closed inverts that. Omitting a step
does not skip a check — it leaves the job in the state it was born in, and the
deposit refuses because nothing ever established a level.

**Absence of evidence stays un-clearance, and never becomes clearance.** This is
the failure this project met repeatedly from the other side — an unasked question
reported as a negative answer — and the ordering here is what prevents it.

### 6.4 What unblocks a job

The confirmation of the responsibility and compliance statement, and the
resolution of every outstanding gate item. Both are human acts.

Note what this means for the declaration agent: it **is** model-based — it sends
the researcher's prose to a backend and gets back proposed claims — and it still
cannot unblock anything. It proposes; the confirmation screen is where a person
accepts, and that act is what releases the job. Model-based proposal, human-only
release, which is the asymmetry the whole design rests on.

### 6.5 The deadlock, and the honest way out

If a job is born blocked and no agent may run on a blocked job, ingestion cannot
run either — and neither can the declaration agent, which produces the very
claims a person confirms. The gate deadlocks at birth.

A list of exempt agents would work and would rot. Instead, **an agent declares
whether it inspects material**:

```
Capabilities
  ...
  inspects_material   bool
```

- `inspects_material = False` — ingestion (profiles container structure, reads
  no content) and the declaration agent (reads the researcher's own statement,
  not the data). These may run on a blocked job.
- `inspects_material = True` — classification, media, redaction, metadata,
  documentation, validation, publication. These may not.

This makes the rule state something true rather than procedural: **nothing looks
at your data until you have told us what it is.** That is §9.3 of the
architecture, expressed as a gate on execution rather than as an ordering
somebody has to maintain.

### 6.6 What this replaces

An earlier draft of this section put a checklist at the deposit: refuse unless a
declaration exists, a level was established, the gate is clear, validation
passed. That is weaker for a specific reason — it requires the deposit to know
every safety condition in advance, so a new condition means editing the deposit,
and whoever composes a pipeline will not think to. Under the status rule, deposit
is an ordinary agent with `inspects_material = True`, gated exactly like the
others, and a new condition needs no edit to it at all.

Two invariants remain outside the status, because they are about material rather
than about workflow position:

- **No agent sees material above its ceiling**, enforced in `JobHandle`, never by
  the caller.
- **`PublicationAgent`'s refusals stay inside the agent.** They were bypassed
  once already, when the deposit route called the driver directly.

### 6.7 One thing to decide deliberately

A deterministic check that can be **recomputed** presents an edge. Validation
runs, finds no blocking error, and the job should be free to proceed. If only a
human may loosen, a person must confirm a machine-checkable fact — which is
either right (someone should see the result) or friction that trains people to
click through.

The recommendation: validation findings are **not** gate items. They are
recomputable — fix the metadata, re-run, the error is gone — so treating them as
human-discharged obligations would accumulate stale blockers that a re-run cannot
clear. A redaction proposal is a gate item; a validation error is a finding that
the deposit consults. Declare which a thing is when it is raised, not when
someone tries to clear it.

## 7. Migrating the eleven agents

Each becomes `run(invocation) -> Outcome` plus a `capabilities` property. The
existing method bodies mostly survive; what changes is the signature, the return
shape, and where material comes from.

| Agent | Note |
|---|---|
| ingestion | Deterministic. Receives the submission through the handle. Currently called from `Pipeline.ingest` |
| declaration | Currently called from the web route. Its `Instruction` is the researcher's statement, author `depositor` |
| dmp | Currently called from `Pipeline._resolve_dmp`. In-container detection stays |
| classification | Holds a per-job probe executor; construct it from the handle |
| media | `requires` an artefact with a medium; ceiling depends on the backend found |
| redaction | Returns gate items, no events. This is legitimate and must not stop the workflow |
| metadata | Standard selection (`schema_choice`) belongs in its `Instruction` handling |
| documentation | `requires` a drafted record |
| validation | Deterministic. `requires` a drafted record |
| repository | Reaches a registry; must stay out of ingestion, where it made uploads wait on a network call |
| publication | Currently called from `JobService`. Its four refusals must remain inside it |

`agents/base.py` becomes the protocol rather than a convenience base class.

---

## 8. Done means

- No call site anywhere reaches an agent by name. `grep 'agents\["' ` returns
  nothing outside the registry.
- `WorkflowGraph` refuses to build when a node names no registered agent.
- No agent accepts a `Path`; material comes only through `JobHandle`.
- `Pipeline._runners()` and `Runtime._build_agents()` no longer exist.
- `Pipeline._pending_items` no longer exists.
- One vocabulary for job position.
- A job is born blocked, and an agent with `inspects_material = True` refuses to
  run on one. Tested with a pipeline that deliberately omits classification: it
  must stall rather than deposit.
- No agent can resolve a gate item or confirm a declaration. `grep` finds no such
  call outside a human-initiated route.
- The existing suite still passes, with the orchestration tests rewritten and
  the domain tests untouched. **If a domain test needs changing, something has
  gone wrong: say so rather than changing it.**

---

## 9. How to carry it out

Land it in slices that each leave the suite green:

1. `JobHandle`, `Invocation`, `Outcome`, `Capabilities`, the protocol, the
   registry. Nothing uses them yet.
2. Migrate the seven graph-driven agents. The graph binds registry names;
   `_runners()` shrinks.
3. Migrate the four hand-called agents — ingestion, declaration, dmp,
   publication — and delete their call sites. This is the slice that fixes the
   original complaint.
4. Enforce: graph refuses unbound nodes, `JobHandle` enforces ceilings, and
   `inspects_material` agents refuse a blocked job.
5. Remove the second position vocabulary and `_pending_items`.

**Run the software at the end of each slice, not just the tests.** Every defect
found in this project so far was found within seconds of someone using it, while
hundreds of tests passed. The suite measures components; a person at a terminal
measures the system.

---

## 10. Deliberately deferred

Not in this scope, and the interface above is shaped so they need no further
change to the agents:

- **User-composed pipelines.** A researcher assembles a pipeline in the web
  interface from `registry.capabilities()` and runs it against a job.
- **Model-composed pipelines.** A `pipeline_builder` proposes a composition. It
  is a proposal, and it needs no separate validation: a composition that omits a
  step cannot deposit, because the job stays blocked and the agents that would
  release it are the ones a person drives. The §6 status does the work that
  would otherwise require inspecting the proposed pipeline for correctness. A model composing the workflow *is* a model
  deciding what runs next, so the determinism guarantee moves from "no model
  chooses" to "a model may propose, a person approves, and the irreversible act
  enforces regardless".

The three modes over one registry are also an experiment worth running: does a
model compose a valid pipeline, does it omit the inconvenient steps, and is the
refusal comprehensible to the researcher who receives it? Few people have an
empirical answer to that.
