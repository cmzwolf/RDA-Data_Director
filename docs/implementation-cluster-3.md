# Implementation Specification — Cluster 3: Reading the Data

**Scope.** The first cluster in which a model reads the **data itself** rather
than a statement about it. Classification with model-directed probing, chunked
content inspection, media inspection, redaction proposals, and the capability
routing all of that requires.

**Boundary.** Ends with a job whose material is classified, whose uninspectable
items are marked, and whose redaction proposals await per-item human decision.
Review happens through the CLI; the proper surface is cluster 5.

---

## Part A — Capability routing

### A1. Contract additions

`ModelBackend.capabilities()` returns a set from a fixed vocabulary:
`text-generation`, `vision`, `audio`, `long-context`, `structured-output`.
Declared in the manifest, so it is auditable rather than inferred.

`PEP.resolve_backend(classification, capability)` gains a second argument.

**The ordering is load-bearing.** Policy filters first, capability narrows
within the permitted set, residency orders what remains. Capability is a
*filter*, never a selector, or the property that a bypass is inexpressible is
lost.

**Must not.** Route by cost, speed or quality. A deployment that quietly sends
classification to a faster model has changed its threat posture without anyone
deciding to, and live testing showed susceptibility varies by model. If such
routing is ever wanted it must be an explicit policy statement, not a
performance setting.

**Halt message** names both the classification and the missing capability:
*"no backend permitted at `sensitive` provides `vision`"* is actionable in a way
the current generic refusal is not.

**Tests.** A vision request at `sensitive` with only a text backend permitted
raises `PolicyHalt` naming the capability. A capable backend that policy forbids
is not selected. Introspection test extended: still no public method accepts a
backend name.

### A2. Job-level model ledger

The set of models that touched a job's material is recorded at job level, so a
reviewer asking *"what saw this data?"* gets one answer rather than reassembling
it from decision records.

---

## Part B — Exposure accounting

### B1. `exposure/ledger.py`

**Responsibility.** Record every release of payload to a model, and enforce a
budget.

Each exposure records: artefact reference, what was released (field name, byte
count, chunk index), the classification in force, the backend and its residency,
and the authorising human where release required one.

**Invariants.**
- Only a description and digest are retained, never the released content
  (class 3, ADR-005).
- The budget is per job and per artefact. A model steered by injected content
  should be able to exhaust its budget, not drain the dataset.
- Exhaustion halts with a recorded reason; it does not silently truncate.

---

## Part C — Classification

### C1. `probing/vocabulary.py`

**Responsibility.** The fixed set of probes a model may request.

**Ownership: the system, and only the system.** The vocabulary is compiled into
the contracts package. It is not configuration and it is not user input.

The reasoning is worth recording, because each alternative owner has a distinct
failure:

| Owner | Failure |
|---|---|
| Researcher | The depositor's channel *is* the injected document. A directive adding `read_file(path)` to the vocabulary makes exfiltration trivial. |
| Deployment administrator | Safer, but the administrator has no workflow authority by design (§10). Widening what a model may see is that separation collapsing, and a probe added once for convenience is a permanent widening nobody revisits. |
| System | Fixed, reviewable in one file, changed only by a code change with a test and a version bump — which lands in the provenance chain through the resolved-configuration digest. |

The general rule this instantiates: a probe vocabulary is a **capability
boundary**, and capability boundaries are never safely configurable outward.
Same reason `NoBackendAction` admits exactly one value.

**Configuration gets the dial, not the vocabulary.** Deployments tune maximum
sample sizes, per-job and per-artefact byte budgets, and may *disable* individual
probes. Narrowing is always available; widening is not expressible.

**Adding a probe is a specification change.** It requires an ADR recording what
the probe reveals and why the existing set is insufficient.

**The vocabulary.**

