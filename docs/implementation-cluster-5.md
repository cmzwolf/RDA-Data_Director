# Implementation Specification — Cluster 5: The Interface

**Scope.** Authentication with real sessions, and a server-rendered web interface
whose centre of gravity is the approval gate. Requirement C6, and the part of P4
that only becomes real when a person is actually looking at a screen.

**Boundary.** Ends with a researcher able to sign in with ORCID, watch a job,
review every outstanding item individually, and deposit — without the command
line, and without any capability the core API does not already expose.

---

## Why server-rendered

A JavaScript application against the core API is what people expect, and it was
rejected. Three reasons, in descending order of how much they should count.

**Accessibility is the requirement, and the default matters.** C6 asks for WCAG
2.1 AA. HTML forms, native controls and full-page navigation are accessible
before anyone works on them; a client-side application is accessible after
sustained effort, and the effort is invisible until someone tries to use it with
a screen reader. ADR-019 already rejected a chat interface partly on these
grounds, and reintroducing the same difficulty in a different shape would make
that decision incoherent.

**The dependency surface is an argument we have already made.** P14 and C17 rest
partly on this system being modest enough to run on an institution's own
hardware without specialist staff. A build toolchain, a package ecosystem and a
second language are not fatal to that, but they are a real cost, and the thing
they buy — interactivity — is not what the gate needs.

**The gate is a form.** The screen that matters presents items, evidence and a
decision per item. That is what HTML forms have always been for. Nothing in the
approval workflow requires optimistic updates, live collaboration or client-side
state; a page that reloads after each decision is not merely acceptable but
*better*, because it re-derives from the log rather than trusting a local copy.

What is given up: no live progress without polling, no drag-and-drop, no rich
client-side validation. The first is met by a refresh interval, the second is not
needed, and the third is a duplicate of server validation we would have to write
anyway.

**Constraint:** the interface uses the core API's service layer and the same
pipeline the command line uses, never a private path. Anything the interface can
do, another Director can do (C5); and a second route into ingestion would be a
second place for the rules to be applied differently. A test asserts the
interface never constructs an ingestion agent of its own.

**Revised after the assembly audit.** This specification was written against
`JobService` as though that were the whole system, before `runtime.py` and
`pipeline.py` existed. The interface now holds a `Pipeline`, which holds a
`Runtime`: submission goes through the same composition root as everything else,
and the submit page can therefore tell a researcher what the installation cannot
do *before* they commit their time to it.

---

## Part A — Ownership

Access is decided by ownership, not by a role hierarchy, and the model is
**append-only**: rights can be granted and never withdrawn.

That single property removes a layer. Revocable rights need arbitration — who
may remove whom, what happens to work in progress, which administrator
adjudicates — and none of that exists here. It is the same shape as the event
log, applied to access.

It also removes a presumption. An earlier draft of §10 gave a data steward
standing rights over work they had never been invited to. Convenient, and a
little high-handed. Under ownership a steward is added by the researcher, and
that addition is itself a recorded human act: consent rather than hierarchy.

### The four rules

1. A job belongs to its creator on creation.
2. An owner may add owners, **by ORCID**, whether or not that person has ever
   signed in. Ownership is by identifier, so a colleague can be added before
   their first visit.
3. **No one is ever removed.** There is no method that removes an owner, and the
   absence is the contract, as it is for the event store and the gate.
4. Ownership governs the workflow. The **auditor** reads without owning and
   cannot act.

### When the last owner leaves

A researcher moves institution and their ORCID is the sole owner of a job. Since
nobody can be removed, nobody can be added, and the job becomes **auditable but
not actionable**: readable for accountability, workable by no one.

This is deliberate. The alternative — an administrator granting themselves
ownership — is a back door that would exist for a rare case and be available for
every other. Placing the obligation on the person leaving makes it a policy
problem with a policy owner, which is where it belongs.

