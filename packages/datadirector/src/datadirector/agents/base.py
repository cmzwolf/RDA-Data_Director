"""The agent identity and the invoked form, shared by every agent.

The fields exist because decisions quote the agent that made them, and a
decision quoted from an unknown agent is unauditable. The identity is attached
to every event an agent emits and to every decision it records, so "who did
this" is always answerable — which is also what makes the registry's
deductions possible.

The invoked form is uniform: an agent is handed an `Invocation` and returns an
`Outcome`, and it never sees a job id, a filesystem path or an event. It works
on the job through the `JobHandle`'s narrow, named views, and everything it
would like to assert reaches the log through the orchestrator, which is the
only thing that appends.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from datadirector_contracts import (
    DecisionRecord,
    Event,
    EventKind,
    GateItem,
    ModelCapability,
    Orcid,
    SensitivityClass,
)

from ..workflow.graph import Condition
if TYPE_CHECKING:
    from ..job_handle import JobHandle


class Agent:
    """Convenience base for agents. The contract is `AgentProtocol` below, not
    this class: anything that answers to a name, states its capabilities and
    runs an invocation satisfies it, whether or not it inherits from here.

    `Capabilities.requires` and `Capabilities.establishes` hold `Condition`
    objects from the graph module, which is why that module imports nothing
    from here.
    """

    name: str = ""
    version: str = "0.1.0"
    serves: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        return f"{self.name}/{self.version}"

    def event(self, state, kind: EventKind, *, human: str | None = None,
            payload: dict | None = None) -> Event:
        """Build an event carrying this agent's identity.

        ``human`` is the authenticated on-behalf-of identity, recorded by the
        entry point that accepted it rather than by the caller's prose, so the
        attribution cannot be forged by a prompt. An agent's own events carry
        its identity and never a human: a person's acts enter the log at the
        entry point that authenticated them.
        """
        return Event(
            sequence=1,
            job_id=state.job_id,
            kind=kind,
            agent=self.identity,
            human=human,
            payload=payload or {},
        )


class Authority(StrEnum):
    """Where an instruction came from, which is decided by the *entry point*
    that accepted it and never by the text. The authenticated depositor
    directs; submitted material informs."""

    DEPOSITOR = "authenticated-depositor"
    OPERATOR = "authenticated-operator"
    COMPOSER = "composed-by-agent"


@dataclass(frozen=True)
class Instruction:
    """One sentence of intent, carried verbatim with its provenance attached
    at the boundary. An agent acts on it only when the author is the
    depositor; the same words arriving from any other channel carry no
    authority."""

    text: str
    author: Authority = Authority.DEPOSITOR
    # The sequence of the event this was recorded from, so an agent can show
    # the depositor the log entry its reading came from.

      # The authenticated person standing behind the instruction, when
      # there is one. Publishing borrows no authority: a deposit agent
      # refuses an invocation that brings no named human.
    actor: Orcid | None = None
    recorded: int | None = None
      # What a reviewer said about the agent's previous draft, when the agent
      # is being asked again. Kept separate from `text` rather than appended to
      # it: the depositor's instruction is quoted verbatim and is nobody's
      # editing target, while this is one named person's criticism of one
      # particular draft. An agent that conflates the two lets a review comment
      # quietly become the instruction for the rest of the job.
    review_note: str | None = None


@dataclass(frozen=True)
class Invocation:
    """How an agent is entered. The job itself is not a parameter: the agent
    receives a handle narrowed to its own capabilities."""

    job: "JobHandle"
    instruction: Instruction | None = None

@dataclass
class Outcome:
    """What an agent returns. It does not append, mark, resolve or select — it
    reports what happened and what it would like to assert, and the
    orchestrator is the only thing that writes to the log."""

    job_id: str
    events: list[Event] = field(default_factory=list)
    # Assertions the agent wants on the record, held until a person resolves
    # them. They are the agent's, and they say so.
    gate_items: list[GateItem] = field(default_factory=list)
    # The model's own prose, pending a named person's validation. The
    # orchestrator turns this into a gate item and refuses to run anything
    # downstream of the agent until that person has had their say: an abstract
    # the machine invented and an abstract the depositor supplied are the same
    # shape on the log, and only a reader can tell them apart. `None` says
    # there was no model output to validate, which is a different fact from a
    # person having approved one.
    review: dict | None = None
    # What a caller outside the system needs back, when it needs anything.
    result: object | None = None
    # Names of artefacts produced, reported not written.
    artefacts: list[str] = field(default_factory=list)
    # For the trace and the operator, never for the log.
    message: str = ""
    # Why the agent chose what it chose, recorded by the engine alongside the
    # events, and refused by the recorder when it is missing.
    decision: DecisionRecord | None = None


@dataclass(frozen=True)
class LlmOutput:
    """What one agent's model wrote, and what a person may do about it.

    An agent that asks a model for prose has produced an assertion, not a
    finding: the stages behind it cannot tell invented detail from supplied
    detail, and each hop makes the draft look more settled. Declaring this
    makes the orchestrator hold the output at the gate for a named person.

    `label` is what the model wrote, in the words a person reads on the
    review screen. `produces` names the event kinds that carry it, so a later
    reader can check that what was approved is what is still there.
    `editable` says whether a person can write their own version here — False
    is not an inconvenience but a refusal to lie, as with a classification
    level, where the projection only ever tightens a sensitivity view and an
    "approved" loosening would mean nothing. `edit_note` carries why, in the
    screen's voice, so the absence of a button reads as a decision rather
    than an oversight.
    """

    label: str
    produces: tuple[EventKind, ...] = ()
    editable: bool = True
    edit_note: str = ""


@dataclass(frozen=True)
class Capabilities:
    """What an agent states it can do, as data a person can read and the
    orchestrator can consume. Intent stated in English is not verified — the
    registry refuses an agent that cannot answer to its name, and the graph
    refuses one nothing leads to; the correspondence between a method and a
    sentence stays a claim, and claiming is what an agent is entitled to do."""

    # The name the agent answers to, and the registry keys it by.
    name: str
    # One sentence a person can read.
    summary: str
    # Conditions that must hold on the job before the agent may run.
    requires: tuple[Condition, ...] = ()
    # Conditions the agent brings about when it runs.
    establishes: tuple[Condition, ...] = ()
    # The highest classification the agent may be handed material at.
    sensitivity_ceiling: SensitivityClass = SensitivityClass.SENSITIVE
    # The capability needed from the backend registry, when it needs a model.
    needs_backend: ModelCapability | None = None
    # The model's own prose, when the agent produces any, and what a person
    # may do about it. Declared here because it belongs beside `needs_backend`:
    # an agent that asks a model for prose is an agent whose prose is somebody's
    # assertion. `human_follows` was declared by nine agents and read by
    # nothing, which is the same defect as a conformance row naming a component
    # that cannot be reached. This one is read: the orchestrator raises the
    # review item from it and refuses to run anything downstream until a named
    # person has validated, rewritten or replaced what the model wrote.
    #
    # Agents whose output is already put to a person item by item — a redaction
    # proposal, a proposed claim — declare nothing here. A second, coarser gate
    # over individually-reviewed items would not add review; it would add a
    # second thing to approve.
    llm_output: LlmOutput | None = None
    # Whether the agent reads submitted material at all. Everything down-
    # stream of classification sets this; the agents upstream of it must not.
    inspects_material: bool = False
    # Whether a person must confirm the agent's output before it takes effect.
    human_follows: bool = False
    # Which §11.2 requirements it claims to serve. Read by the registry to
    # deduce the coverage report; no Python analysis establishes that a class
    # satisfies a sentence of English, which is why the report is evidence,
    # not a guarantee.
    serves: tuple[str, ...] = ()


@runtime_checkable
class AgentProtocol(Protocol):
    """The one interface every agent satisfies. The engine and the
    orchestrator may hold only an `AgentProtocol`, never a concrete agent
    class: the agent decides how to gather what it needs, which is the point
    of the seam."""

    name: str

    def capabilities(self) -> Capabilities: ...

    def run(self, invocation: Invocation) -> Outcome: ...

