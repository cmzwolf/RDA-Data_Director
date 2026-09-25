"""The application service behind the core API.

Holds no state of its own. Every answer is folded from the event log, so an
instance restarted mid-workflow, or a second instance reading the same store,
sees what the first one saw. That property is what C5's inter-instance
interaction would rest on, and it is cheaper to keep than to add later.

Gate items live in the log too, rather than in a Gate object held in memory:
an approval surface that forgot its items on restart would make the human gate
the least durable part of a system built around durability.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Callable

from datadirector_contracts import (
    CanonicalRecord, Event, EventKind, GateItem, GateState, ItemDecision,
    Orcid, Resolution, SensitivityClass,
)

from ..errors import AuthorityError
from ..gate.items import Gate, artefacts_to_upload
from ..job_handle import latest_drafted_record
from ..state.projection import JobState, fold
from ..state.store import EventStore

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

GATE_ITEMS_ADDED = "gate.items-added"


def new_job_id() -> str:
    return "job-" + "".join(random.choice(ALPHABET) for _ in range(26))


class JobService:
    def __init__(self, store: EventStore, *, recorder=None, driver=None,
                 publication=None, config_digest: str | None = None,
                 agent: str = "core-api/0.1.0",
                 unpacked_root: Callable[[str], Path] | None = None) -> None:
        self.store = store
        self.recorder = recorder
        self.driver = driver
        # The publication agent, where the refusals live. Optional only because
        # a deployment may have no repository at all; where one exists, deposit
        # goes through it. It used to be constructed and bypassed: the interface
        # called this service directly, which checked the gate and nothing else,
        # so a job with validation errors could be published from a browser.
        self.publication = publication
        self.config_digest = config_digest
        self.agent = agent
        # Where a job's material sits. The log records artefacts by name,
        # relative to the directory ingestion wrote them to, and only the
        # deployment knows that layout. It is handed in from the runtime that
        # owns it: a second spelling of a path is how a deposit comes to look
        # for files where nothing was ever written.
        self._unpacked_root = unpacked_root

    # -- jobs --------------------------------------------------------------

    def create(self, *, human: Orcid) -> str:
        job_id = new_job_id()
        self.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.WORKFLOW_CREATED,
            agent=self.agent, payload={"config_digest": self.config_digest,
                                       "created_by": human.value}))
        return job_id

    def list_jobs(self) -> list[str]:
        return self.store.list_jobs()

    def state(self, job_id: str) -> JobState:
        return fold(self.store.load(job_id))

    def events(self, job_id: str) -> list[Event]:
        return self.store.load(job_id)

    # -- the gate ----------------------------------------------------------

    def add_gate_items(self, job_id: str, items: list[GateItem]) -> None:
        """Record items in the log so the gate survives a restart."""
        self.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.REDACTION_PROPOSED,
            agent=self.agent,
            payload={"marker": GATE_ITEMS_ADDED,
                     "items": [json.loads(i.model_dump_json()) for i in items]}))

    def gate(self, job_id: str) -> Gate:
        """Rebuild the gate from the log.

        Items and resolutions are both events, so the answer to "what is still
        outstanding" is the same after a restart, on another machine, or a year
        later.
        """
        items: list[GateItem] = []
        resolutions: list[Resolution] = []
        for event in self.store.load(job_id):
            if event.kind is EventKind.REDACTION_PROPOSED and \
                    event.payload.get("marker") == GATE_ITEMS_ADDED:
                items += [GateItem.model_validate(i)
                          for i in event.payload.get("items", [])]
            elif event.kind is EventKind.REDACTION_DECIDED and event.human:
                resolutions.append(Resolution(
                    item_id=event.payload["item_id"],
                    decision=ItemDecision(event.payload["decision"]),
                    decided_by=event.human,
                    reason=event.payload.get("reason"),
                    decided_at=event.occurred_at))
        return Gate(GateState(items=items, resolutions=resolutions))

    def resolve(self, job_id: str, item_id: str, decision: ItemDecision, *,
                human: Orcid, reason: str | None = None) -> Resolution:
        gate = self.gate(job_id)
        resolution = gate.resolve(item_id, decision, human=human, reason=reason)
        self.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.REDACTION_DECIDED,
            agent=self.agent, human=human,
            payload={"item_id": item_id, "decision": decision.value,
                     "reason": reason, "reason_recorded": bool(reason)}))
        return resolution

    # -- the material --------------------------------------------------------

    def unpacked_root(self, job_id: str) -> Path:
        """Where this job's registered material sits.

        Asked of the deployment rather than reconstructed here: one place
        owns the working layout, and a second spelling of it is how a
        deposit ends up looking in a directory that was never written to.
        """
        if self._unpacked_root is None:
            raise AuthorityError(
                  "this instance was built without the runtime that knows "
                  "where a job's material sits, so the artefacts named in "
                  "the log cannot be resolved to files")
        return self._unpacked_root(job_id)

    def artefacts(self, job_id: str) -> list[Path]:
        """The artefacts the log names, as files, in registration order.

        Read from the log rather than handed over by the caller. A deposit whose
        file list arrives from the screen that offered it is a deposit that can
        quietly carry a shorter list than the one the researcher reviewed; a
        deposit whose list comes from the log can only publish what ingestion
        registered and the gate let through.

        A name in the log with no file behind it is refused rather than skipped.
        A file that has gone missing is not a file that was excluded, and
        publishing the remainder without saying so would attach an identifier to
        something other than what was reviewed.
        """
        root = self.unpacked_root(job_id)
        names = list(self.state(job_id).material)
        paths = [root / name for name in names]
        missing = [name for name, path in zip(names, paths) if not path.is_file()]
        if missing:
            raise AuthorityError(
                 f"the log names {', '.join(missing)}, but "
                 f"{'it is' if len(missing) == 1 else 'they are'} not on disk "
                 f"under {root}. The material has been removed since it was "
                  "registered, so this job cannot be published as it stands")
        return paths

    def _record_deposit_decision(self, job_id: str, decision) -> None:
        """Keep the basis of the deposit beside the provenance graph.

        The engine writes a decision for every step it runs, and the deposit is
        the one step it cannot run because it belongs to a person. The only
        irreversible act in the system should not be the only one whose reasoning
        goes unrecorded.
        """
        root = getattr(self.recorder, "graph_root", None)
        if root is None:
            return
        path = Path(root) / job_id
        path.mkdir(parents=True, exist_ok=True)
        with open(path / "decisions.jsonl", "a", encoding="utf-8") as fh:
            fh.write(decision.model_dump_json() + "\n")

    def _validation_findings(self, job_id: str):
        """Validation results, read back from the log.

        Read rather than recomputed: the findings a person saw when they
        approved are the findings the deposit is checked against, and
        revalidating here could refuse on a basis nobody was shown.
        """
        from datadirector_contracts import ValidationFinding

        for event in reversed(self.store.load(job_id)):
            if event.kind is EventKind.VALIDATION_COMPLETED:
                out = []
                for raw in event.payload.get("findings") or []:
                    try:
                        out.append(ValidationFinding.model_validate(raw))
                    except Exception:
                        continue
                return out
        return []

    def validation_findings(self, job_id: str):
        """The findings a person was last shown, for the review screen.

        Public because the deposit screen must say plainly which findings
        block publishing and which are only advisory, and it must show the
        same findings the deposit will be checked against rather than a
        fresh computation the researcher never saw.
        """
        return self._validation_findings(job_id)


    # -- human acts --------------------------------------------------------

    def confirm_declaration(self, job_id: str, *, human: Orcid,
                            sensitivity: SensitivityClass | None,
                            note: str | None = None) -> SensitivityClass:
        level = sensitivity if sensitivity is not None else SensitivityClass.SENSITIVE
        self.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.DECLARATION_CONFIRMED,
            agent=self.agent, human=human,
            payload={"sensitivity": int(level), "note": note,
                     "assumed_most_restrictive": sensitivity is None}))
        return level

    def approve_metadata(self, job_id: str, *, human: Orcid) -> None:
        self.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.METADATA_APPROVED,
            agent=self.agent, human=human, payload={}))

    def revise_metadata(self, job_id: str, record, *, human: Orcid) -> None:
        """Keep a researcher's own revision of the drafted record.

        The record is frozen, so this appends a *new* draft rather than editing
        one in place: the draft the metadata agent produced, and any earlier
        revision, both stay in the log, and `latest_drafted_record` reads back
        the newest. That is what makes an edit auditable — you can see what was
        drafted, what the person changed, and when.

        The fields the researcher filled are stamped as theirs by the caller
        that built `record`, not here: a value a person typed has a different
        standing from one a model guessed, and only the entry point that
        authenticated the person can say which is which (C14).
        """
        self.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.METADATA_DRAFTED,
            agent=self.agent, human=human,
            payload={"record": json.loads(record.model_dump_json())}))

    def deposit(self, job_id: str, *, human: Orcid, repository: str,
                record: CanonicalRecord | None = None,
                artefacts: list[Path] | None = None):
        """Publish. The only irreversible operation in the API.

        Refuses while the gate is open. Returns the existing identifiers if this
        job was already published, because a client that lost the response must
        be able to ask again without creating a second record — the failure the
        Zenodo driver was corrected for.
        """
        state = self.state(job_id)
        if state.deposit_pid:
            return {"pid": state.deposit_pid, "already_published": True}

        gate = self.gate(job_id)

        # The gate first. A missing driver is the deployment's problem and a
        # missing decision is the researcher's, and telling someone their
        # installation is misconfigured when what is actually outstanding is a
        # decision they must make sends them to fix the wrong thing.
        if gate.blocks_deposit():
            raise AuthorityError(
                "deposit refused: "
                + "; ".join(i.summary for i in gate.unresolved()))

        if self.driver is None and self.publication is None:
            raise AuthorityError(
                "no repository driver is configured for this deployment, so "
                "nothing can be deposited from it")

         # What is published is what the log says, not what the caller says it
         # is. Both entry points used to arrive here with neither the record nor
         # the artefacts, so the driver was offered an empty list and the
         # publication agent read that as "the researcher excluded every file" —
         # a fully reviewed job answering a press of Publish with a 500. The
         # record is the newest draft, which is the one the person last edited;
         # the artefacts are those ingestion registered and the gate let through.
        if record is None:
            record = latest_drafted_record(self.store.load(job_id))
            if record is None:
                raise AuthorityError(
                    "nothing can be published before a metadata record has been "
                    "drafted; continue the job until the metadata step has run")
        if artefacts is None:
            artefacts = self.artefacts(job_id)
            if not artefacts:
                raise AuthorityError(
                    "no material has been registered for this job, so there is "
                    "nothing to publish. That is not the same as every file "
                    "having been excluded at the gate, and is said plainly "
                    "rather than closing the job on a decision nobody made")

        if self.publication is not None:
            # Every refusal in one place. The agent checks the gate *and*
            # validation, excludes what a person withheld, and refuses over an
            # interrupted attempt that was never settled.
            from ..agents.publication import NothingToDeposit
            try:
                receipt, events, decision = self.publication.deposit(
                    state, record, list(artefacts), gate=gate,
                    findings=self._validation_findings(job_id), human=human)
            except NothingToDeposit as closed:
                # Not a failure: the researcher withheld every file, and a
                # decision to publish nothing is honoured rather than punished.
                # The closure event is what makes the job terminal, so it
                # belongs in the log, and the client is told nothing was
                # published rather than met with an unhandled exception.
                for event in closed.events:
                    self.store.append(event)
                self._record_deposit_decision(job_id, closed.decision)
                return {"pid": None, "concept_pid": None, "landing_page": None,
                        "already_published": False,
                        "nothing_to_deposit": True}
            for event in events:
                self.store.append(event)
            self._record_deposit_decision(job_id, decision)
            return {"pid": receipt.pid, "concept_pid": receipt.concept_pid,
                    "landing_page": receipt.landing_page,
                    "already_published": False}

        excluded = set(gate.state.excluded_artefacts())
        unpacked_dir = (self._unpacked_root(job_id)
                          if self._unpacked_root is not None else None)
        to_upload = artefacts_to_upload(list(artefacts), excluded,
                                         unpacked=unpacked_dir)
        receipt = self.driver.deposit(job_id, record, to_upload,
                                      on_behalf_of=human)
        self.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.DEPOSIT_COMPLETED,
            agent=self.agent, human=human,
            payload={"pid": receipt.pid, "concept_pid": receipt.concept_pid,
                     "landing_page": receipt.landing_page,
                     "repository": repository,
                     "excluded": sorted(excluded)}))
        return {"pid": receipt.pid, "concept_pid": receipt.concept_pid,
                "landing_page": receipt.landing_page,
                "already_published": False}


def capabilities_from(report, *, repositories: list[str] | None = None,
                      residencies: list[str] | None = None,
                      schemas: list[str] | None = None,
                      accepts_deposits: bool = False):
    """Build the capability response from the conformance report.

    Derived rather than written, for the reason Appendix B.5 gives: a
    hand-maintained statement of what an instance can do is a statement nobody
    checks, and this one is read by other instances deciding what to send.
    """
    from .models import Capabilities, RequirementCoverage
    from datadirector_contracts import SensitivityClass

    return Capabilities(
        coverage=[RequirementCoverage(requirement=r, declared_status=s,
                                      components=report.coverage.get(r, []))
                  for r, s in sorted(report.claims.items(),
                                     key=lambda kv: (kv[0][0], int(kv[0][1:])))],
        schemas_emitted=sorted(schemas or []),
        repositories=sorted(repositories or []),
        model_residencies=sorted(residencies or []),
        sensitivity_classes=[c.label for c in SensitivityClass],
        accepts_deposits=accepts_deposits)
