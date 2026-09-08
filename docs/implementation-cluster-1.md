# Implementation Specification — Cluster 1: The Spine

Companion to `data-director-architecture.md` (Document A). This document is
volatile: it describes module boundaries and responsibilities for one cluster
and is rewritten as implementation proceeds. Document A is stable; where the
two disagree, Document A is authoritative on intent and the contracts package
is authoritative on interface.

**Scope.** Everything the workflow rests on, with no agents and no user
interface. At the end of this cluster a job can be created, events appended and
verified, state reconstructed, provenance recorded, configuration loaded and
hashed, plugins discovered, and a model called through the policy gate. Nothing
user-visible works yet, and that is expected.

**Convention throughout.** Prose states responsibility, prohibitions and
invariants. Exact types live in `datadirector/contracts/`; this document never
restates a signature, because a restated signature drifts. Read the contract
module.

---

## Package layout

Two distributions (ADR-025). `datadirector_contracts` is the separately
packaged, Apache-2.0 interface layer that plugin authors depend on; everything
below is the EUPL-1.2 application and imports it.

```
datadirector_contracts/   existing, separate distribution, Apache 2.0
datadirector/             this cluster, EUPL 1.2
  errors.py           the exception hierarchy
  config/
    models.py         WiringConfig, ResolvedConfig
    loader.py         load, validate, resolve, hash
  state/
    store.py          EventStore: append-only, atomic, hash-chained
    projection.py     JobState reconstructed from events
  provenance/
    recorder.py       deterministic PROV-O recorder
    restricted.py     the restricted justification store
  policy/
    pep.py            the Policy Enforcement Point
  plugins/
    discovery.py      entry-point discovery and manifest validation
  credentials/
    broker.py         scoped credential injection
  backends/
    ollama.py         local ModelBackend
    anthropic.py      remote ModelBackend
    recording.py      test double: records and replays
  workflow/
    engine.py         step sequencing, resumption, halting
```

---

## 1. `errors.py`

**Responsibility.** One exception hierarchy so that failure modes are
distinguishable by type rather than by message text.

