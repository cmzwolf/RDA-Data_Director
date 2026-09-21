# Handover

For whoever picks this up next, including a future session of this assistant.
The architecture document describes the system; this describes its *state* —
what has been run against something real, what has only been run against tests,
and what went wrong often enough to be worth expecting again.

Read `docs/architecture.md` for the design and `README.md` for how to start it.
This file is the part that does not belong in either: the difference between
"implemented" and "known to work", and how the work should be conducted.

---

## 1. How to work on this

**Ask before building.** Over the course of development the assistant
repeatedly noticed a problem and built a solution in the same turn, without
proposing it first. Twice this produced work that had to be reversed — a
single-user session command that quietly inverted the multi-user design, and an
HTTPS enforcement path that made local testing harder for no benefit at that
stage. Noticing a problem is not a mandate to solve it. Describe the options,
say which you would pick and why, and stop.

**A green suite proves almost nothing about the system.** Every defect the
author found, he found within seconds of using the software, while 600+ tests
passed. The suite measures components; a person at a terminal measures the
system. Specifically:

- four CLI subcommands were dispatchable and had no parser entry, so typing them
  gave "invalid choice";
- `serve` raised `ModuleNotFoundError` from an import above the check meant to
  guard it;
- the API and the interface both claimed `GET /`, so the browser got JSON;
- `ClassificationAgent.classify()` was called with an argument it does not take;
- the workflow silently stopped after the first step that emitted no events.

None of these were visible to the offline suite. When something is changed, run
it.

**When the document and the code disagree, the code is wrong by default.** The
architecture document exists to be able to say the implementation is incorrect.
Editing it to match what was built destroys that property and turns it into a
description of whatever happened. Change the specification only when the design
itself has been reconsidered and the change is deliberate — and say so when you
do. Two edits made during development were legitimate on these grounds: the
workflow moved from an ordered step list to a graph, and the metadata standard
became selectable, both by decision rather than by drift. Everything else that
looked "stale" was the code being wrong.

**Verify string edits.** A large number of defects in this codebase were
introduced by `str.replace` calls that matched nothing and failed silently. If
you patch by replacement, assert the old text was present.

**Do not simulate a missing dependency by raising from `find_spec`.** A genuinely
absent module makes `find_spec` return `None`. This mistake was made twice and
both times produced a test that proved the wrong thing.

---

## 2. What has actually been run

| Area | Status |
|---|---|
| Offline suite | 636 tests, ~30s, no network (a guard enforces this) |
| Local model classification, declaration, media | Run live against Ollama earlier in development |
| Zenodo deposit | Run against the **sandbox**; five divergences found and fixed |
| OLS vocabulary grounding | Run live; ground/search separation verified |
| **ORCID sign-in** | **Never run against ORCID.** Tests stub the token endpoint |
| **The full agent chain through the web interface** | **Never run end to end with a live model** |
| **Metadata standard selection** | Unit-tested only |
| **Publication refusals through the browser** | Unit-tested only |
| WCAG 2.1 AA | **Not claimed.** Structural checks only; no screen reader pass |

The rows in bold are where to expect trouble. The Zenodo sandbox run is the
precedent: five things the implementation believed about the API were wrong, and
every one of them was found in the first hour of contact with the real service.

---

## 3. The failure pattern, which is also the paper's finding

The same defect recurred in at least six distinct forms, and it is worth stating
as one thing because it will recur again.

**Something complete, tested, documented — and not connected.**

- Ten agents built and reachable from no entry point.
- Four CLI handlers with no parser entry.
- `assert_no_nested_archive`, a security-shaped function that returned in both
  branches and was called from nowhere.
- `discrepancy_item`, which built gate items nothing raised.
- `PublicationAgent`, holding four deposit refusals, bypassed by the deposit
  route.
- Metadata, documentation and validation, wired and reachable, never running.

And its mirror image, which is worse because it produces confident falsehood
rather than silence:

**An unasked question reported as a negative answer.**

- `check` reported every requirement NOT AVAILABLE because it read entry-point
  discovery, while `conformance` reported the opposite.
- The repository confirmation said "no repository called 'Zenodo' was found in
  the registry" when no registry had been consulted.
- A phase passed without evidence was marked *done* rather than *skipped*.

The countermeasures now in place — the reachability test, the CLI surface test,
the assembly test, the graph's orphan detection — each closed one boundary. None
generalised to the next. That is the honest finding: **the question has to be
asked separately at every boundary**, and "we added a reachability test" does
not transfer.

### Where the defects came from

Twelve substantive defects, classified by origin, split about evenly.

