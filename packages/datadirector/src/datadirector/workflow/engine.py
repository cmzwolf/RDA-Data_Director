"""The workflow engine: sequencing, halting, resumption, compensation.

Document A §7.2 and §12. There is no in-memory session. Resumption is: load the
log, fold it, select the next runnable step, continue. That is what makes a job
started on Monday resumable on Thursday, possibly by a different person with the
appropriate role.
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

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
    def __init__(self, store: EventStore, recorder: Recorder,
                 steps: list[Step], agent_id: str = "workflow-engine/0.1.0") -> None:
        self.store = store
        self.recorder = recorder
        self.steps = list(steps)
        self.agent_id = agent_id

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

        External service failures halt rather than propagate: principle P13
        requires the workflow to pause and resume rather than fail, and the
        halt is a recorded event so the reason survives the process.
        """
        state = self.state(job_id)
        step = self.next_step(state)
        if step is None:
            return state
        try:
            events, decision = step.run(state)
        except ExternalServiceError as exc:
            return self._halt(job_id, f"{step.name}: {exc}")
        except Exception as exc:
            return self._halt(job_id, f"{step.name} failed unexpectedly: {exc}")

        for ev in events:
            self.store.append(ev)
        self._record_decision(job_id, step, decision)
        return self.state(job_id)

    def run_until_blocked(self, job_id: str, max_steps: int = 50) -> JobState:
        """Advance until no step is runnable, a gate is reached, or a halt.

        Gates are not special-cased here: a step awaiting a human simply stops
        being runnable, so the engine stops with no separate notion of waiting.
        """
        state = self.state(job_id)
        for _ in range(max_steps):
            step = self.next_step(state)
            if step is None:
                return state
            new_state = self.advance(job_id)
            if new_state == state:
                return state
            state = new_state
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
