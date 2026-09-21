# Implementation Specification — Cluster 4: Deposit

**Scope.** Metadata generation, validation, repository selection and actual
deposit to Zenodo, with identity and delegated credentials. The first cluster
that touches an external system which can refuse us.

**Boundary.** Ends with a file in the watched folder becoming a DOI in the
Zenodo sandbox, with provenance throughout and every gate item resolved by a
named human before anything is committed.

**Why this cluster will change the design.** Everything so far has been ours to
define. Zenodo has its own metadata requirements, its own versioning semantics,
its own idea of what a valid deposit is, and its own failure modes. The core API
resource model has been deferred since Document A precisely so it could be
written after this contact rather than before it.

---

## Part A — The canonical metadata record

### A1. The canonical record: `datadirector_contracts/record.py`

**Responsibility.** One internal representation, projected to target schemas at
binding time. It lives in the contracts package rather than the application
because the plugin protocol that projects it - `SchemaProfile`, whose `project()`
takes the canonical record - is the interface plugin authors compile against, and
that interface cannot depend on the EUPL application (ADR-025).

The alternative — generating DataCite directly — collapses on the second
repository, and R6 requires the same dataset be expressible in several standards.
Late binding also makes the crosswalk visible to the user, which R6 asks for.

Fields: title, creators and contributors (ORCID, affiliation with ROR),
publication year, publisher, resource type, descriptions by kind, subjects with
vocabulary URIs, rights and licence, dates, related resources (§8.5), funding,
language, version, sizes and formats. `FieldProvenance` itself - origin,
contributor, CRediT role - is in `datadirector_contracts/decisions.py`, keyed by
field path on the record.

**Invariants.**
- Every field carries a `FieldProvenance` (cluster 1): origin, contributor,
  CRediT role. A record where nobody can say where a value came from fails C14.
- `ABSENT_BY_DESIGN` is a value. R2 requires the system to state openly when no
  controlled vocabulary exists for a domain; that statement is a field, not a
  sentence in generated prose.
- Frozen. Revision produces a new record, so the approval gate compares two
  states rather than trusting a mutation.

### A2. `schemas/datacite.py`, `schemas/rocrate.py`

`SchemaProfile` implementations. `project()` maps canonical to target and
`required_fields()` drives pre-flight, both declared on the protocol;
`missing_required()` reports what a specific record lacks. `relation_vocabulary()`
is not a protocol method - the two DataCite profiles carry it because the
permitted relation types are a property of that standard's version (ADR-026), and
RO-Crate has no equivalent to supply.

**Note.** Zenodo accepts a subset of DataCite and adds its own fields. The
profile declares what Zenodo actually takes, not what DataCite defines. We will
only learn the difference by trying.

---

## Part B — Generation and documentation

### B1. `agents/metadata.py`

Three-phase, mirroring classification: structural profile and confirmed claims
in, model proposes values, human approves.

**Must not.** Invent a value it cannot ground. An unknown creator affiliation is
absent, not guessed — a plausible wrong affiliation is worse than a blank,
because it will be believed.

**Invariants.** Vocabulary terms come from a `VocabularyProvider` and are
recorded with their URIs; a term the provider does not return is not used.
Every field's origin is recorded (A1). Runs at the backend the classification
permits.

### B2. `agents/documentation.py`

Drafts README and data dictionary (R5). Structural elements are inferable;
variable definitions, units, missing-value codes and collection procedures are
not, and are surfaced as gaps for the researcher rather than filled.

### B3. `agents/validation.py`

Deterministic. Runs `ValidatorDriver` implementations against the projected
record, plus the C15 consistency check (ADR-011): a generated value contradicting
its source record is a finding.

**Invariant.** Validation failure blocks deposit and is not a gate item a human
can wave through. A repository will reject the deposit anyway; failing here is
cheaper and more legible.

---

## Part C — Identity and delegation

### C1. `identity/orcid.py`

`IdentityProvider` over ORCID's OAuth 2.0 authorisation code flow. Identity
only. Affiliation in an ORCID record is self-asserted and cannot ground
authorisation (§10).