Root `DataDirectorError`. Subclasses: `ConfigurationError` (raised only at
startup), `ChainIntegrityError` (tamper evidence failure), `PolicyHalt`
(re-exported from contracts; no permitted backend), `AuthorityError` (an
unconfirmed assertion was acted on), `PluginError`, `CredentialError`,
`ExternalServiceError` (a plugin's remote dependency failed).

**Invariant.** `ExternalServiceError` is always recoverable: the workflow pauses
and resumes. `ChainIntegrityError` is never recoverable and must never be caught
broadly; it means the audit record is untrustworthy and the operator must be
told.

**Tests.** Every error carries a message naming what to do next, per C7's
graceful-failure clause. A test asserts no error class has an empty docstring.

---

## 2. `config/models.py`

**Responsibility.** The wiring configuration type, and the resolved
configuration that is hashed into provenance.

`WiringConfig`: declared model backends, active plugins, storage paths,
credential references (names of environment variables, never values), watched
folder path, deployment profile.

`ResolvedConfig`: the fully materialised startup state. Every plugin with its
version and capability manifest, every backend with its residency, every policy
value, the deployment profile. This is the object hashed at job start
(Document A §11.1), which is how a published dataset is tied to the exact
software configuration that produced its metadata.

**Must not.** Hold any secret value. The type has no field capable of carrying
one; credential references are variable *names*.

**Invariant.** `ResolvedConfig` is frozen and serialises to canonical JSON, or
the same configuration would hash differently on two machines and the
reproducibility claim fails.

**Tests.** Two structurally identical resolved configurations hash equally.
A config containing something shaped like a secret is rejected.

---

## 3. `config/loader.py`

**Responsibility.** Load the two YAML surfaces (wiring, policy), validate both,
resolve plugin references against what is actually installed, and produce a
`ResolvedConfig`.

**Must not.** Continue past an invalid configuration. Blueprint C7 requires
clear failure; a configuration error surfacing three steps into a workflow is
the failure mode this module exists to prevent.

**Key behaviour.** Emits a **startup capability report**: which Blueprint
requirements the current configuration can satisfy, derived from installed
plugin manifests (Document A §6.3). A deployment lacking a `DMPSource` says so
rather than silently omitting R8. This report is also the machine-generated
portion of the conformance matrix.

**Invariants.**
- Policy naming a backend that wiring does not declare is a startup failure.
- A sensitivity class with an empty permitted-backend list is a startup failure,
  not a runtime halt: silently unusable policy is worse than absent policy.
- The loader never reads a credential value; it verifies only that the named
  environment variable is *set*.

**Tests.** Missing variable produces a message naming the variable. Policy
referencing an undeclared backend fails at load. Capability report lists R8 as
unavailable when no `DMPSource` is installed.

---

## 4. `state/store.py`

**Responsibility.** The append-only event log. The most carefully specified
module in this cluster, because every other guarantee rests on it.

**Layout.** One directory per job under the configured state root, files named
`{sequence:05d}-{unix_timestamp}.json`.

**Operations.** `append` (one event, returns the written event with its
`prev_digest` populated), `load` (all events for a job, ordered), `verify`
(runs `contracts.verify_chain`), `latest_sequence`, `list_jobs`.

**Must not.** Expose any delete, truncate or update operation. The absence is
the contract. Rollback is a `state.compensated` event appended through the
normal path (ADR-008).

**Invariants.**
- Writes are atomic: serialise to a temporary file in the same directory, fsync,
  then `os.replace`. A crash mid-write must never leave a partial record.
- `append` reads the current tail to compute `prev_digest`. Two concurrent
  appends to one job must not both succeed: take an exclusive lock on a per-job
  lock file. Losing the race raises rather than overwriting.
- `load` verifies the chain and raises `ChainIntegrityError` on any break.
  Callers cannot opt out; there is no unchecked read.
- Timestamps in filenames are informational. Ordering is by sequence only.

**Tests.** Crash simulation (write a partial temp file, then load) yields a
valid log. Concurrent append from two processes: one succeeds, one raises.
Hand-edited event file causes `load` to raise. A 10,000-event job loads within a
stated time bound, so the file-backed choice is measured rather than assumed.

---

## 5. `state/projection.py`

**Responsibility.** Reconstruct current `JobState` by folding events. This is
the read model; the log is the write model.

`JobState` carries: current step, classification, confirmed assertion sets,
registered material references, pending proposals, halt reason if halted.

**Must not.** Persist anything. Projections are derived and disposable. If a
projection is wrong, replaying fixes it; if projections were stored, they could
diverge from the log and there would be two truths.

**Key behaviour.** Compensation events are applied by replaying the log up to
`restores_state_at_sequence`, then continuing past the compensation. This is
what makes rollback (C8) coexist with immutability (C1, P5).

**Invariants.**
- Folding is pure: same events, same state, always.
- An unknown event kind raises rather than being skipped. Silently ignoring
  events a newer writer produced would let an old reader report a confidently
  wrong state.

**Tests.** Property test: fold(events) equals fold(events) across process
restarts. A compensation restores the exact prior state. Replay to any sequence
reproduces the state at that point, which is the resumability claim.

---

## 6. `provenance/recorder.py`

**Responsibility.** Deterministic middleware converting actions into PROV-O
activities. No model anywhere in this module (ADR-009).

**Must not.** Accept an activity with no responsible agent; contain payload;
accept free-text justification (it takes a digest and delegates the text to
`restricted.py`).

**Key behaviour.** Wraps every agent action and every plugin call. Writes the
activity, and appends the corresponding event through `state/store.py`, so the
provenance graph and the event log are two projections of one sequence and
cannot disagree.

`export(up_to=Visibility)` emits named subgraphs at or below a clearance as
PROV-O in JSON-LD.

**Invariants.**
- Default visibility is `RESTRICTED` (P3, P5).
- Export at `OPEN` contains no `justification_digest` dereferences and remains a
  valid, internally consistent graph.
- Recording is synchronous with the action. An action that succeeded but was not
  recorded is a defect, not an acceptable degradation.

**Tests.** Round-trip a graph through JSON-LD and compare. Export at each
visibility level contains exactly the expected subgraphs. An activity carrying
payload-shaped content is rejected.

---

## 7. `provenance/restricted.py`

**Responsibility.** Store free-text justifications that must not enter the open
record, keyed by activity id, retrievable only by the auditor role.

**Key behaviour.** `put` returns a digest and stores the text. `get` requires a
role assertion. Presenting the text later and recomputing the digest proves it
is the original, which is sufficient under tamper evidence (ADR-007).

**Invariants.** The store is append-only like everything else. A digest recorded
in the chain must always resolve, so entries are never deleted; retention
applies to *access*, not existence.

**Tests.** A non-auditor role receives an error, not an empty result. Digest
recomputation matches after retrieval.

---

## 8. `policy/pep.py`

**Responsibility.** The single choke point for model access (Document A §9.4).

**Key behaviour.** `resolve_backend(classification)` returns the most
restrictive permitted backend for the current classification, or raises
`PolicyHalt`. Every resolution is recorded as a provenance activity: which
classification, which backend, which residency.

**Must not.** Offer any way to name a backend directly. Fall back to a
less-appropriate backend when the preferred one is unavailable — that is a halt,
not a degradation.

**Invariants.**
- `resolve_backend` is total over `SensitivityClass`: config validation
  guarantees a decision exists for every class, so there is no undefined case.
- The PEP holds the only references to backend credentials. Plugins never do.
- `ModelRequest` construction happens here, not in agents, which is why the type
  has no backend field.

**Tests.** `sensitive` with only a remote backend configured raises `PolicyHalt`
and the message names the missing residency requirement. Each resolution
produces exactly one provenance activity. A test asserts by introspection that
no public method accepts a backend name — the guarantee is structural, so the
test checks the structure.

---

## 9. `plugins/discovery.py`

**Responsibility.** Find plugins via `importlib.metadata` entry points,
instantiate them, validate their manifests, and register them by protocol.

**Must not.** Load a plugin whose declared residency is incompatible with local
policy. Import plugin modules before the manifest has been read, so far as the
packaging mechanism allows.

**Invariants.**
- A plugin failing manifest validation is refused with a message naming the
  plugin and the defect; the system starts without it rather than failing
  entirely, and the capability report records the absence.
- A plugin claiming to implement a protocol it does not satisfy is refused at
  registration, not at first call.
- Duplicate registration for the same protocol and name is an error.

**Tests.** A fixture plugin with a malformed manifest is refused and named. A
plugin declaring `extra-jurisdiction` residency is refused when policy forbids
it. Protocol conformance checked with `runtime_checkable` isinstance.

---

## 10. `credentials/broker.py`

**Responsibility.** Hold delegated credentials and inject them at call time.

**Key behaviour.** Plugins request by scope (`zenodo:deposit`), never by value.
The broker resolves from the environment, injects into the outbound call, and
returns a redacting wrapper.

**Must not.** Return a raw secret to caller code. Log a credential, or allow one
into an exception message, a provenance activity or an event payload.

**Invariants.**
- `repr` and `str` of any credential holder yield a redacted form. Tested, not
  assumed: this is the mechanism that stops a token reaching a traceback.
- A missing credential raises `CredentialError` naming the environment variable,
  never its value.

**Tests.** `repr` contains no secret material. Raising inside a credentialed
call produces a traceback free of the secret. Environment fixture only; no real
credential appears in the test suite.

---

## 11. `backends/`

**`ollama.py`.** Local `ModelBackend`, residency `on-premise`. Talks to
`/api/chat`. At startup queries `/api/tags` and fails with a clear message
listing installed models if the configured tag is absent, since a wrong tag
discovered mid-workflow is exactly the failure C7 forbids. Model tags are opaque
strings; the module never parses or validates their shape.

**`anthropic.py`.** Remote `ModelBackend`, residency `extra-jurisdiction`.
Credentials via the broker.

**A third slot.** The backend registry is keyed by `kind`, so an additional
provider is a new module plus a config entry, with no change to the PEP or to
any agent. Nothing in this cluster assumes exactly two backends.

**`recording.py`.** Test double that records real responses to fixtures and
replays them. This is what lets the whole suite run offline and lets the
paper's experiments be reproduced by a reviewer without credentials.

**Invariant across all backends.** `system`, `user_content` and
`trusted_instructions` are placed in structurally distinct positions in the
outbound request. They are never concatenated into one string. This is where
the shield is either kept or lost, and it is lost in exactly one line of careless
code.

**Tests.** A prompt containing injected instruction text in `user_content` never
appears in the instruction position of the outbound payload. Fixture replay is
byte-identical. Missing model tag fails at startup with the installed list.

---

## 12. `workflow/engine.py`

**Responsibility.** Sequence steps, halt, resume, compensate.

**Key behaviour.** A step is a named unit with a precondition on `JobState`.
The engine selects the next runnable step, executes it, and appends the
resulting events. Resumption is: load, fold, select, continue. There is no
in-memory session, which is what makes a job started Monday resumable Thursday
by a different person.

**Must not.** Hold workflow state in memory across calls. Retry a step that
produced a partial external effect without a compensating event first.

**Invariants.**
- Halting is a recorded event with a reason, never an exception escaping to a
  log file.
- `closed-not-shared` is a normal terminal state, not a failure. A DMP
  committing to no sharing must reach it cleanly.
- Every step execution produces at least one event and exactly one decision
  record, including deterministic steps.

**Tests.** Kill the process mid-workflow and resume: state is identical and no
step runs twice. A step raising `ExternalServiceError` halts and resumes cleanly.
A workflow reaching `closed-not-shared` reports success.

---

## Build order

1. `errors`, `config/models` — no dependencies.
2. `state/store` — the foundation. Do not proceed until crash and concurrency
   tests pass.
3. `state/projection` — needs the store.
4. `provenance/restricted`, then `provenance/recorder` — needs the store.
5. `config/loader`, `plugins/discovery` — needs manifests and models.
6. `credentials/broker`.
7. `backends/recording`, then `ollama`, then `anthropic`.
8. `policy/pep` — needs backends, config and the recorder.
9. `workflow/engine` — needs everything above.

Steps 2 and 8 are the two where a defect is expensive later: the store because
every guarantee rests on it, the PEP because a bypass there silently voids the
confidentiality architecture. Both warrant slower work than their size suggests.

---

## Definition of done

- All contract invariant tests still green.
- Per-module tests above passing.
- One end-to-end test: create a job, append twelve events including a
  compensation, kill and restart the process, resume, verify the chain, export
  provenance at each visibility level, and call a model through the PEP under
  each sensitivity class including one that halts.
- `ResolvedConfig` hashing demonstrated stable across two processes.
- Startup capability report produced and matching the installed plugin set.

Not in this cluster: any agent, any repository driver, any user interface, the
core API resource model, or container extraction. The last of these belongs with
ingestion in cluster 2, but note that its safety rules (§8.4, §14 of Document A)
are refusals rather than best-effort checks, and the `ExtractionLimits` defaults
in the contracts package are already set to refuse symlinks, absolute paths and
nesting. The last of these stays deferred until the Zenodo
driver exists, for the reason recorded in Document A §13.

---

## Open question carried into cluster 2

The file-backed store is right for the single-user and small institutional
profiles. The 10,000-event load benchmark above exists to tell us where it stops
being right, so that the C11 scalability row in the conformance matrix rests on a
measurement rather than an assertion. If the number is poor, the fix is an index
file rather than a database, and that decision belongs in cluster 2 once we have
the figure.