| Probe | Returns |
|---|---|
| `sample_field(field, n)` | Up to *n* values from one profiled column |
| `distinct_count(field)` | Cardinality only |
| `value_shapes(field)` | Character-class shapes, no values |
| `null_pattern(field)` | Null distribution |
| `cross_tab(field_a, field_b)` | Co-occurrence counts, no values |
| `read_chunk(artefact, index)` | One chunk of a registered artefact |

**Every argument names something already in job state.** Field names come from
the structural profile; artefacts come from registered material. `read_chunk`
takes an **artefact reference, never a path**: a path argument invites traversal
and a chunk of an arbitrary file is close to unbounded read. This is the same
discipline as container extraction — resolve against a known set rather than
trusting a supplied string.

**Must not.** Accept arbitrary code, regular expressions, free-form queries,
paths, or any identifier not already present in job state. Bounding the verbs
without bounding the arguments would leave the injection route open through the
arguments, which is the residual hole this rule closes.

**Invariants.**
- An argument naming something absent from job state is **refused, not silently
  empty**: silence is indistinguishable from a genuine empty result.
- Every probe result is an exposure and is recorded in the ledger (B1).
- Sample sizes are capped by configuration; a request above the cap is clamped
  and the clamping recorded, not refused, since a truncated answer is still a
  useful answer.

**Tests.** A probe naming an unprofiled field is refused. `read_chunk` with a
path-like string is refused. A disabled probe is refused with a message naming
the policy. Probe results appear in the ledger with byte counts. A probe
requesting more than the cap is clamped and the clamp recorded.

### C2. `agents/classification.py`

**Responsibility.** Establish the sensitivity of the material, in three phases.

1. **Deterministic profile** (cluster 2) — no model.
2. **Probe proposal** — the model reads the profile and requests probes from the
   fixed vocabulary. It sees no values yet.
3. **Interpretation** — probes execute programmatically, results return, the
   model forms a classification with indicators.

**Invariants.**
- Runs at the most restrictive backend the confirmed declaration permits.
- May tighten the classification autonomously; may never relax it. Relaxation
  requires an identified human (§9.3).
- A classification contradicting the confirmed declaration halts and returns the
  declaration for human reconsideration.
- Every phase emits a decision record, including the deterministic one.

**Tests.** A probe naming a nonexistent field is refused. Probe results count
against the budget. A scan proposing a lower level than the declaration does not
lower it. Contradiction produces `classification.contradicted`, not a silent
tighten.

### C3. `content/chunking.py`

**Responsibility.** Inspect documents too long for one context window.

**Key behaviour.** Overlapping chunks, with an **accumulating indicator set**
carried forward, and a final correlation pass over the accumulated indicators
rather than over the text.

**Why.** The disclosure that motivates this whole design is cross-referential:
*"the only midwife"* in one passage and *"a village of four hundred"* in
another. Neither chunk is sensitive alone. Chunk-local analysis finds nothing,
which would be the worst possible failure — confident silence.

**Invariants.**
- Sensitivity is a property of the document, not of a chunk: any chunk sensitive
  makes the document sensitive, and indicators are unioned.
- The correlation pass sees indicators, not raw text, so it costs one small call
  rather than a second full read.
- Chunk boundaries and overlap are recorded, so a finding can be located.

---

## Part D — Media

### D1. `media/metadata.py`

**Responsibility.** Deterministic extraction of embedded metadata. No model.

EXIF including GPS, DICOM patient tags, PDF author and producer, audio ID3,
instrument headers, document revision history.

**Why first.** Cheap, high yield, and frequently where the actual leak is: a
photograph of a field site carries the coordinates of the field site.

### D2. `agents/media.py`

**Responsibility.** Classify non-textual material across three tiers.

| Tier | Method | Availability |
|---|---|---|
| Metadata | deterministic | always |
| Content | multimodal model | only if a permitted backend declares the capability |
| Uninspectable | none | proprietary or instrument formats |

**The invariant that matters.** *Not inspected* is a recorded, surfaced state,
never silence. A reviewer seeing no flags on a file will reasonably infer it was
checked and found clean; that inference must be prevented, because it is the one
that would actually harm someone.

