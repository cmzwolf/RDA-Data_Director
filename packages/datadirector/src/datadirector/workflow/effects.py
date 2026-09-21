"""Recording intent before an external effect, and reconciling what is left over.

The problem this closes. The event log records completed steps, so a crash
*during* a step is indistinguishable from a step never attempted. That is
harmless for internal work — recompute and carry on — and not harmless for
anything with an effect outside this system. A process that dies after a
repository accepts an upload but before the completion is written leaves a log
saying "not deposited", and a resume that trusts the log deposits again.

Until now that case was handled in one driver, by hand: the Zenodo driver
persists its deposition id and checks whether the record is already published.
That worked and did not generalise. A second `RepositoryDriver` written by a
third party would have had to reinvent it, and nothing told them they must.

What replaces it: an **intent** written before the effect and an **outcome**
written after. On recovery, an intent with no outcome is not retried; it is
*reconciled* — the external service is asked what actually happened, using an
idempotency key recorded with the intent.

The honest part is the third answer. Reconciliation can find that the effect
took place, that it did not, or that it cannot tell. The third is not the
system's to resolve. Guessing is how a dataset gets published twice or silently
not at all, so an indeterminate outcome becomes a gate item and a person decides.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Callable

from datadirector_contracts import (
    EffectKind, Event, EventKind, GateItem, GateItemKind, ItemDecision, Orcid,
    Reconciliation, StepIntent, StepOutcome,
)
from pydantic import BaseModel, ConfigDict

from ..errors import DataDirectorError
from ..state.store import EventStore

TOOK_EFFECT = "took-effect"
DID_NOT = "did-not-take-effect"
INDETERMINATE = "indeterminate"

# Reconciliation decisions a person may take on an indeterminate outcome.
INDETERMINATE_DECISIONS = [
    ItemDecision.CONSULTED,      # I checked the service myself; it took effect
    ItemDecision.NOT_APPLICABLE,  # I checked; it did not
    ItemDecision.EXCLUDE_FROM_DEPOSIT,
]


class UnfinishedAttempt(BaseModel):
    """An intent with no outcome. Something may or may not have happened."""

    model_config = ConfigDict(frozen=True)

    sequence: int
    intent: StepIntent


class ReconciliationRequired(DataDirectorError):
    """The workflow cannot proceed until unfinished attempts are settled.

    Raised rather than resolved, because the alternative is to guess on behalf
    of a researcher about whether their data was published.
    """


def idempotency_key(job_id: str, step: str, target: str, *,
                    discriminator: str = "") -> str:
    """A key that is stable across retries of the same logical attempt.

    Stable, not unique: two retries of one attempt must share a key or the
    external service cannot tell them apart, which is how duplicates are made.
    Two genuinely different attempts must not, which is why the discriminator
    exists — uploading two files in one job is two attempts, not one.
    """
    material = f"{job_id}|{step}|{target}|{discriminator}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


class EffectRecorder:
    """Writes intent before an effect and an outcome after it."""

    def __init__(self, store: EventStore, agent: str = "effects/0.1.0") -> None:
        self.store = store
        self.agent = agent

    @contextmanager
    def attempt(self, job_id: str, intent: StepIntent):
        """Bracket an external effect with an intent and an outcome.

        The intent is written and flushed *before* the body runs. If the process
        dies inside the body, the log holds an intent with no outcome, which is
        precisely the signal recovery needs — and the reason this is a context
        manager rather than a decorator is that the ordering has to be visible
        at the call site.
        """
        self.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.STEP_STARTED,
            agent=self.agent, payload=intent.model_dump(mode="json")))
        result: dict = {}
        try:
            yield result
        except Exception as exc:
            # A failure the process survived is recorded as one. It is not an
            # unfinished attempt: we know how it ended.
            self.store.append(Event(
                sequence=1, job_id=job_id, kind=EventKind.STEP_FAILED,
                agent=self.agent,
                payload={"step": intent.step,
                         "idempotency_key": intent.idempotency_key,
                         "error": f"{type(exc).__name__}: {exc}"[:500]}))
            raise
        self.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.STEP_COMPLETED,
            agent=self.agent,
            payload=StepOutcome(step=intent.step,
                                idempotency_key=intent.idempotency_key,
                                result=result).model_dump(mode="json")))


def unfinished(store: EventStore, job_id: str) -> list[UnfinishedAttempt]:
    """Attempts that were started and never concluded.

    An attempt is concluded by a completion, a recorded failure, or a
    reconciliation. Anything else is a question the log cannot answer alone.
    """
    started: dict[str, tuple[int, StepIntent]] = {}
    concluded: set[str] = set()
    for event in store.load(job_id):
        payload = event.payload
        key = payload.get("idempotency_key")
        if not key:
            continue
        if event.kind is EventKind.STEP_STARTED:
            started[key] = (event.sequence, StepIntent.model_validate(payload))
        elif event.kind in (EventKind.STEP_COMPLETED, EventKind.STEP_FAILED,
                            EventKind.STEP_RECONCILED):
            concluded.add(key)
    return [UnfinishedAttempt(sequence=seq, intent=intent)
            for key, (seq, intent) in sorted(started.items(),
                                             key=lambda kv: kv[1][0])
            if key not in concluded]


Prober = Callable[[StepIntent], tuple[str, str]]
"""Asks an external service whether an attempt took effect.

