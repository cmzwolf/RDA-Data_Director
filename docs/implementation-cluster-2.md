# Implementation Specification — Cluster 2: The Front Door

**Written retrospectively.** Cluster 2 was implemented directly from the design
agreed in discussion, without a spec preceding the code. This document records
what was built and why, so the documentation trail is complete. Where it
describes a decision taken during implementation rather than before it, that is
noted.

**Scope.** Everything between material arriving and a confirmed declaration:
watched folder, container extraction, structural profiling, the ingestion agent,
the declaration agent. First cluster in which a model is called.

---

## 1. `containers/safety.py`

**Responsibility.** The refusal rules for archive extraction. No model, because
these threats are defeated by rules and judgement is the wrong instrument.

**Refuses:** parent references in member names, absolute paths, null bytes,
symlinks and hardlinks, device and FIFO entries, member counts and total
uncompressed size beyond configured limits, and any member whose *resolved* path
escapes the extraction root.

**Invariants.**
- Escape is checked on the resolved path, not the declared one: a path component
  may itself be a symlink placed by an earlier member of the same archive.
- Budget is checked *before* each write. A decompression bomb detected after
  writing has already consumed the disk the check was protecting.
- Nothing is sanitised or renamed into safety. A member whose name had to be
  repaired is one whose provenance can no longer be described honestly.

**Tests.** Genuinely malicious archives are constructed, not mocked: `../../`
traversal, absolute member path, tar symlink, tar device entry, 200 KB expansion
against a 1 KB budget, member-count exhaustion.

## 2. `containers/archive.py`, `selfdescribing.py`, `detect.py`

**Responsibility.** Zip and tar extraction over the shared safety rules;
BagIt and RO-Crate as self-describing formats; detection across all four.

**Key behaviour.** Detection tries self-describing formats first, because a
BagIt archive is also a valid zip and treating it as one discards the manifest
and checksums the depositor supplied. BagIt checksums are verified against ours
and a mismatch is **reported, never repaired**: it means the archive is not what
its manifest says.

**Invariant.** Both the archive digest and the member digests are retained. The
first is what we can prove was submitted, the second what we can prove was
deposited. Members are deposited expanded, so this lineage lives in the
provenance chain and not in a related-identifier field: the members carry no
separate identifier (ADR-026).

## 3. `profiling/structural.py`

**Responsibility.** Describe material without disclosing it (§9.1).

**Key behaviour.** Tabular files yield column names, inferred types, null counts,
distinct counts and a **character-class shape** per column: `AB-1234` becomes
`AA-NNNN`. A model can recognise a date or an identifier format without a single
real value crossing the boundary. Non-tabular files yield size, media type and
encoding only.

**Invariant.** No value appears in the profile. Tested by profiling a table of
invented patient records and asserting that none of the values appear anywhere
in the serialised output.

**Decided during implementation.** The shape reduction was not in the agreed
design; it emerged from asking what a model needs to distinguish a date column
from an identifier column, given that it may not see either.

## 4. `watch/folder.py`

**Responsibility.** Detect submissions in the watched folder.

**Key behaviour.** A file appearing is not a file that has finished being
written. Stability is established by comparing two signatures (total size,
newest mtime, file count) separated by a quiet interval. A directory is one
candidate, not many.

**Invariant.** A candidate is claimed once, so polling does not re-ingest it.

## 5. `agents/base.py`, `agents/ingestion.py`

**Responsibility.** Register material, unpack containers under guard, profile the
result, emit `material.registered`.

**Must not.** Read content. Call a model. Take any further step until a
declaration is present and confirmed. Ingestion runs before any classification
exists, so it must not be *capable* of exposing material.

**Invariant.** All three entry paths (watched folder, upload, API) converge on
the same event, so nothing downstream depends on how material arrived.

**Tests.** A test asserts the ingestion decision record names no model and
carries no input digest.

## 6. `agents/declaration.py`

**Responsibility.** Parse the responsibility and compliance statement into a
claim set for human confirmation.

**Must not.** Produce anything other than a `PROPOSED` assertion set. Emit a
default claim when the model reply is unparseable — a fabricated "public" here
is the exact privilege escalation the design forbids.

**Key behaviour.**
- Parses at the most restrictive classification, because before confirmation the
  sensitivity is unknown and unknown resolves to most-restrictive.
- Reports `stated_sensitivity` and `inferred_sensitivity` separately; an
  inference may only tighten (ADR-027).
- Confirmation with no level available assumes the most restrictive class: an
  absent statement is not a statement of openness.

**Added after live testing.** The two-field split did not exist in the original
implementation. Four model families, tested independently, all correctly
reported no level for a statement describing dates of birth and home addresses
that never used the word "sensitive". The behaviour was right and the
specification was wrong. See ADR-027.

## 7. `cli.py`

Scaffolding, not the researcher-facing interface: `check`, `ingest`, `status`,
`verify`, `provenance`. The approval surface of ADR-019 belongs to cluster 5.

---

## Live testing

`tests/live/` exercises the one module that calls a model, skipped unless
`DD_LIVE_TESTS=1`. It exists because the offline suite establishes our prompt
construction and authority handling but cannot establish that any model returns
usable output.

Three fixture-design errors were found by running models, none by reading code:
one fixture serving two purposes, obedience conflated with transcription, and a
control claim embedded inside the attack it was controlling for. Each inverted
or dissolved the previous conclusion. This is recorded because the paper's claim
is partly about what implementers can verify, and it argues that specifications
of this kind cannot be validated by inspection.

Findings are recorded in §14.2 of the architecture document.
