"""The workflow engine: sequencing, halting, resumption, compensation.

Document A §7.2 and §12. There is no in-memory session. Resumption is: load the
log, fold it, select the next runnable step, continue. That is what makes a job
started on Monday resumable on Thursday, possibly by a different person with the
appropriate role.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from datadirector_contracts import DecisionRecord, Event, EventKind, Orcid
from datadirector_contracts.events import CompensationPayload

from ..errors import DataDirectorError, ExternalServiceError
from ..provenance.recorder import Recorder
from ..state.projection import JobState, fold, fold_to
from ..state.store import EventStore


class StepFailed(DataDirectorError):
    """A step could not complete. Halts the job with a recorded reason."""


@runtime_checkable
class Step(Protocol):
    """One unit of work with a precondition on job state."""

    name: str

    def runnable(self, state: JobState) -> bool: ...

    def run(self, state: JobState) -> tuple[list[Event], DecisionRecord]:
        """Return the events to append and the decision record for this step.

        Every step produces at least one event and exactly one decision record,
        including deterministic ones: C14 explainability does not distinguish
        between steps that used a model and steps that did not.
        """
        ...


class WorkflowEngine:
    SERVES = ("C7", "C10", "C8", "P13")
    def __init__(self, store: EventStore, recorder: Recorder,
                 steps: list[Step], agent_id: str = "workflow-engine/0.1.0",
                 *, max_attempts: int = 3, base_delay_seconds: float = 2.0,
                 sleep=None) -> None:
        self.store = store
        self.recorder = recorder
        self.steps = list(steps)
        self.agent_id = agent_id
        self.max_attempts = max(1, max_attempts)
        self.base_delay_seconds = base_delay_seconds
        # Injectable so tests do not spend real seconds proving that waiting
        # happens, and so a deployment can make retries immediate if it wants.
        self._sleep = sleep or time.sleep

    def _backoff(self, attempt: int) -> float:
        """Exponential, so a service that is down is not hammered.

        Capped: a delay long enough to look like a hang is worse for a person
        watching than a halt they can act on.
        """
        return min(self.base_delay_seconds * (2 ** (attempt - 1)), 30.0)

    def state(self, job_id: str) -> JobState:
        return fold(self.store.load(job_id))

    def next_step(self, state: JobState) -> Step | None:
        if state.terminal or state.halted_reason:
            return None
        for step in self.steps:
            if step.runnable(state):
                return step
        return None

    def advance(self, job_id: str) -> JobState:
        """Run the next runnable step, if any.

        External service failures are **retried** before they halt. A local
        model that timed out on a long document, or a registry that was briefly
        unreachable, is the common case, and halting for a human on the first
        timeout makes the workflow demand attention it does not need.

        A failure that is not an external service failure is not retried: a
        malformed record will be malformed again, and repeating it only delays
        the halt. The distinction is the reason `ExternalServiceError` exists as
        a separate type.
        """
        state = self.state(job_id)
        step = self.next_step(state)
        if step is None:
            return state

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                events, decision = step.run(state)
                break
            except ExternalServiceError as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    self.store.append(Event(
                        sequence=1, job_id=job_id, kind=EventKind.STEP_FAILED,
                        agent=self.agent_id,
                        payload={"step": step.name, "attempt": attempt,
                                 "error": str(exc)[:300],
                                 "retrying_in_seconds": self._backoff(attempt)}))
                    self._sleep(self._backoff(attempt))
                    continue
                return self._halt(
                    job_id,
                    f"{step.name}: {exc} (after {attempt} attempts)")
            except Exception as exc:
                return self._halt(job_id,
                                  f"{step.name} failed unexpectedly: {exc}")
        else:  # pragma: no cover - the loop always breaks or returns
            return self._halt(job_id, f"{step.name}: {last_error}")


        return self._append_run(job_id, step, events, decision)

    def run_step(self, job_id: str, step: Step) -> JobState:
        """Run one named step, with the same recording as a first run.

        A rerun has to leave the same trace as a first run — the events, the
        completion, the decision — or a draft produced on request looks, in
        the audit, like a draft that never happened. Splitting it out is what
        lets a rerun reuse that path instead of approximating it.
        """
        state = self.state(job_id)
        events, decision = step.run(state)
        return self._append_run(job_id, step, events, decision)

    def _append_run(self, job_id: str, step: Step, events, decision) -> JobState:
        for ev in events:
            self.store.append(ev)

        # That a step ran is itself worth recording. Inferring completion from
        # the events a step produces leaves a node that produces none — media
        # with nothing to inspect, redaction, whose output is gate items —
        # permanently incomplete, and the traversal returns it forever. That is
        # how metadata, documentation and validation came to never run in a
        # workflow whose every component was wired and tested.
        self.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.STEP_COMPLETED,
            agent=self.agent_id,
            payload={"step": step.name,
                     "idempotency_key": f"step:{job_id}:{step.name}"}))
        self._record_decision(job_id, step, decision)
        return self.state(job_id)

    def run_until_blocked(self, job_id: str, max_steps: int = 50) -> JobState:
        """Advance until no step is runnable, a gate is reached, or a halt.

        Gates are not special-cased here: a step awaiting a human simply stops
        being runnable, so the engine stops with no separate notion of waiting.

        **Progress is measured in steps run, not in state changed.** An earlier
        version returned as soon as a step left the state unchanged, and steps
        legitimately do: media inspection emits no event when a submission holds
        no images, and redaction emits none at all because its output is gate
        items. The first such step convinced the engine there was nothing left
        to do, so metadata, documentation and validation never ran — in a
        workflow whose every component was wired and tested.

        The loop guard is now "this step ran and is still asking to run",
        which is a real stall, rather than "nothing changed", which is ordinary.
        """
        state = self.state(job_id)
        ran: set[str] = set()
        for _ in range(max_steps):
            step = self.next_step(state)
            if step is None:
                return state
            if step.name in ran:
                # Ran and still runnable: it cannot make progress, and looping
                # would spin rather than stop.
                return state
            ran.add(step.name)
            state = self.advance(job_id)
            if state.halted_reason or state.terminal:
                return state
        return state

    def _halt(self, job_id: str, reason: str) -> JobState:
        self.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.WORKFLOW_HALTED,
            agent=self.agent_id, payload={"reason": reason},
        ))
        return self.state(job_id)

    def _record_decision(self, job_id: str, step: Step, decision: DecisionRecord) -> None:
        path = self.recorder.graph_root / job_id
        path.mkdir(parents=True, exist_ok=True)
        with open(path / "decisions.jsonl", "a", encoding="utf-8") as fh:
            fh.write(decision.model_dump_json() + "\n")

    def compensate(self, job_id: str, *, to_sequence: int, reason: str,
                   human: Orcid) -> JobState:
        """Roll back by appending, never by deleting (ADR-008, C8).

        The compensating event names the state being restored; the fold does the
        undoing. Nothing is removed, so the audit record of what was undone
        survives the undoing.
        """
        events = self.store.load(job_id)
        if not 1 <= to_sequence <= len(events):
            raise ValueError(
                f"cannot restore {job_id} to sequence {to_sequence}; "
                f"the log has {len(events)} events"
            )
        payload = CompensationPayload(
            compensates_sequence=len(events),
            restores_state_at_sequence=to_sequence,
            reason=reason,
        )
        self.store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.COMPENSATED,
            agent=self.agent_id, human=human,
            payload=payload.model_dump(mode="json"),
        ))
        return self.state(job_id)

    def state_at(self, job_id: str, sequence: int) -> JobState:
        return fold_to(self.store.load(job_id), sequence)