Returns a finding and a human-readable detail. Supplied per effect kind by the
component that owns the external relationship, because only it knows how to ask.
"""


def reconcile(store: EventStore, job_id: str, attempt: UnfinishedAttempt,
              probers: dict[EffectKind, Prober], *,
              agent: str = "effects/0.1.0") -> Reconciliation:
    """Ask what happened, and record the answer.

    Where no prober is registered for an effect kind the answer is
    indeterminate, not "did not happen". An unasked question and a negative
    answer must not look alike — the same rule the media tier keeps about
    inspection.
    """
    prober = probers.get(attempt.intent.effect)
    if prober is None:
        finding, detail = INDETERMINATE, (
            f"no way to ask {attempt.intent.target} whether this took effect; "
            "nothing is inferred from the absence of an answer")
    else:
        try:
            finding, detail = prober(attempt.intent)
        except Exception as exc:
            finding, detail = INDETERMINATE, (
                f"{attempt.intent.target} could not be asked: "
                f"{type(exc).__name__}: {exc}"[:300])

    reconciliation = Reconciliation(
        step=attempt.intent.step, idempotency_key=attempt.intent.idempotency_key,
        finding=finding, detail=detail)

    if finding != INDETERMINATE:
        # A settled answer concludes the attempt. An indeterminate one does not:
        # it stays open until a person settles it, because a workflow that moved
        # on would be acting on a guess about whether data was published.
        store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.STEP_RECONCILED,
            agent=agent, payload=reconciliation.model_dump(mode="json")))
    return reconciliation


def settle(store: EventStore, job_id: str, attempt: UnfinishedAttempt,
           finding: str, *, human: Orcid, detail: str,
           agent: str = "effects/0.1.0") -> Reconciliation:
    """A person settles an indeterminate attempt. A human act, so it names one."""
    if finding not in (TOOK_EFFECT, DID_NOT):
        raise ValueError(
            f"{finding!r} does not settle anything; a person settling an "
            f"attempt must say whether it took effect")
    reconciliation = Reconciliation(
        step=attempt.intent.step, idempotency_key=attempt.intent.idempotency_key,
        finding=finding, detail=detail, resolved_by=human)
    store.append(Event(
        sequence=1, job_id=job_id, kind=EventKind.STEP_RECONCILED,
        agent=agent, human=human,
        payload=reconciliation.model_dump(mode="json")))
    return reconciliation


def gate_items_for(attempts: list[UnfinishedAttempt]) -> list[GateItem]:
    """Indeterminate attempts as gate items, so deposit blocks on them."""
    items = []
    for attempt in attempts:
        intent = attempt.intent
        items.append(GateItem(
            item_id=f"unfinished:{intent.idempotency_key}",
            kind=GateItemKind.UNINSPECTED_FILE
            if intent.effect is EffectKind.REPOSITORY_UPLOAD
            else GateItemKind.DMP_DISCREPANCY,
            artefact=intent.detail.get("artefact"),
            summary=(f"{intent.step} against {intent.target} was interrupted and "
                     "it is not known whether it took effect"),
            detail=[
                f"effect: {intent.effect.value}",
                f"idempotency key: {intent.idempotency_key}",
                "Check the service directly before continuing. Retrying without "
                "checking risks a duplicate; abandoning without checking risks "
                "believing something was published when it was not.",
            ],
            permitted_decisions=INDETERMINATE_DECISIONS))
    return items