### C2. `credentials/oauth.py`

Authorisation code flow for repository tokens. Zenodo issues its own tokens with
`deposit:write` and `deposit:actions`; ORCID is not a deposit credential, and the
code says so in the scope it requests.

Two stores, because the two kinds of credential have different owners.
`credentials/broker.py` resolves the deployment's own scopes from the
environment, so a service credential belongs to the installation. `DelegatedTokenStore`
in this module holds what a *person* delegated, keyed by ORCID and repository name
in one file outside the state root, written through a temporary file and
`chmod 0600` before the rename. It is not the restricted provenance store (§7.5)
and not the configuration: a token is neither an audit record nor a setting, and
`forget()` exists because a person who revokes a delegation must be able to make
the installation forget it without editing a file by hand.

**Invariant.** Tokens never reach a plugin as values, never enter an event
payload or provenance record, and never appear in an exception message. The
`Secret` wrapper from cluster 1 already enforces the last, and the driver reveals
at the one point the header is built.

---

## Part D — The repository driver

### D1. `repositories/zenodo.py`

`RepositoryDriver` for Zenodo, plus `DepositionRegistry` in the same module: the
deposition id per job, persisted so a retry reuses it instead of creating a second
record. Sandbox and production differ only in base URL.

Protocol operations: `preflight`, `deposit`, `new_version_of`. The steps `deposit`
sequences are public too - `begin`, `upload`, `set_metadata`, `publish` - because
resumption has to be able to re-enter mid-sequence, and `probers()` supplies the
intent-reconciliation probes (§12b) that settle an attempt the process died in the
middle of.

**Invariants.**
- `preflight` returns problems; it never fixes them. A driver that silently
  adjusts metadata to satisfy a repository has changed what the human approved.
- Deposit is idempotent per job: a retry after a network failure must not create
  a second record. Zenodo's deposition id is recorded on first creation and
  reused.
- The concept DOI and the version DOI are both recorded (§7.1).
- Nothing is published until every gate item is resolved. That check is not the
  driver's: `PublicationAgent.check_ready` collects the unresolved items and the
  error-severity validation findings and refuses before a request is made, so the
  driver never has to trust a caller's promise.

### D2. `tests/fake_zenodo.py`

A local HTTP server implementing enough of the API to run the deposit path
offline: create, upload, update metadata, publish, new version, plus the failure
modes worth testing — 400 on invalid metadata, 401 on a bad token, 409 on
double publish, and a connection drop mid-upload.

**Why a fake rather than mocks.** The interesting failures are in HTTP status
handling and retry behaviour, and a mock asserts our assumptions rather than
testing them. The fake is also what lets a reviewer run the deposit path with no
credentials.

The fake is not a substitute for the live run: it encodes what we believe the
API does. Divergence between fake and live is a finding, and the live test
records it.

---

## Part E — The deposit agent

### E1. `agents/publication.py`

Sequences: check the gate, project the record, preflight, create, upload,
attach metadata, publish, record the PID, delete class-1 working material
(§7.3).

**Invariants.**
- Refuses to run while any gate item is unresolved, or while a validation finding
  of error severity stands.
- Refuses to run at all without an authenticated person: publishing requires an
  instruction whose author is the depositor and which carries an actor, and an
  invocation without one raises rather than the agent going hunting through the
  log for a name whose authority it would be borrowing.
- Refuses to publish over an unsettled attempt: if the log records an effect
  intended but not settled, the agent raises `ReconciliationRequired` and asks the
  repository what happened, because publishing over an interrupted attempt is how
  a dataset gets deposited twice.
- Excluded artefacts (`exclude-from-deposit`) are not uploaded, and a job whose
  every artefact was excluded ends in `NothingToDeposit` - a terminal success with
  a recorded selection basis, not an error.
- Working data is deleted only after a confirmed publish; a failed deposit
  leaves the job resumable.
- `closed-not-shared` is reachable and is a success.