The interface therefore owes a researcher one specific list: **the jobs where
they are the only owner**. That is what a person needs before they go, and
nobody can assemble it for themselves.

### Recording

**Adding an owner carries a reason.** Not enforced, but recorded and prompted
for. "Added M. Aroa" is weaker than "Added M. Aroa, taking over while I am on
leave", and the second is what an auditor reading it in two years needs.

**An auditor's read leaves a trace.** Reading material one does not own is
recorded as an `access.audited` event, payload-free. An audit role whose use is
invisible is an unlogged back door with a respectable name.

### Consequences for the API

- `GET /jobs` returns only jobs the caller owns.
- A job the caller does not own returns **404, not 403**. A 403 confirms the job
  exists, and for a system holding sensitive material existence is itself a
  disclosure.
- Ownership is checked **before** anything else in a mutating endpoint. A
  refusal that arrives after a side effect is not a refusal.
- Auditors are exempt from the ownership filter on reads and from nothing else.

### Invariants

- No public method removes an owner.
- The creator is an owner from the first event, not from a later grant.
- An added ORCID is checksum-validated; that it belongs to a real person who
  exists is not verified, and is not claimed to be.
- An auditor cannot resolve a gate item, confirm a declaration, or deposit.

### Tests

A non-owner sees 404 for a job that exists. An added owner sees it. Removal is
not expressible. A sole-owner job is listed as such for its owner. An auditor
reads and is refused every act. Reading by an auditor appends an access event;
reading by an owner does not.

## Part B — Sessions

### B1. `identity/session.py`

**Responsibility.** Turn an ORCID sign-in into a session the API and interface
can both resolve.

`cmd_serve` began as a refusal - it raised rather than accept any token,
deliberately, because an API that accepted anything would attribute every act to
whoever called it and every provenance record would be a lie. That refusal has
since been replaced with the mechanism below, and the command now brings up the
core API and the interface together, bound to localhost by default: the API
identifies every caller by ORCID but holds no network security of its own, so
exposing it beyond the machine is a deployment decision rather than a default.

**Design.**
- A session is a random identifier, an ORCID, the roles that identity carries, an
  issue time and an expiry.
- Stored server-side; the cookie holds the identifier and nothing else. A
  self-describing token would put identity in the client's hands, and identity is
  what every accountability claim in this system rests on.
- Cookie is `HttpOnly` and `SameSite=Lax`. `Secure` follows the scheme actually in
  use rather than the deployment profile: a single-user deployment behind a tunnel
  is served over HTTPS, and a session cookie without `Secure` there would be sent
  in clear on any accidental downgrade.
- Sessions are revocable, and revocation is immediate rather than at expiry
  (`revoke`, and `revoke_all_for` when an identity is withdrawn altogether).

**Invariants.**
- No session without an identity established by a completed ORCID exchange, or by
  the local-account path that exists for the single-user profile and says so.
  Every session records how its identity was established - `authentication` is
  `orcid` or `local-accounts` - so provenance from a run where the operator
  asserted an identity stays distinguishable from a real researcher's work. The
  local path is mounted only where the deployment profile permits it
  (`DEVELOPMENT_PROFILES`, which is the single-user-local and institutional
  profiles) and only where an account file is configured, because passwords in a
  text file are not an authentication system; `cmd_session`, which mints a session
  from the machine without ORCID at all, is confined further - to the single-user
  local profile, as a command rather than a route, and its act is written to the
  log.
- An expired or unknown identifier resolves to nothing, never to a default user.
- Outside that path, the ORCID in a session is the one ORCID returned by the
  identity provider; it is never supplied by the client.

**Tests.** A forged cookie resolves to nothing. Expiry is enforced on read, not
only on write. Revocation takes effect on the next request. The cookie carries
no ORCID.

### B2. `api/auth.py`

Wires sessions into the existing `resolve_principal` hook, so the API is
unchanged in shape: it already takes a resolver, and this supplies a real one.

