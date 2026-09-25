"""A minimal command-line interface.

A command-line surface over the same pipeline the web interface uses. It is not
the approval surface — that is the web gate screen, which shows evidence beside
each proposal and takes decisions one at a time — but it is no longer scaffolding
either: `ingest`, `advance`, `watch`, `accounts` and `serve` are how a deployment
is operated. The approval surface described
in ADR-019 needs field-level diffs and per-item decisions, and belongs to a later
cluster; this exists so the spine and the cluster-2 agents can be exercised by
hand rather than only through tests.

Nothing here bypasses a gate: `confirm` requires an ORCID on the command line
precisely because a confirmation without an accountable person is not a
confirmation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from datadirector_contracts import Visibility

from .credentials.broker import CredentialBroker
from .credentials.envfile import load_env_file
from .config.loader import capability_report_text, load_policy, load_wiring, resolve
from .errors import DataDirectorError
from .plugins.discovery import PluginRegistry
from .provenance.recorder import Recorder
from .state.projection import fold
from .state.store import EventStore

APP_VERSION = "0.1.0"


def _new_job_id() -> str:
    import random
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "job-" + "".join(random.choice(alphabet) for _ in range(26))


def _paths(args) -> tuple[EventStore, Recorder, Path]:
    wiring = load_wiring(args.wiring)
    store = EventStore(wiring.storage.state_root)
    recorder = Recorder(store, Path(wiring.storage.state_root).parent / "prov")
    return store, recorder, Path(wiring.storage.working_root)


def cmd_check(args) -> int:
    wiring = load_wiring(args.wiring)
    policy = load_policy(args.policy)
    registry = PluginRegistry()
    registry.discover()
    broker = CredentialBroker(wiring.credential_env_vars)
    missing = broker.check_present()
    resolved = resolve(wiring, policy, registry, application_version=APP_VERSION)
    # Reported from what this deployment actually builds, not from entry-point
    # discovery: the built-in components are not entry points, so that report
    # said NOT AVAILABLE for every requirement while `conformance` said the
    # opposite — two commands in one tool contradicting each other.
    from .runtime import Runtime

    coverage, runtime = None, None
    try:
        runtime = Runtime(resolved, state_root=wiring.storage.state_root,
                          working_root=wiring.storage.working_root)
        coverage = runtime.coverage()
    except Exception as exc:
        print(f"could not assemble the runtime: {type(exc).__name__}: {exc}",
              file=sys.stderr)

    print(capability_report_text(resolved, refused=registry.refused,
                                 coverage=coverage))

    if runtime is not None:
        shortfalls = runtime.unavailable()
        if shortfalls:
            print("\nThis deployment cannot:")
            for line in shortfalls:
                print(f"  - {line}")
    if missing:
        print(f"\nCredentials not set in the environment: {missing}")
        read = getattr(args, "env_file", None)
        if read is not None:
             # "Not set" was true and useless while a filled-in .env sat in the
             # working directory unread. Say which file was consulted and which
             # names it supplied — names only, because this is the credential
             # path, and the thing that gets printed is the thing that leaks.
            print(f"Environment file: {read.summary()}")
    missing = _missing_web_dependencies()
    if missing:
        # Reported here so it is found while checking configuration rather than
        # when someone tries to serve and gets a traceback.
        print("\nnot installed (the interface and API need these):")
        for line in missing:
            print(f"  - {line}")
        print(f"\n{MISSING_DEPENDENCY_HINT}")

    return 0


def cmd_ingest(args) -> int:
    """Take in a submission and advance it as far as it can go.

    Previously this ran the ingestion agent and stopped, which left every other
    agent unreachable from any entry point. It now goes through the pipeline,
    which is the only place the parts are assembled.
    """
    from datadirector_contracts import Orcid

    from .pipeline import Pipeline
    from .runtime import Runtime

    wiring = load_wiring(args.wiring)
    policy = load_policy(args.policy)
    resolved = resolve(wiring, policy, PluginRegistry(),
                       application_version=APP_VERSION)

    if not args.depositor:
        print("--depositor is required: nothing here acts anonymously "
              "(Blueprint section 5.4)", file=sys.stderr)
        return 2

    runtime = Runtime(resolved, state_root=wiring.storage.state_root,
                      working_root=wiring.storage.working_root)
    pipeline = Pipeline(runtime)

    missing = runtime.unavailable()
    if missing:
        print("this deployment is missing:")
        for line in missing:
            print(f"  - {line}")
        print()

    result = pipeline.ingest(args.source, human=Orcid(value=args.depositor),
                             dmp_reference=args.dmp,
                             instruction=args.instruction)
    state = fold(runtime.store.load(result.job_id))

    print(f"job: {result.job_id}")
    print(f"files: {len(state.material)}")
    print(f"step: {state.step}")
    print(f"ran: {', '.join(result.ran)}")

    plan = [e for e in runtime.store.load(result.job_id)
            if e.kind.value == "dmp.commitments-read"]
    if plan:
        payload = plan[-1].payload
        route = payload.get("route")
        if route == "none-found":
            print("plan: none supplied or found. No commitments to verify "
                  "against, which is not permission to share.")
        else:
            print(f"plan: {payload.get('commitments', 0)} commitments "
                  f"({route}"
                  + (", structured" if payload.get("structured") else
                     ", extracted and provisional") + ")")
    for item in result.gate_items:
        print(f"awaiting confirmation: {item.summary}")

    print("awaiting: a responsibility and compliance statement, then human "
          "confirmation")
    return 0


def resolve_config_path(given: str | None, kind: str) -> str:
    """The configuration file to use, and a warning if it is the example.

    `*.example.yaml` means "copy me and edit me". The command line defaulted
    straight to the example, which made the suffix a lie in two ways: a
    production deployment would run on it, and any local edit would be a change
    to a version-controlled file, lost at the next pull.
    """
    if given:
        return given
    real = Path(f"config/{kind}.yaml")
    if real.exists():
        return str(real)
    example = Path(f"config/{kind}.example.yaml")
    if example.exists():
        print(
            f"using {example} because {real} does not exist.\n"
            f"  This is a template, and it is version-controlled: edits to it "
            f"will be lost.\n"
            f"  Copy it first:  cp {example} {real}",
            file=sys.stderr)
        return str(example)
    return str(real)


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def cmd_accounts(args) -> int:
    """Create a local development accounts file.

    Exists because configuring an ORCID application, and HTTPS with it, in order
    to debug an ingestion is the wrong order to do things in. The identifiers it
    writes are well formed — ORCID's checksum applies to fictions too — and are
    local fictions that must never reach a real deposit.
    """
    from .identity.local_accounts import write_example

    path = Path(args.path)
    if path.exists() and not args.force:
        print(f"{path} already exists; pass --force to replace it",
              file=sys.stderr)
        return 2
    write_example(path, count=args.count)
    print(f"wrote {path}")
    print("\nEvery account uses the password 'password'. Change them if you "
          "like;\nnothing here is protecting anything.")
    print(f"\n  export DD_LOCAL_ACCOUNTS={path}")
    print("  datadirector serve")
    return 0


def cmd_session(args) -> int:
    """Issue a session for an ORCID, from the machine, without the OAuth flow.

    This exists because the ORCID sandbox is not always able to issue tokens,
    which leaves a local deployment unable to sign anyone in at all.

    **It is a single-user workaround, and it is not how this system is meant to
    be used.** The instance belongs to no one ORCID: the application credentials
    identify the *installation* to ORCID, and every visitor signs in as
    themselves. Naming an ORCID on a command line inverts that — the operator
    decides who exists — so this is for a laptop with one user and nothing else.

    **It is also an impersonation tool**, and it is confined accordingly:

    - it is a command, never a route, so it cannot be reached over the network;
    - it refuses outside the single-user local profile, where the person at the
      keyboard is the only user and already controls the process;
    - it records what it did, so a session that did not come from ORCID is
      distinguishable in the log from one that did.

    On a shared deployment this would be a way to become anybody. On a laptop it
    is a way to use your own tool, and someone with shell access there can
    already read the state directory.
    """
    from datadirector_contracts import Event, EventKind, Orcid

    from .identity.session import SessionStore

    wiring = load_wiring(args.wiring)
    if wiring.profile.value != "single-user-local":
        print(f"refused: this issues a session without proving identity, and "
              f"the deployment profile is {wiring.profile.value!r}. Use the "
              "ORCID sign-in flow.", file=sys.stderr)
        return 2

    try:
        orcid = Orcid(value=args.orcid)
    except ValueError as exc:
        print(f"not a valid ORCID: {exc}", file=sys.stderr)
        return 2

    store = EventStore(wiring.storage.state_root)
    sessions = SessionStore(
        Path(wiring.storage.state_root).parent / "sessions.json")
    session = sessions.create(orcid)

    # Recorded against the deployment rather than a job: it is a fact about how
    # someone got in, and it should not be invisible.
    marker = f"job-{'0' * 26}"
    try:
        store.append(Event(sequence=1, job_id=marker,
                           kind=EventKind.INSTRUCTIONS_RECEIVED,
                           agent="cli-session/0.1.0", human=orcid,
                           payload={"note": "session issued locally without an "
                                            "ORCID exchange",
                                    "channel": "operator-console",
                                    "profile": wiring.profile.value}))
    except Exception:
        pass

    print(f"session for {orcid.value}, valid until "
          f"{session.expires_at.isoformat()}")
    print("\nThis session did not come from ORCID: identity was asserted, not "
          "proven.")
    print("It also makes this instance single-user, which is not how it is "
          "meant to run:\nwith an ORCID application configured, every visitor "
          "signs in as themselves.")
    print("Set it as a cookie in your browser, or use it as a bearer token:")
    print(f"\n  document.cookie = 'dd_session={session.identifier}; path=/'")
    print(f"\n  curl -H 'Authorization: Bearer {session.identifier}' "
          f"http://127.0.0.1:8000/jobs")
    return 0


def cmd_watch(args) -> int:
    """Report what the watched folder is offering.

    Reports rather than ingests: the folder is the lowest-friction way in
    (P12), and a command that silently swallowed whatever appeared there would
    ingest a half-copied file. Stability is checked before anything is offered.
    """
    from .runtime import Runtime

    wiring = load_wiring(args.wiring)
    policy = load_policy(args.policy)
    resolved = resolve(wiring, policy, PluginRegistry(),
                       application_version=APP_VERSION)
    runtime = Runtime(resolved, state_root=wiring.storage.state_root,
                      working_root=wiring.storage.working_root)

    ready = runtime.watched.stable_entries()
    if not ready:
        print(f"nothing settled in {wiring.storage.watched_folder}")
        return 0
    for entry in ready:
        print(f"ready: {entry}")
    print("\ningest one with: datadirector ingest <path> --depositor <orcid>")
    return 0


def cmd_gate(args) -> int:
    """Read what a model wrote, and let a named person answer for it.

    The web is where this is normally done, and the command line is where a job
    could quietly stop: an item that only a browser can resolve is a job that
    blocks forever with no reason printed anywhere. So this prints what is
    waiting, and takes the three answers a review item offers — validate it,
    write your own, ask again — through the same `Gate.resolve` the browser
    uses, which is the only way the two interfaces refuse the same things.
    """
    from datadirector_contracts import ItemDecision, Orcid

    from .pipeline import Pipeline
    from .runtime import Runtime

    wiring = load_wiring(args.wiring)
    policy = load_policy(args.policy)
    resolved = resolve(wiring, policy, PluginRegistry(),
                        application_version=APP_VERSION)
    runtime = Runtime(resolved, state_root=wiring.storage.state_root,
                       working_root=wiring.storage.working_root)
    pipeline = Pipeline(runtime)
    who = Orcid(value=args.reviewer)

    if args.rerun:
        result = pipeline.request_rerun(args.job, args.rerun, human=who,
                                         note=args.note or args.reason or "")
        print(f"reran: {', '.join(result.ran)}")
        for item in result.gate_items:
            print(f"still waiting: {item.summary}")
        return 0

    pending = pipeline.pending_reviews(args.job)
    if args.validate is not None:
        item = _review_item_for(pending, args.validate)
        if item is None:
            print(f"nothing from {args.validate!r} is waiting for a decision",
                   file=sys.stderr)
            return 2
        decision = ItemDecision(args.decision)
        if decision is ItemDecision.REQUEST_RERUN and not (args.reason
                                                              or "").strip():
            print("asking for another attempt requires --reason saying what "
                   "is wrong with the draft", file=sys.stderr)
            return 2
        pipeline.resolve_review(args.job, item.item_id, decision, human=who,
                                  reason=args.reason)
        print(f"{decision.value}: {item.summary}")
        if decision is not ItemDecision.REQUEST_RERUN:
             # The distinction the whole file is about: two of the three
             # answers validate the draft, and the third leaves it exactly as
             # blocked as it was. Printing them alike would recreate the defect.
            remaining = pipeline.pending_reviews(args.job)
            print(f"still waiting: {len(remaining)}")
        else:
            print(f"still waiting: {len(pipeline.pending_reviews(args.job))}"
                   " (a request for another attempt is not a validation)")
        return 0

    if not pending:
        print("no model output is waiting for a decision")
        return 0
    for item in pending:
        print(f"{item.item_id}")
        print(f"  {item.summary}")
        for line in item.detail:
            print(f"    - {line}")
        print(f"  answers: {', '.join(d.value for d in item.permitted_decisions)}")
    print(f"\n{len(pending)} model output(s) unvalidated. Nothing downstream of "
            "them will run, and nothing will be deposited.")
    print("answer one with: datadirector gate <job> --validate "
           "<agent> --decision approve|edited|request-rerun --reviewer <orcid>"
           " [--reason ...]")
    return 0


def _review_item_for(items, agent):
    """The pending item for one agent, by agent name rather than by digest.

    A person reading the screen knows which agent wrote the abstract, not what
    its digest came out as. The digest stays in the identifier, where it keeps
    doing its job.
    """
    for item in items:
        from .gate import review as review_items
        if review_items.agent_of(item.item_id) == agent:
            return item
    return None


def cmd_advance(args) -> int:
    """Run whatever the job can run now, and say what it is waiting for."""
    from .pipeline import Pipeline
    from .runtime import Runtime

    wiring = load_wiring(args.wiring)
    policy = load_policy(args.policy)
    resolved = resolve(wiring, policy, PluginRegistry(),
                       application_version=APP_VERSION)
    runtime = Runtime(resolved, state_root=wiring.storage.state_root,
                      working_root=wiring.storage.working_root)

    result = Pipeline(runtime).advance(args.job)
    if result.ran:
        print(f"ran: {', '.join(result.ran)}")
    else:
        print("ran: nothing")
    if result.awaiting:
        print(f"awaiting: {result.awaiting}")
    for line in result.unavailable:
        print(f"unavailable: {line}")
    return 0


def cmd_status(args) -> int:
    store, _, _ = _paths(args)
    if args.job:
        state = fold(store.load(args.job))
        print(json.dumps(json.loads(state.model_dump_json()), indent=2))
    else:
        for job in store.list_jobs():
            st = fold(store.load(job))
            print(f"{job}  step={st.step}  files={len(st.material)}"
                  f"  halted={st.halted_reason or '-'}")
    return 0


def cmd_verify(args) -> int:
    store, _, _ = _paths(args)
    jobs = [args.job] if args.job else store.list_jobs()
    for job in jobs:
        store.verify(job)
        print(f"{job}: chain verifies ({len(store.load(job))} events)")
    return 0


def cmd_conformance(args) -> int:
    from .conformance.report import generate, render
    report = generate(args.matrix)
    print(render(report))
    return 1 if report.findings else 0


MISSING_DEPENDENCY_HINT = (
    "install the web extras:\n"
    "    pip install -e 'packages/datadirector[api]'\n"
    "or, for everything a contributor needs:\n"
    "    pip install -e 'packages/datadirector[dev]'")


# What each command actually needs. Split because printing the descriptor needs
# no server, and telling someone to install one in order to write a JSON file
# would be asking for work that buys nothing.
WEB_DEPENDENCIES = {
    "fastapi": "the HTTP framework",
    "jinja2": "templates for the interface",
    "multipart": "file uploads",
}
SERVER_DEPENDENCY = {"uvicorn": "the server itself"}


def _missing_web_dependencies(required: dict[str, str] | None = None
                              ) -> list[str]:
    """Which web packages are absent, all of them, before anything imports one.

    Reported together rather than one crash at a time: a person who installs
    fastapi, runs again, and is told about jinja2 has been made to do the work
    a single message could have done. And a traceback from an import at the top
    of a function is a worse answer than a sentence.
    """
    import importlib.util

    required = required if required is not None else {
        **WEB_DEPENDENCIES, **SERVER_DEPENDENCY}

    def absent(name: str) -> bool:
        # An absent module makes find_spec return None; a module whose finder
        # raises is also unusable. Both count, because the question here is
        # "can this run", not "why not".
        try:
            return importlib.util.find_spec(name) is None
        except (ImportError, ValueError):
            return True

    return [f"{name} ({why})" for name, why in required.items()
            if absent(name)]


def _orcid_configuration_problems(args) -> list[str]:
    """Configuration faults that need no imports to detect.

    Separated so they can be reported alongside missing packages: both stop the
    service, and a person should learn about both at once.
    """
    problems = []
    if not os.environ.get("DD_LOCAL_ACCOUNTS") \
            and os.environ.get("DD_ORCID_CLIENT_ID"):
        base = os.environ.get("DD_ORCID_BASE", "https://sandbox.orcid.org")
        redirect = os.environ.get(
            "DD_ORCID_REDIRECT_URI",
            f"http://{args.host}:{args.port}/auth/callback")
        if "sandbox" not in base and not redirect.startswith("https://"):
            problems.append(
                "production ORCID accepts only HTTPS redirect URIs, and "
                f"DD_ORCID_REDIRECT_URI is {redirect!r}.\n\n"
                "Either put the service behind HTTPS, or expose it through a "
                "tunnel:\n"
                "    cloudflared tunnel --url http://127.0.0.1:8000\n"
                "then register that https URL plus /auth/callback with ORCID "
                "and set DD_ORCID_REDIRECT_URI to exactly the same string. "
                "ORCID matches it exactly: a trailing slash, or localhost "
                "where you registered 127.0.0.1, is a rejection.\n\n"
                "For local development, skip ORCID entirely:\n"
                "    datadirector accounts\n"
                "    export DD_LOCAL_ACCOUNTS=./local-accounts.txt")
    return problems


def cmd_serve(args) -> int:
    """Run the core API.

    Bound to localhost by default: this API identifies every caller by ORCID but
    holds no session infrastructure of its own, so exposing it beyond the machine
    is a deployment decision and not a default.
    """
    # Everything that would stop this starting, collected before anything is
    # reported. Returning on the first problem meant someone with a missing
    # package and a bad redirect URI learned about one, fixed it, and met the
    # other — two runs to discover what one could have said.
    problems: list[str] = []
    missing = _missing_web_dependencies()
    if missing:
        problems.append("not installed:\n  - " + "\n  - ".join(missing)
                        + f"\n\n{MISSING_DEPENDENCY_HINT}")

    problems += _orcid_configuration_problems(args)

    if problems:
        print("cannot serve:\n", file=sys.stderr)
        for problem in problems:
            print(f"{problem}\n", file=sys.stderr)
        return 2

    from .api.app import build_app
    from .api.auth import SessionResolver
    from .api.service import JobService, capabilities_from
    from .conformance.report import generate
    from .identity.session import SessionStore
    from .runtime import Runtime
    from .web.auth_routes import build_auth
    from .web.views import build_web

    wiring = load_wiring(args.wiring)
    policy = load_policy(args.policy)
    resolved = resolve(wiring, policy, PluginRegistry(),
                       application_version=APP_VERSION)
    from .runtime import Runtime as _Runtime
    runtime_for_service = _Runtime(resolved,
                                   state_root=wiring.storage.state_root,
                                   working_root=wiring.storage.working_root)
    store = runtime_for_service.store
    recorder = runtime_for_service.recorder
    service = JobService(store, recorder=recorder,
                         driver=runtime_for_service.repository,
                         publication=runtime_for_service.agent_registry.get("publication"),
                         unpacked_root=runtime_for_service.unpacked_root)

    sessions = SessionStore(Path(wiring.storage.state_root).parent / "sessions.json")
    resolver = SessionResolver(sessions)

    app = build_app(
        service, resolve_principal=resolver, resolve_roles=resolver.roles_for,
        deployment_profile=wiring.profile.value,
        capabilities=capabilities_from(
            generate(args.matrix),
            repositories=[p.name for p in wiring.plugins],
            residencies=sorted({b.residency.value for b in wiring.backends}),
            accepts_deposits=False))

    # The interface shares the API's service layer and its resolver rather than
    # having a path of its own: anything the interface can do, another Data
    # Director can do (C5).
    from .pipeline import Pipeline
    runtime_for_web = Runtime(resolved, state_root=wiring.storage.state_root,
                              working_root=wiring.storage.working_root)
    build_web(app, service, pipeline=Pipeline(runtime_for_web),
              deployment_profile=wiring.profile.value,
              sign_in_available=bool(os.environ.get("DD_ORCID_CLIENT_ID")
                                     or os.environ.get("DD_LOCAL_ACCOUNTS")),
              local_accounts=bool(os.environ.get("DD_LOCAL_ACCOUNTS")),
              residencies=sorted({b.residency.value
                                  for b in wiring.backends}),
              resolve_principal=resolver, resolve_roles=resolver.roles_for)

    # Sign-in. Local accounts first, because a deployment that has configured
    # them has said it is being developed against, and mounting both would make
    # it ambiguous how any given session was established.
    accounts_path = os.environ.get("DD_LOCAL_ACCOUNTS")
    if accounts_path:
        from .identity.local_accounts import LocalAccounts
        from .web.local_auth import build_local_auth
        from .web.views import templates_for

        accounts = LocalAccounts(accounts_path)
        try:
            known = accounts.load()
        except ValueError as exc:
            print(f"local accounts file is unusable: {exc}", file=sys.stderr)
            return 2
        if not known:
            print(f"no accounts in {accounts_path}; run "
                  "`datadirector accounts` to create some", file=sys.stderr)
            return 2
        try:
            build_local_auth(app, accounts, sessions, templates_for(),
                             profile=wiring.profile.value)
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        print(f"local development accounts in use ({len(known)} of them): "
              "identities are asserted, not proven")
        client_id = None
    else:
        client_id = os.environ.get("DD_ORCID_CLIENT_ID")
    if client_id:
        from .identity.orcid import OrcidIdentityProvider
        broker = CredentialBroker(dict(wiring.credential_env_vars))
        try:
            broker.get("orcid:client-secret")
        except Exception as exc:
            # Checked before the routes are mounted. Failing at the token
            # exchange instead would send a person to ORCID, bring them back,
            # and refuse them — a confusing way to discover a missing line of
            # configuration.
            print(f"sign-in is configured but unusable: {exc}", file=sys.stderr)
            return 2
        orcid_base = os.environ.get("DD_ORCID_BASE",
                                    "https://sandbox.orcid.org")
        redirect = os.environ.get(
            "DD_ORCID_REDIRECT_URI",
            f"http://{args.host}:{args.port}/auth/callback")
        if "sandbox" not in orcid_base:
            print(f"using production ORCID ({orcid_base}): every act will be "
                  "attributed to a real identifier, permanently")
        provider = OrcidIdentityProvider(client_id, broker, redirect,
                                         base_url=orcid_base)
        flow = None
        zenodo_client = os.environ.get("DD_ZENODO_CLIENT_ID")
        if zenodo_client:
            from .credentials.oauth import DelegatedTokenStore, ZenodoOAuthFlow
            flow = ZenodoOAuthFlow(
                zenodo_client,
                CredentialBroker(dict(wiring.credential_env_vars))
                .get("zenodo:client-secret"),
                os.environ.get("DD_ZENODO_REDIRECT_URI",
                               f"http://{args.host}:{args.port}"
                               "/auth/repository/callback"),
                DelegatedTokenStore(
                    Path(wiring.storage.state_root).parent / "delegations.json"),
                base_url=os.environ.get("DD_ZENODO_BASE",
                                        "https://sandbox.zenodo.org"))
        # Secure cookies follow the scheme actually in use, not the profile: a
        # single-user deployment behind a tunnel is served over HTTPS, and a
        # session cookie without Secure there would be sent in clear on any
        # accidental downgrade.
        secure_cookies = _env_flag(
            "DD_SECURE_COOKIES",
            default=redirect.startswith("https://"))
        build_auth(app, provider, sessions,
                   secure_cookies=secure_cookies,
                   deposit_flow=flow, resolver=resolver)
    else:
        print("no DD_ORCID_CLIENT_ID: sign-in routes are not mounted, so no "
              "session can be issued")

    import uvicorn

    print(f"serving on http://{args.host}:{args.port}/")
    print("sign-in requires an ORCID exchange; no session is minted without one")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_openapi(args) -> int:
    """Write the OpenAPI descriptor generated from the runtime models."""
    missing = _missing_web_dependencies(WEB_DEPENDENCIES)
    if missing:
        print("cannot build the descriptor; these are not installed:",
              file=sys.stderr)
        for line in missing:
            print(f"  - {line}", file=sys.stderr)
        print(f"\n{MISSING_DEPENDENCY_HINT}", file=sys.stderr)
        return 2

    import json as _json

    from datadirector_contracts import Orcid

    from .api.app import build_app
    from .api.service import JobService

    service = JobService(EventStore(args.state_root or "./var/state"))
    app = build_app(service, resolve_principal=lambda a: Orcid(
        value="0000-0002-1825-0097"))
    print(_json.dumps(app.openapi(), indent=2))
    return 0


def cmd_retention(args) -> int:
    """Report what could be deleted. Deletes only with --apply.

    Reporting is the default because a cleanup that removed material on a first
    exploratory run would be the failure this whole design avoids: our working
    copy may be the only copy.
    """
    from .retention.sweeper import RetentionSweeper

    wiring = load_wiring(args.wiring)
    store = EventStore(wiring.storage.state_root)
    sweeper = RetentionSweeper(store, wiring.storage.working_root)

    candidates = sweeper.assess()
    if not candidates:
        print("no working directories found")
        return 0

    print(f"{'category':<20} {'files':>6} {'bytes':>12}  {'safe':<5} job")
    for candidate in candidates:
        safe = "yes" if candidate.deletable_automatically else "no"
        print(f"{candidate.category.value:<20} {candidate.file_count:>6} "
              f"{candidate.byte_size:>12}  {safe:<5} "
              f"{candidate.job_id or Path(candidate.path).name}")
        print(f"    {candidate.reason}")
        if candidate.survival and candidate.survival.detail:
            print(f"    survival: {candidate.survival.detail}")

    if not args.apply:
        print("\nreporting only; pass --apply to mark eligible directories")
        return 0

    marked = 0
    for candidate in candidates:
        if candidate.deletable_automatically:
            sweeper.mark(candidate)
            marked += 1
    print(f"\nmarked {marked} directory(ies) for deletion after the grace period")

    due = sweeper.due()
    for path, mark in due:
        record = sweeper.delete(path, mark)
        print(f"deleted {record.path}: {record.file_count} files, "
              f"{record.byte_size} bytes")
    if not due:
        print("nothing is past its grace period yet")
    print("\nclosed-not-shared material is never handled here: nothing else "
          "holds it,\nso its removal is ordered by a person, not a schedule.")
    return 0


def cmd_provenance(args) -> int:
    store, recorder, _ = _paths(args)
    level = Visibility(args.visibility)
    print(json.dumps(recorder.export(args.job, up_to=level), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="datadirector", description=__doc__)
    parser.add_argument(
        "--wiring", default=None,
        help="deployment wiring; defaults to config/wiring.yaml, falling back "
             "to the example with a warning")
    parser.add_argument(
        "--policy", default=None,
        help="policy; defaults to config/policy.yaml, falling back to the "
             "example with a warning")
    parser.add_argument(
        "--env-file", default=None,
        help="a file of NAME=VALUE lines read into the environment before the "
              "command runs, for credentials the shell did not set (default: "
              ".env in the working directory). A variable the shell already "
              "exported wins over the file, and no value is ever printed.")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="validate configuration and report capabilities")

    p = sub.add_parser("ingest", help="register a submission and profile it")
    p.add_argument("source", help="a file, archive or directory")
    p.add_argument("--depositor", required=True,
                   help="ORCID of the person depositing. Required: nothing "
                        "in this system acts anonymously.")
    p.add_argument("--dmp", default=None,
                   help="path or URL of a data management plan; omit and the "
                        "submission is searched for one")
    p.add_argument("--instruction", default=None,
                   help="what you want done, in your own words, e.g. 'deposit "
                        "this in EUDAT'. Carries directive authority because "
                        "you are accountable for the deposit; the same words "
                        "inside a submitted file carry none.")

    p = sub.add_parser("status", help="show job state")
    p.add_argument("--job", default=None)

    p = sub.add_parser("verify", help="verify the event chain")
    p.add_argument("--job", default=None)

    p = sub.add_parser("conformance",
                       help="check the conformance matrix against this tree")
    p.add_argument("--matrix", default="docs/architecture.md")

    p = sub.add_parser("retention",
                       help="report, and with --apply remove, orphaned material")
    p.add_argument("--apply", action="store_true",
                   help="mark eligible directories and delete those past grace")

    p = sub.add_parser("advance", help="run what this job can run now")
    p.add_argument("job")

    p = sub.add_parser(
         "gate",
        help="read what a model wrote that nobody has validated, and answer")
    p.add_argument("job")
    p.add_argument("--validate", default=None,
                    metavar="AGENT",
                    help="record a decision about this agent's model output")
    p.add_argument("--decision", default="approve",
                    choices=["approve", "edited", "request-rerun"],
                    help="approve or edited validate what the model wrote; "
                          "request-rerun does not — the draft stays unvalidated "
                          "until the next one has been read")
    p.add_argument("--reason", default=None,
                    help="why the draft is wrong, in your own words. Required "
                          "to ask for another attempt: without it the second "
                          "draft is the first draft's procedure run again")
    p.add_argument("--rerun", default=None, metavar="AGENT",
                    help="run this agent again on the reason on the log")
    p.add_argument("--note", default=None,
                    help="what the reviewer says about the draft, recorded as "
                          "the reviewer's own words and kept separate from the "
                          "depositor's instruction")
    p.add_argument("--reviewer", required=True,
                    help="ORCID of the person answering. A review item that a "
                          "model cleared is not reviewed")

    p = sub.add_parser("watch",
                       help="report settled submissions in the watched folder")

    p = sub.add_parser(
        "accounts",
        help="write a local development accounts file (no ORCID needed)")
    p.add_argument("--path", default="./local-accounts.txt")
    p.add_argument("--count", type=int, default=3,
                   help="how many accounts; the workflow needs more than one "
                        "to exercise ownership and the auditor role")
    p.add_argument("--force", action="store_true",
                   help="replace an existing file")

    p = sub.add_parser(
        "session",
        help="issue one local session without a login form "
             "(single-user profile only)")
    p.add_argument("--orcid", required=True,
                   help="the identifier to issue a session for. Asserted, not "
                        "proven.")

    p = sub.add_parser("serve", help="run the core API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--matrix", default="docs/architecture.md")

    p = sub.add_parser("openapi", help="print the generated OpenAPI descriptor")
    p.add_argument("--state-root", default=None)

    p = sub.add_parser("provenance", help="export PROV-O at a visibility level")
    p.add_argument("job")
    p.add_argument("--visibility", default="open",
                   choices=[v.value for v in Visibility])

    args = parser.parse_args(argv)
    args.wiring = resolve_config_path(args.wiring, "wiring")
    args.policy = resolve_config_path(args.policy, "policy")

    # Credentials reach the process from the environment and from nowhere else, and
    # `.env` is where a developer puts them. Read before dispatch, so every
    # command sees the same environment: the broker reported the variable missing
    # while a filled-in .env sat unread in the working directory, because nothing
    # in this tree opened the file. Names are reported; values never are.
    args.env_file = load_env_file(args.env_file)
    for line in args.env_file.problems():
        print(line, file=sys.stderr)

    handlers = {"check": cmd_check, "ingest": cmd_ingest, "status": cmd_status,
                "verify": cmd_verify, "provenance": cmd_provenance,
                "conformance": cmd_conformance, "serve": cmd_serve,
                "retention": cmd_retention, "advance": cmd_advance, "watch": cmd_watch,
                "session": cmd_session, "accounts": cmd_accounts,
                  "gate": cmd_gate,
                "openapi": cmd_openapi}
    try:
        return handlers[args.command](args)
    except DataDirectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