| Defect | Origin |
|---|---|
| Ten agents unreachable from any entry point | Specification |
| `PublicationAgent` bypassed at deposit | Specification |
| Metadata standard unselectable | Specification |
| `check` and `conformance` contradicting each other | Specification |
| API and interface both claiming `GET /` | Specification |
| Workflow stopping after the first step emitting no events | Specification |
| Four CLI handlers with no parser entry | Implementation |
| `classify()` called with an argument it does not take | Implementation |
| Probe paths missing the job directory | Implementation |
| `assert_no_nested_archive` returning in both branches | Implementation |
| Registry consulted synchronously during ingestion | Implementation |
| "not found in the registry" after the registry stopped being consulted | Implementation |

The two halves are not alike.

**The implementation slips are ordinary and cheap.** A wrong argument, a wrong
path, an edit that matched nothing. Each was found within seconds of someone
running the software, and each is one line to fix. They are the cost of working
quickly, and the remedy is to run the thing.

**Every specification-origin defect is an unspecified *relationship*, never an
unspecified *component*.** That is not a coincidence, and it is the most useful
thing this project learned.

Look at the architecture document's own contents: layers, agents, plugins, the
capability manifest, the conformance matrix. It says what each thing **is** and
what each thing **does**. It has no section saying what **calls** what. The
cluster specifications are organised the same way, one per group of components.

So the declaration agent is specified and the classification agent is specified,
and that classification must not run before a confirmed declaration was written
as a property of the system, in prose, rather than as an edge anything could
check. Every component was present and correct; nothing said the assembly was an
artefact to be built, because no document had a place to say it.

The same shape produced the rest. `PublicationAgent`'s four refusals are
specified in detail; that deposit must route *through* it is nowhere, because
that is an edge. The API specification says the service describes itself at `/`;
the interface specification says the landing page is at `/`. Two documents, each
internally correct, describing incompatible edges that neither could see.

**And the Blueprint itself is built this way.** R1 to R12, C1 to C15: a list of
properties a conformant system must have. Not a graph. A conformance matrix can
be complete, with every row green and honestly earned, while the system does
nothing — because the matrix has rows for components and no rows for edges.

That is why the retrofitted checks did not generalise. Each was an edge-check
bolted onto a node-shaped document: reachability, orphan detection, CLI surface,
runner signatures. There was no enumeration of edges to generalise over.

The workflow graph is the exception and the demonstration. It is the one place
where edges became first-class objects — each carrying a condition, in a
sentence as well as in code — and it is the only mechanism here that detects its
own omissions **by construction** rather than because someone thought to look.
An agent absent from `workflow/definition.py` is an orphan the tests name; an
agent absent from a prose specification is invisible.

---

## 3b. Do the specifications need revising?

Partly, and the useful distinction is between **wrong** and **wrongly shaped**.

**Factually, they are in good repair.** The conformance matrix is checked by a
test that reconciles it against the source tree in both directions, so a row
naming a component that does not exist fails the suite. Statements overtaken by
later work were corrected as the work happened: §12b records the assembly audit,
the workflow section describes the graph, the diagram in it is generated from
the code rather than drawn. A sweep at handover found two stale phrases and
fixed them. There is no known false claim outstanding.

**Structurally, they have the defect described above**, and correcting that is a
larger piece of work than proof-reading.

What is missing from both the architecture document and the cluster
specifications is a section that enumerates **edges**: what calls what, in what
order, under which condition, and what must never precede what. That absence is
where six of the twelve defects came from. `workflow/definition.py` is now the
only place where that information exists in checkable form, and the architecture
document embeds a generated diagram of it — which is a start, but the diagram
covers the agent workflow only. It says nothing about the edge between the API
and the interface at `GET /`, or between deposit and the publication agent, or
between `check` and the capability report. Those are exactly the edges that
broke.

So the recommendation is not "review the documents for errors" but:

1. **Add an edges section** to the architecture document, covering the
   relationships the workflow graph does not: entry points to services, services
   to agents, commands to handlers. Prose is acceptable; enumerated is better;
   checkable is best.
2. **Leave the cluster documents alone.** They are a record of how each piece was
   specified and built, and rewriting them now would turn a contemporaneous
   account into a retrospective one. For a paper about how implementation
   diverges from specification, the contemporaneous version is the evidence.
3. **Do not re-verify the matrix by reading.** It is machine-checked; reading it
   adds confidence without adding information.

---

## 3c. The next phase, and the rule that governs it

`agent-interface-spec.md` (provided alongside this repository) specifies the
next piece of work: a common interface every agent satisfies, a registry, and
the workflow graph binding registry names rather than hand-written closures. It
exists because seven of eleven agents are driven by the graph and four —
ingestion, the plan reader, the declaration and publication — are still invoked
by name from `Pipeline.ingest`, the web route and `JobService`.

The safety rule in that specification is worth stating here too, because it
governs how anything is added afterwards.