Routes: `GET /auth/login` (redirect to ORCID), `GET /auth/callback` (exchange,
create session, set cookie), `POST /auth/logout`. Where the deployment has no ORCID
client configured the ORCID routes are not mounted at all - `cmd_serve` says so
rather than sending a visitor to an authorization page that cannot complete - and
the single-user profile may instead mount the local-account equivalent from
`web/local_auth.py`: the same `/auth/login` path, a `POST /auth/local` that checks
the account file, and the same logout, with every session it issues marked
`local-accounts`.

Where the deployment also has a repository driver and an OAuth client, the same
builder mounts `/auth/repository` and its callback, so a person delegates a
repository token in the same pass in which they sign in (§C2 of cluster 4) rather
than being sent to a settings page to paste one.

**Invariant.** The state parameter is single-use and bound to the request that
issued it, as the OAuth flow in cluster 4 already requires.

---

## Part C — The gate screen

This is the cluster. Everything else is navigation.

### C1. `web/templates/gate.html`

**What it must show, per item.**

| Element | Why |
|---|---|
| What is proposed, in plain language | A vocabulary token is not a decision a person can make |
| The evidence | §9.5: the system decides what to *show*; the human decides |
| A field-level diff where a value would change | "Redact column 3" is not reviewable; `Kerema → [village]` is |
| Model uncertainty, where the finding came from a model | Displayed rather than hidden: a confident-looking wrong answer is the failure mode |
| Whether a finding is a presumption | §9.5: "not inspected, presumed sensitive" must not read like "inspected, found sensitive" |
| The permitted decisions, and only those | A CARE referral offers no *approve*; the screen must not either |

**What it must not have.** No select-all. No "approve remaining". No default
selection on any decision control. A reviewer who can accept forty items with
one action has reviewed nothing, and the interface is the last place that rule
can be quietly broken.

**Reasons that block the button.** `PUBLISH_AS_IS` and `CONSULTED` already
require a recorded reason in the domain layer, and the form must enforce it
before submission as well as after — not because the server check is
insufficient, but because a rejection after the fact teaches a user to type
anything that passes.

### C2. Measurement

§4.1 claims rubber-stamping is detectable from approval latency and per-item
override rates. That claim costs something: the interface must record when an
item was **displayed**, not only when it was decided, or latency is unmeasurable.

- A `gate.item-displayed` event per item shown, once per page render.
- The existing resolution events already carry the decision and the human.

**Invariant.** Display events are provenance-visible but carry no payload; they
are about the interaction, not the material.

---

## Part D — The rest of the interface

Deliberately thin. Each screen exists because the gate needs it as context.

| Screen | Contents |
|---|---|
| **Landing** | What this is, that you sign in with your own ORCID, and what happens to your data. The only unauthenticated page |
| **Submit** | Upload, an optional instruction in the researcher's own words, an optional plan reference, and what this installation cannot do |
| Jobs | List with state, outstanding items, blocked-or-not |
| Sole-owned | The jobs where the caller is the only owner, because nobody can assemble that list for a colleague who is leaving |
| Job | Progress as named phases with what the person must do next, the material, and ownership, collapsed because it is real but rarely urgent |
| Declaration | The claims the statement produced, stated and inferred sensitivity shown separately, confirmed item by item with nothing preselected |
| Gate | The outstanding items, each its own form (§C1). The reason the interface exists |
| History | The event log, narrated rather than dumped, filterable to one phase |
| Deposit | Target, what will be uploaded, what is excluded, and the irreversibility notice |

There is **no metadata screen and no provenance screen**, and that is a decision
rather than an omission. The metadata record reaches a person through the gate and
the deposit screen, where the fields they must actually decide between are shown
beside their provenance; a page that renders the whole `CanonicalRecord` would be
a second place for a value to appear without its origin, which is the failure the
field-level provenance exists to prevent. Provenance leaves the system through the
API's export endpoint and the `provenance` command, at a visibility the caller's
role permits, because provenance is an artefact to be handed to someone who will
read it carefully rather than browsed.

