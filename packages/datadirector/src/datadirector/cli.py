"""A minimal command-line interface.

Scaffolding, not the researcher-facing interface. The approval surface described
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
import sys
from pathlib import Path

from datadirector_contracts import Event, EventKind, Orcid, Visibility

from .agents.ingestion import IngestionAgent
from .config.loader import capability_report_text, load_policy, load_wiring, resolve
from .credentials.broker import CredentialBroker
from .errors import DataDirectorError
from .plugins.discovery import PluginRegistry
from .provenance.recorder import Recorder
from .state.projection import fold
from .state.store import EventStore

APP_VERSION = "0.1.0"


def _new_job_id() -> str:
    import random
    import string
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
    print(capability_report_text(resolved, refused=registry.refused))
    if missing:
        print(f"\nCredentials not set in the environment: {missing}")
    return 0


def cmd_ingest(args) -> int:
    store, recorder, working = _paths(args)
    job_id = _new_job_id()
    wiring = load_wiring(args.wiring)
    policy = load_policy(args.policy)
    resolved = resolve(wiring, policy, PluginRegistry(), application_version=APP_VERSION)

    store.append(Event(sequence=1, job_id=job_id, kind=EventKind.WORKFLOW_CREATED,
                       agent="cli/0.1.0",
                       payload={"config_digest": str(resolved.digest())}))
    state = fold(store.load(job_id))
    agent = IngestionAgent(working)
    events, decision = agent.ingest(state, Path(args.source))
    for ev in events:
        store.append(ev)
    state = fold(store.load(job_id))
    print(f"job: {job_id}")
    print(f"files: {len(state.material)}")
    print(f"step: {state.step}")
    print(f"awaiting: a responsibility and compliance statement, then human confirmation")
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


def cmd_provenance(args) -> int:
    store, recorder, _ = _paths(args)
    level = Visibility(args.visibility)
    print(json.dumps(recorder.export(args.job, up_to=level), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="datadirector", description=__doc__)
    parser.add_argument("--wiring", default="config/wiring.example.yaml")
    parser.add_argument("--policy", default="config/policy.example.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="validate configuration and report capabilities")

    p = sub.add_parser("ingest", help="register a submission and profile it")
    p.add_argument("source", help="a file, archive or directory")

    p = sub.add_parser("status", help="show job state")
    p.add_argument("--job", default=None)

    p = sub.add_parser("verify", help="verify the event chain")
    p.add_argument("--job", default=None)

    p = sub.add_parser("provenance", help="export PROV-O at a visibility level")
    p.add_argument("job")
    p.add_argument("--visibility", default="open",
                   choices=[v.value for v in Visibility])

    args = parser.parse_args(argv)
    handlers = {"check": cmd_check, "ingest": cmd_ingest, "status": cmd_status,
                "verify": cmd_verify, "provenance": cmd_provenance}
    try:
        return handlers[args.command](args)
    except DataDirectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
