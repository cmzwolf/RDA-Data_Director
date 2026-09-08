"""Agent scaffolding.

An agent owns one step of the workflow and has a contract; it may or may not
contain a model (ADR-009). Four of the nine agents use none at all, which is why
this base makes no assumption either way.

Every agent emits exactly one decision record per step, including deterministic
ones: C14 explainability does not distinguish between steps that used a model
and steps that did not.
"""

from __future__ import annotations

from datadirector_contracts import DecisionRecord, Event, EventKind, Orcid

from ..state.projection import JobState


class Agent:
    name: str = "agent"
    version: str = "0.1.0"

    @property
    def identity(self) -> str:
        return f"{self.name}/{self.version}"

    def runnable(self, state: JobState) -> bool:
        raise NotImplementedError

    def run(self, state: JobState) -> tuple[list[Event], DecisionRecord]:
        raise NotImplementedError

    def event(self, state: JobState, kind: EventKind, *,
              payload: dict | None = None, human: Orcid | None = None) -> Event:
        """Build an event attributed to this agent.

        `sequence` and `prev_digest` are placeholders: only the event store knows
        the current tail, and it sets both under its lock.
        """
        return Event(
            sequence=1, job_id=state.job_id, kind=kind,
            agent=self.identity, human=human, payload=payload or {},
        )