**Presumption.** An uninspected image or audio file is treated as **sensitive**,
not unknown: a face is personal data and a voice is biometric, independent of
content. Only an explicit human act relaxes it — the §9.3 asymmetry again, so
no new rule.

**Uninspected reasons** are coded, not free text: `no-capable-backend`,
`capability-not-permitted-at-classification`, `format-unreadable`, `encrypted`,
`exceeds-size-limit`.

**Tests.** An image at `sensitive` with no permitted vision backend yields
`uninspected(capability-not-permitted-at-classification)` and a sensitive
presumption, and the workflow continues. EXIF GPS is found without any model.

---

## Part E — Gate items and redaction

### E1. `gate/items.py`

**Responsibility.** Collect everything awaiting a human decision into one
reviewable set: uninspected files, redaction proposals, declaration
discrepancies.

**Key behaviour.** The **workflow proceeds; the gate blocks.** Every file is
profiled and every proposal generated, then the human reviews them together with
the declaration and DMP in view. Deposit cannot commit while any item is
unresolved.

**Why not halt per file.** Reviewing *"can we publish this `.fits`?"* in
isolation is a worse decision than reviewing it alongside everything else.

**Invariants.** Per-item decisions, no bulk accept. Each decision is a human act
bound to an ORCID with a reason code. Unresolved items block deposit.

**Decisions available on an uninspected file:** `inspected-externally`,
`publish-as-is`, `exclude-from-deposit`, `classify-sensitive`.

### E2. `agents/redaction.py`

**Responsibility.** Produce redaction **proposals**, never applied redactions
(ADR-013).

Each entry: location, reason code from the controlled vocabulary, evidence, and
a proposed treatment (`suppress`, `generalise`, `pseudonymise`, `coarsen`).

**Must not.** Apply anything without per-item approval. Overwrite the original —
approved items produce a new working artefact.

**Invariants.** Recorded via the confidential-provenance mechanism (§7.5): reason
codes in the open graph, justification digests, content in the restricted store.
The system never decides what is sensitive; it decides what to show a human, who
decides.

---

## Build order

1. Capability routing (A) — everything else depends on it.
2. Exposure ledger (B) — must exist before the first probe.
3. Probe vocabulary, then classification agent (C1, C2).
4. Chunking (C3).
5. Media metadata, then media agent (D).
6. Gate items (E1), then redaction proposer (E2).

A and B first is not negotiable: a probe that runs before the ledger exists is an
unaccounted exposure, and there is no way to reconstruct it afterwards.

---

## Definition of done

- All existing tests green; the offline suite still runs without a model.
- Capability halt names the missing capability.
- A probe naming an unknown field is refused; a probe naming a path rather than a
  registered artefact is refused; probe results appear in the ledger.
- A cross-referential disclosure split across two chunks is detected by the
  correlation pass and missed by chunk-local analysis. **This test is the one
  that justifies the cluster**; if it cannot be made to pass, the chunking design
  is wrong.

  Two traps in constructing it, both encountered. The fixture must actually span
  chunks, so the test asserts on the section count rather than trusting the
  document to be long enough. And neither half may be identifying on its own: a
  first version described the informant as attending nearly every birth in the
  district, which is identifying alone and would have let the test pass without
  any correlation occurring. `chunk_local_sensitive` is recorded for exactly
  this reason — a sensitive verdict reached because one section was
  independently sensitive has demonstrated nothing.
- An uninspectable file reaches the gate as an item, with a coded reason and a
  sensitive presumption, and blocks deposit until decided.
- A redaction proposal requires per-item approval and writes to a new artefact.

## Live tests to add

- Probe quality: does a model request useful probes from a profile alone?
- Cross-referential detection: the midwife case, split across chunks.
- Media: does a multimodal backend identify faces where one is permitted?
- An injection attacking the **inference** rather than the stated field. This is
  the clause the architecture actually depends on, and no fixture yet targets it.