**The submit screen is the way in**, and its instruction field is where the
trust boundary becomes visible to a researcher. What they type carries directive
authority because they are accountable for the deposit; the identical sentence
inside one of their files carries none. The field says so, because a person who
does not know that cannot reason about why their deposit went where it did.

**The deposit screen is the second-hardest.** It is the last point at which a
mistake is cheap. It must show what will be uploaded *and what will not*, name
the repository, state plainly that publication cannot be undone, and require the
same explicit acknowledgement the API requires. No default focus on the submit
button.

---

## Part E — Accessibility

C6 requires WCAG 2.1 AA, and we have cited accessibility as a *reason* for two
design decisions without ever testing it. Building an interface and not checking
would make C6 another claimed-but-unverified row, which is the failure Appendix
B.5 is about.

**In the build.** Semantic elements over ARIA patches; every control labelled;
visible focus; a skip link; no colour-only meaning; forms usable by keyboard
alone; error summaries linked to the fields that caused them.

**Checked automatically** by structural assertions over the rendered HTML: every
page declares a language, has exactly one `h1`, offers a skip link to `#main`,
labels every control, conveys a presumption in text rather than by styling
alone, and defines a visible focus style. These run with no browser engine and
no extra dependency, which is what makes them run at all.

An `axe-core` pass over a served instance would find more, and is **not**
implemented. It needs a browser engine in the test environment, and a suite that
cannot run without one is a suite people stop running. Recorded here as absent
rather than listed as a future intention that never arrives.

**Not checked automatically**, and stated as such: a screen reader pass, and
whether the language of the gate is comprehensible to someone who is not a data
steward. Automated checks find perhaps a third of WCAG failures. Claiming AA on
their strength alone would be exactly the overstatement this project keeps
finding in itself.

---

## Build order

1. Ownership (A) and sessions (B) together. Neither is testable without the
   other: ownership is the model, sessions are how it becomes checkable.
2. Layout, navigation, jobs list — the smallest thing that renders.
3. **The gate screen (C1).** The reason for the cluster.
4. Display events (C2).
5. Job, declaration and history screens (D).
6. Submit and deposit screens (D).
7. Accessibility checks (E), continuously rather than at the end.

---

## Definition of done

- A researcher signs in with ORCID, uploads data, optionally says what should
  happen to it, and reaches their own jobs — and only those — without the CLI.
- Submission goes through the same pipeline as the command line.
- An owner can continue or retry a job from the browser; an auditor cannot, and
  is not offered the control.
- An owner can add another owner by ORCID; no route removes one.
- A researcher can list the jobs where they are the sole owner.
- An auditor can read a job they do not own, cannot act on it, and their reading
  is recorded.
- Every gate item is decided individually; no route accepts more than one.
- A decision requiring a reason cannot be submitted without one, client and
  server.
- An uninspected artefact is visibly a presumption, not a finding.
- A CARE referral offers no approval control.
- Deposit states what will and will not be uploaded, and requires explicit
  acknowledgement.
- Every screen is keyboard-navigable and passes automated accessibility checks.
- The conformance report still reconciles, with C6 updated as the work lands
  rather than after it.

## Open questions

**Multi-user deployment.** Ownership is straightforward to test with two ORCIDs
in the suite. What it does not test is whether the *offboarding* obligation is
one institutions will actually meet: a job whose owner has left is readable and
unworkable by design, and how often that happens in practice is an operational
question this project cannot answer from here.

**Polling interval.** A job waiting on a model call needs a refresh; too frequent
and it is a load problem on a local deployment, too slow and it feels broken.
Probably a decision for a deployment rather than for us, which means
configuration.

**Whether the interface should show the exposure ledger.** It answers "what saw
this data?", which a researcher may well want. But it is also a surface on which
to notice that a lot of their material reached a model, which may be exactly the
conversation that should happen.
