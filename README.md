# Data Director

An implementation of the Research Data Alliance
[Data Director Agentic AI Blueprint](https://www.rd-alliance.org/) v1.0
(August 2026): a system that assists researchers in preparing, describing,
validating and depositing research data, with provenance and human oversight
throughout.

At the time of writing no other implementation of the Blueprint exists.

## Repository layout

```
config/
  wiring.example.yaml             Administrator surface: backends, plugins, paths
  policy.example.yaml             Steward surface: sensitivity -> permitted backends
docs/
  architecture.md                 Document A: the architecture (stable)
  implementation-cluster-1.md     Cluster 1 spec: the spine
  implementation-cluster-2.md     Cluster 2 spec: the front door (retrospective)
  implementation-cluster-3.md     Cluster 3 spec: reading the data (not built)
packages/
  datadirector-contracts/         Interfaces and types. Apache 2.0.
    src/datadirector_contracts/
      primitives.py               Identifiers, digests, references, residency
      sensitivity.py              Classification lattice; tightening asymmetry
      assertions.py               Shared envelope for document-derived claims
      payloads.py                 Declaration, DMP commitment, instruction
      relations.py                Related resources; how a relation is arrived at
      containers.py               Archives, safe extraction, self-describing formats
      events.py                   Append-only log, hash chain, human acts
      decisions.py                Decision records, field provenance
      provenance.py               PROV-O, reason codes, visibility partitions
      policy.py                   Policy config, PEP contract
      plugins.py                  The nine extension protocols
  datadirector/                   The application. EUPL 1.2.
    src/datadirector/
      errors.py                   Exception hierarchy
      config/                     models.py, loader.py
      state/                      store.py (event log), projection.py (fold)
      provenance/                 recorder.py, restricted.py
      policy/                     pep.py
      plugins/                    discovery.py
      credentials/                broker.py
      backends/                   base.py, ollama.py, anthropic.py, recording.py
      workflow/                   engine.py
      containers/                 safety.py, archive.py, selfdescribing.py, detect.py
      profiling/                  structural.py
      watch/                      folder.py
      agents/                     base.py, ingestion.py, declaration.py,
                                  classification.py, media.py, redaction.py
      exposure/                   ledger.py
      probing/                    executor.py
      cli.py                      Minimal CLI (scaffolding, not the UI)
tests/
  conftest.py                     Shared identities as fixtures
  test_invariants.py              Architecture safety properties (contracts)
  test_cluster1.py                Spine behaviour
  test_end_to_end.py              Cluster-1 definition of done
  test_cluster2.py                Extraction attacks, profiling, agents
```

## Two packages, two licences

`datadirector-contracts` is **Apache 2.0**. `datadirector` is **EUPL 1.2**.

The application is reciprocally licensed because it is public-sector research
software and reciprocity is appropriate for it. The contracts are permissively
licensed because the architecture depends on third parties writing plugins, and
a plugin must import the contracts. Under a reciprocal licence that import could
oblige a plugin author to release under EUPL or a compatible licence, which
would deter exactly the contributions the design is built around.

Incorporating Apache 2.0 code into an EUPL work is permitted; the reverse is
not, so the split works in this direction only. Recorded as ADR-025.

This is a deviation from Blueprint principle P7, which asks for a permissive
licence such as MIT or Apache 2.0. P7's stated rationale is community
contribution, independent audit and reuse without access barriers; the split
preserves all three at the point where they operate, which is the plugin
boundary. The deviation is documented rather than glossed, and appears as such
in the conformance matrix.

## Status

Stage 2 complete: architecture, executable contracts, threat model, conformance
matrix covering all 43 normative Blueprint items.

Cluster 1 (the spine) implemented: append-only event store with hash chain,
state projection with compensation, deterministic provenance recorder with
visibility partitions, restricted justification store, credential broker,
plugin discovery, policy enforcement point, model backends (Ollama, Anthropic,
replay), configuration loader with startup capability report, workflow engine
with resumption and rollback. 51 tests.

Cluster 2 (the front door) implemented: guarded container extraction (zip, tar,
BagIt, RO-Crate) refusing path traversal, absolute paths, symlinks, device
entries, decompression bombs and member-count exhaustion; structural profiling
that reports column names, inferred types and character-class shapes but never
values; watched-folder detection with write-stability checking; ingestion agent;
declaration agent producing proposed claim sets that carry no authority until a
human confirms them, reporting stated and inferred sensitivity separately with
inference permitted only to tighten (ADR-027). 79 tests.

A minimal CLI (`datadirector check | ingest | status | verify | provenance`)
exists as scaffolding so the system can be exercised by hand. It is not the
researcher-facing interface.

Cluster 3 in progress. Part A (capability routing) implemented: model backends
declare capabilities, and the policy enforcement point resolves by policy first,
capability second, residency third. Part B (exposure accounting) implemented: an
append-only ledger of every release of payload to a model, with per-job and
per-artefact budgets, and whole-artefact releases requiring named human
authorisation. 99 tests.

Part C (classification) implemented: a closed six-verb probe vocabulary whose
arguments must name something already in job state, a probe executor that
charges the exposure ledger, and a three-phase classification agent that shows a
model the structural profile, runs the probes it requests, and interprets the
results. 121 tests.

Not yet built: chunked content inspection, media inspection, redaction
proposals, metadata and documentation agents, repository drivers, the web
interface, the core API resource model.

## What is and is not tested without a model

One module of sixteen calls a model: `agents/declaration.py`. Everything else —
event store, projection, provenance, policy enforcement, configuration,
credentials, plugin discovery, container extraction, structural profiling,
watched folder, ingestion, workflow engine, CLI — is deterministic, and the 74
offline tests exercise it directly.

The declaration agent is tested offline against a scripted backend. That
establishes the agent's parsing, the authority state machine, the routing to the
most restrictive backend, and that ingested document text reaches the model only
as user content and never as instruction. It does not establish that any
particular model returns usable output, nor that the Ollama and Anthropic
request payloads are correctly shaped: no offline test executes either HTTP
backend.

`tests/test_static.py` runs pyflakes over the source packages, the scripts and
the live suite. It exists because live-only code is expensive to execute and
cheap to break: three undefined names reached a live run during development, and
none was reachable by any test. Static analysis is not a substitute for running
the code, but it is the cheapest guard on code that is costly to run.

`tests/live/` covers what only a real model can settle. It is skipped unless
`DD_LIVE_TESTS=1`:

```bash
DD_LIVE_TESTS=1 DD_OLLAMA_MODEL=qwen3.8:27b-mlx pytest tests/live -q -s

# sweep every installed model, 20 repeats each
python3 scripts/measure.py --repeats 20
python3 scripts/summarise_measurements.py
```

`DD_OLLAMA_MODEL` is required and has no default, so every measurement is
attributable to a named model. Each run appends to
`tests/results/measurements.jsonl`, and recorded model responses are filed per
model under `tests/fixtures/model/<tag>/`. Neither is git-ignored: they are the
evidence behind the figures.

Large models exceed the 120-second default; set `timeout_seconds` for the
backend in the wiring configuration, or `DD_OLLAMA_TIMEOUT` for the live suite,
which defaults to 600 seconds.

Injection is measured against three fixtures which test three different things,
and the summariser explains which figure each one licenses. Note in particular
that a directive announcing itself as a directive is refused by models that
comply with the same demand phrased as a routine processing note, so the obvious
fixture is a false-negative generator.

Note what the injection test asserts and what it only measures. That an injected
document yields a proposal carrying no authority is **asserted**, because it must
hold even when the model is completely steered. Whether the model is steered is
**measured and reported**, not asserted: the architecture does not assume the
model resists injection, but how often it can be steered is an empirical number
worth publishing rather than assuming.

Successful live responses are written to `tests/fixtures/model/` by the recording
backend, so a reviewer without a model or credentials can replay them.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e packages/datadirector-contracts
pip install -e "packages/datadirector[dev]"     # includes pillow and pyflakes
cp .env.example .env        # then fill in; .env is git-ignored
pytest -q
```

Pillow is needed for the image tests and for the deterministic structure check
that contradicts a vision model reporting an image as empty. Without it the
image tests skip and the check reports that it could not be made; it never
degrades silently. Re-run the install after pulling if the image tests skip
unexpectedly.

## Credentials

No credential value appears in any configuration file, event payload or
provenance record. The application reads secrets only from the environment, and
plugins request them by scope from a credential broker rather than receiving
them as values. `.env` is git-ignored from the first commit.