**A job is born blocked, and only a person can unblock it.** `blocked` is
derived from the log — no confirmed declaration, or any unresolved gate item —
so nothing new is stored and there is no status field an agent could write to.
An agent may raise gate items, which restrains; an agent cannot resolve one or
confirm a declaration, because both are human acts at the nodal points the
architecture already defines. The monotonicity therefore needs no enforcement:
an agent can tighten and **structurally cannot loosen**, since the only
loosening transitions are ones no agent can perform.

Starting blocked is what closes omission. If the block were raised *by*
classification, a pipeline omitting classification would never raise it and
every later agent would run — a gate that is real and that nothing ever shut.
Born closed, omitting a step does not skip a check; it leaves the job as it was
created. **Absence of evidence stays un-clearance and never becomes clearance**,
which is this project's recurring failure prevented by construction rather than
detected after the fact.

Agents declare `inspects_material`. Ingestion and the declaration agent do not
inspect the researcher's data — one profiles container structure, the other
reads the researcher's own prose — so they may run on a blocked job. Everything
that reads the data may not. The rule then states something true rather than
procedural: *nothing looks at your data until you have told us what it is*,
which is §9.3 of the architecture expressed as a gate on execution rather than
as an ordering somebody must maintain.

Note that the declaration agent is model-based and still cannot unblock
anything. It proposes claims; a person confirms them. Model-based proposal,
human-only release.

## 4. Known gaps, in the order worth addressing

1. **ORCID production sign-in is unverified.** `tests/live/test_orcid_live.py`
   covers what can be checked without a person consenting in a browser. Note
   that production ORCID accepts only HTTPS redirect URIs, so a laptop needs a
   tunnel; local development accounts exist precisely to avoid needing this.
2. **The live chain.** Classification through validation has never run against a
   real model via the interface. This is the single most valuable next test.
3. **Retried model calls charge the exposure budget twice.** Known, unfixed.
4. **Audio inspection is unimplemented** — recorded as `NO_CAPABLE_BACKEND`
   rather than skipped.
5. **Discipline-specific metadata profiles** (R2 partial), **SHACL** (R4
   partial), **inter-instance synchronisation** (R6 partial, the Blueprint does
   not define the semantics).
6. **Four agents are still invoked by name** rather than driven by the graph:
   ingestion, the plan reader, the declaration and publication. The graph nodes
   for the first three exist and are empty, which is worse than an honest gap
   because the graph looks complete and the orphan test passes.
   `agent-interface-spec.md` addresses this; see §3b.

---

## 5. Things a reader of the code may misread

**`stated_sensitivity` and `inferred_sensitivity` are deliberately two fields.**
Collapsing them looks like simplification and would destroy the property that
live testing confirmed: four model families all correctly reported *no stated
level* for a statement describing personal data without labelling it. Inference
may only tighten, never relax.

**Capability is a filter, never a selector**, and the backend preference is
applied *after* it. An earlier version applied the preference first and could
eliminate the only model able to do a job.

**One place knows the working directory layout** — `runtime.unpacked_root()`.
The path was assembled in six places and the seventh spelled it differently,
which produced "no such file" on a file that was plainly there.

**The graph's conditions carry sentences as well as predicates.** This is not
decoration: an edge whose condition can only be read as code cannot be drawn,
explained to a researcher, or reviewed by anyone who does not read Python. The
Mermaid diagram in the architecture document is generated from the graph, so it
cannot describe a workflow the software does not have.

---

## 6. For the paper

Material that exists and is worth using:

- **Appendix B.5** of the architecture document: six conformance rows that were
  wrong at audit, including R8 recorded as implemented with three named plugins
  that were never written.
- **§12b**: the assembly audit and what it found.
- The **conformance report** (`datadirector conformance`) reconciles declared
  capabilities against the matrix in both directions and is checked by a test.
- The **live testing results** recorded through the development conversation:
  the Zenodo sandbox divergences, the injection fixtures, the cross-referential
  detection with disjoint halves, the OLS ground/search separation.
- `operational-tests/` — 9 manual scenarios, each stating what should happen
  and **what would count as a failure**.

The most defensible claim this work supports is not that the implementation is
conformant. It is this:

**A specification organised as a list of required properties directs attention
away from the relationships between them, and a system can satisfy every listed
property while doing nothing.** Conformance claims decay silently; the decay is
detectable only by mechanisms built specifically to detect it; and those
mechanisms do not generalise across boundaries, because a node-shaped document
offers no enumeration of edges to generalise over.

This codebase is the evidence. Half its defects were ordinary implementation
slips, found in seconds by using the software. The other half were unspecified
edges between correctly specified components, invisible to 600 passing tests and
to a conformance matrix in which every row was honestly earned.

The practical corollary, for anyone implementing a blueprint of this kind: build
the assembly as an artefact, specify the edges as objects that can be checked,
and treat "every requirement is implemented" as a statement about parts rather
than about a system.
