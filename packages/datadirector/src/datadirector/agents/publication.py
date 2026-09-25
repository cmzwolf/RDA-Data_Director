"""The publication agent: the last step, and the only irreversible one.

Cluster 4 Part E. Sequences the deposit and enforces the conditions that must
hold before anything leaves the institution.

Four refusals, each guarding something that cannot be undone afterwards:

  - it will not run while a gate item is unresolved (§9.6);
  - it will not run while validation reports an error (R4);
  - it will not upload an artefact a human excluded;
  - it will not delete working material until a publish is confirmed.

The last is the one that looks like housekeeping and is not. Deleting class-1
material after a *failed* deposit would leave a job that cannot be resumed and a
researcher whose data the tool consumed without publishing.
"""

from __future__ import annotations

from pathlib import Path
from shutil import rmtree

from datadirector_contracts import (
    CanonicalRecord, DecisionRecord, DepositReceipt, EffectKind, Event,
    EventKind, Orcid, StepIntent, ValidationFinding,
)

from ..errors import AuthorityError
from ..gate.items import Gate, artefacts_to_upload
from ..workflow.effects import (
    EffectRecorder, ReconciliationRequired, idempotency_key, unfinished,
)
from ..state.projection import JobState
from .base import Agent



from ..job_handle import latest_drafted_record
from ..workflow.engine import StepFailed
from ..workflow.graph import Condition
from .base import Authority, Capabilities, Invocation, Outcome
from .registry import AgentContext, register_agent