---

## Build order

1. Canonical record and schema profiles (A) — everything downstream projects
   from it.
2. Fake Zenodo (D2) before the driver, so the driver is written against
   something that can refuse it.
3. Repository driver (D1).
4. Credentials and identity (C).
5. Metadata, documentation and validation agents (B).
6. Publication agent (E).

D2 before D1 is deliberate and mirrors B-before-C in cluster 3: a driver written
against a server that always says yes will handle failure badly, and deposit
failures are the ones that leave a repository in an inconsistent state.

---

## Definition of done

- All existing tests green.
- The full path runs offline against the fake: watched folder to published
  identifier, with the gate blocking until resolved.
- Retry after a simulated mid-upload drop produces one record, not two.
- Invalid metadata is refused at preflight with a message naming the field.
- A live run against the Zenodo sandbox produces a real DOI, and any divergence
  from the fake is recorded.

## Divergences found against the live API

Recorded as they are found, because the fake encodes belief and the live run is
what tests it. Each was invisible offline by construction.

**A deposition returns its files.** The fake omitted the `files` list that a real
deposition carries, which is how a resumed deposit knows what it has already
sent. Without it the driver re-uploaded. Found by the offline suite once the
fake was corrected; the live test now asserts the key exists, because the
driver's resume logic rests on it.

**Zenodo assigns the publisher.** DataCite requires `publisher`; Zenodo supplies
it and a depositor never does. The Zenodo profile inherited the requirement and
preflight therefore refused a record the repository would have accepted. A
profile stricter than its target is not safer, only obstructive.

**Drafts are not validated; publish is.** The real API accepts incomplete
metadata on a draft and refuses it at publish. The fake validated every write,
so it was stricter than the API and the driver's publish-time error handling was
never exercised. Our own preflight remains the guard that should catch this,
before a round trip rather than after one.

This one has a consequence beyond the fake. Zenodo will hold a draft carrying
unusable metadata indefinitely, so for most of a deposit's life our preflight is
not a convenience layer over the repository's validation — it is the only
validation there is. That makes `missing_required` and the C15 consistency
checks load-bearing rather than defensive.

**A draft omits `doi` and `conceptdoi` entirely.** The fake returned them as
null. A caller testing key presence rather than value would have read a draft as
a published record. Real depositions also carry `created`, `modified`, `owner`,
`record_id` and `title`, and their `links` include `edit`, `discard`, `files`,
`badge`, `latest_draft` and `latest_draft_html`.

**A published deposition refuses metadata writes.** Zenodo requires
`actions/edit` to reopen a published record before it will accept metadata
again. The driver's retry path replayed the whole sequence — set metadata,
upload, publish — and so failed at the first write, never reaching the publish
step whose 409 handling the retry existed to exercise.

The fix corrected a mistake in principle, not only in detail: idempotency here
means that "already done" returns the existing result rather than repeating the
work. Reopening a published deposit in order to write metadata nobody asked to
change would be worse than failing.

The same run showed that error messages carrying no status code are close to
useless: a bare "Not found." left it open whether the endpoint was wrong, the
record was missing, or the operation was refused. Statuses are now included.

### Keeping the fake honest

Every divergence above was invisible offline by construction: the fake and the
tests agreed with each other and both were wrong. The observed key sets are
therefore recorded in `tests/fixtures/zenodo/deposition-shape.json`, an offline
test asserts the fake produces at least those keys and omits the ones a draft
lacks, and the live test reports drift between the recorded shape and what the
API currently returns.

The fixture is updated only from an observed response. Editing it by hand to
make a test pass would restore precisely the situation it exists to prevent.

## What you need before the live part

- A Zenodo **sandbox** account.
- A registered OAuth application, for the client id and redirect URI.
- A personal token with `deposit:write` and `deposit:actions`, in `.env` as
  `DD_ZENODO_TOKEN`. It reaches only your machine; the implementation and its
  tests never need the value.

Nothing above blocks Parts A, B or D2, which is where the work starts.