@register_agent
class PublicationAgent(Agent):
    name = "publication"
    serves = ("R7",)

    def __init__(self, driver, working_root: Path | str, *, store=None) -> None:
        self._driver = driver
        self.root = Path(working_root)
        self._store = store
        self._effects = EffectRecorder(store) if store is not None else None


    def check_ready(self, gate: Gate, findings: list[ValidationFinding]) -> list[str]:
        """Everything that must be true before a deposit is attempted.

        Returned as a list rather than raised one at a time, so a researcher sees
        all of it at once instead of discovering it in sequence.
        """
        blocking: list[str] = []
        for item in gate.unresolved():
            blocking.append(f"unresolved: {item.summary}")
        for finding in findings:
            if finding.severity == "error":
                blocking.append(
                    f"validation error on {finding.field or 'record'}: "
                    f"{finding.message}")
        return blocking

    def deposit(self, state: JobState, record: CanonicalRecord,
                artefacts: list[Path], *, gate: Gate,
                findings: list[ValidationFinding], human: Orcid
                ) -> tuple[DepositReceipt, list[Event], DecisionRecord]:
        if self._store is not None:
            outstanding = unfinished(self._store, state.job_id)
            if outstanding:
                # Publishing over an interrupted attempt is how a dataset gets
                # deposited twice. The attempt is settled first, or not at all.
                raise ReconciliationRequired(
                    "an earlier attempt against an external service was "
                    "interrupted and has not been settled: "
                    + "; ".join(f"{a.intent.step} on {a.intent.target}"
                                for a in outstanding))

        blocking = self.check_ready(gate, findings)
        if blocking:
            raise AuthorityError(
                "deposit refused; the following must be resolved first:\n  - "
                + "\n  - ".join(blocking))

        excluded = set(gate.state.excluded_artefacts())
        # Compared by every form the log could have used, not by basename:
        # material registered as "data/obs.csv" is not named "obs.csv" to the
        # gate, and a basename comparison uploads the file a person excluded.
        to_upload = artefacts_to_upload(
            artefacts, excluded,
            unpacked=self.root / state.job_id / "unpacked")
        if not to_upload:
            # Every file excluded is a coherent outcome, not an error: the
            # researcher decided nothing here may be published.
            events = [self.event(state, EventKind.WORKFLOW_CLOSED_NOT_SHARED,
                                 human=human,
                                 payload={"reason": "all artefacts excluded at "
                                                    "the approval gate"})]
            decision = DecisionRecord(
                agent=self.identity, step="deposit",
                selected="closed-not-shared",
                selection_basis="every artefact was excluded by a human decision "
                                "at the gate; there is nothing to deposit")
            raise NothingToDeposit(events, decision)

        if self._effects is None:
            receipt = self._driver.deposit(state.job_id, record, to_upload,
                                           on_behalf_of=human)
        else:
            intent = StepIntent(
                step="deposit", effect=EffectKind.REPOSITORY_PUBLISH,
                target=getattr(self._driver, "base_url", "repository"),
                idempotency_key=idempotency_key(state.job_id, "deposit",
                                                "repository"),
                detail={"job_id": state.job_id,
                        "files": [p.name for p in to_upload]})
            with self._effects.attempt(state.job_id, intent) as result:
                receipt = self._driver.deposit(state.job_id, record, to_upload,
                                               on_behalf_of=human)
                result.update({"pid": receipt.pid,
                               "concept_pid": receipt.concept_pid})

        events = [self.event(state, EventKind.DEPOSIT_COMPLETED, human=human,
                             payload={
                                 "pid": receipt.pid,
                                 "concept_pid": receipt.concept_pid,
                                 "landing_page": receipt.landing_page,
                                 "files": [p.name for p in to_upload],
                                 "excluded": sorted(excluded),
                             })]
        decision = DecisionRecord(
            agent=self.identity, step="deposit", selected=receipt.pid,
            selection_basis=(
                "deposited after every gate item was resolved and validation "
                "reported no errors; excluded artefacts were not uploaded"
            ))
        return receipt, events, decision

    def release_working_material(self, state: JobState, receipt: DepositReceipt
                                 ) -> list[str]:
        """Delete class-1 material, only after a confirmed publish (§7.3).

        Refuses without a persistent identifier. Deleting after a failed deposit
        would leave a job that cannot be resumed and a researcher whose data the
        tool consumed without publishing it.
        """
        if not receipt.pid:
            raise AuthorityError(
                "working material is not released without a persistent "
                "identifier: without one the deposit is not confirmed and the "
                "job must remain resumable"
            )
        target = self.root / state.job_id / "unpacked"
        removed: list[str] = []
        if target.exists():
            removed = sorted(p.name for p in target.rglob("*") if p.is_file())
            rmtree(target)
        return removed

    @classmethod
    def build(cls, context: AgentContext) -> "PublicationAgent":
        return cls(context.repository_driver, context.working_root,
                   store=context.store)
    @classmethod
    def capabilities(cls) -> Capabilities:
        return Capabilities(
            name="publication",
            summary=("checks the gate and the findings, and publishes the "
                      "record and its artefacts through the configured "
                      "repository driver"),
            human_follows=True,
            establishes=(
                Condition("the job has concluded",
                          lambda s: bool(getattr(s, "terminal", None))),
                 ),
            inspects_material=True,
            serves=("R7", "R9"))
    def run(self, invocation: Invocation) -> Outcome:
        """Publish. The person is not something this agent may borrow.
        The driver is handed the whole state, so an agent that could reach
        this with a hand-built handle could publish a dataset without
        anyone's consent. The identity that authorises a deposit therefore
        arrives as an authenticated instruction, and an invocation without
        one is refused here, loudly, rather than going hunting through the
        log for a name whose authority it would be borrowing.
        """
        handle = invocation.job
        instruction = invocation.instruction
        if (instruction is None or instruction.author is not
                 Authority.DEPOSITOR or instruction.actor is None):
            raise PermissionError(
                 "publishing requires an authenticated person; the agent "
                 "takes one from the invocation, never from the log")
        state = handle.state
        events_log = handle.events()
        record = latest_drafted_record(events_log)
        if record is None:
            raise StepFailed("no metadata record has been drafted yet")
        raw_findings = next((event.payload.get("findings", [])
                              for event in reversed(events_log)
                              if event.kind is EventKind.VALIDATION_COMPLETED),
                             [])
        findings = [ValidationFinding.model_validate(raw)
                     for raw in raw_findings]
        root = handle.unpacked_root()
        artefacts = [root / a for a in handle.material()]
        receipt, events, decision = self.deposit(
            state, record, artefacts, gate=handle.gate(),
            findings=findings, human=instruction.actor)
        return Outcome(job_id=handle.job_id, events=list(events),
                       decision=decision, result=receipt,
                       artefacts=[str(a.relative_to(root))
                                   for a in artefacts],
                       message="deposited the collection")


class NothingToDeposit(Exception):
    """Every artefact was excluded. A terminal success, not a failure."""

    def __init__(self, events: list[Event], decision: DecisionRecord) -> None:
        super().__init__("all artefacts excluded at the approval gate")
        self.events = events
        self.decision = decision
