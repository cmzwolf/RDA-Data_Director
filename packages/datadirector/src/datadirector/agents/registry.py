"""The registry: keyed access, self-registration, and the coverage report.

An agent registers itself — its class, and a builder that says how to make one
from what the deployment provides. The registry is what refuses to run an
agent that cannot answer to its own name, and it is what the coverage report
reads. It never imports an agent module, never instantiates a concrete class,
and never calls a method beyond the interface: everything specific to an agent
stays in the agent's own file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from .base import AgentProtocol, Capabilities


class UnregisteredAgent(KeyError):
    """The registry holds nothing under that name. Raised with the names it
    does hold, so the failure is legible rather than a bare key error."""


class AgentRegistry:
    """Holds the agent instances the orchestrator dispatches to, keyed by the
    name each agent registered itself under."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentProtocol] = {}

    def register(self, agent: object) -> None:
        """Accept an agent instance, refusing anything the orchestrator could
        not dispatch to. The refusal is the registry's real work: an agent that
        cannot answer to its own name, or state its capabilities, would fail
        later at the point where it was the only thing that could have gone
        wrong."""
        name = getattr(agent, "name", None)
        if not isinstance(name, str) or not name:
            raise TypeError(
                f"an agent must answer to a name, not {type(agent).__name__}")
        if not callable(getattr(agent, "run", None)):
            raise TypeError(f"agent {name!r} has no run() to invoke")
        capabilities = getattr(agent, "capabilities", None)
        capabilities = capabilities() if callable(capabilities) \
            else capabilities
        if not isinstance(capabilities, Capabilities):
            raise TypeError(f"agent {name!r} states no capabilities")
        if capabilities.name != name:
            raise TypeError(f"agent registered as {name!r} answers to "
                            f"{capabilities.name!r} in its capabilities")
        if name in self._agents:
            raise TypeError(f"an agent is already registered as {name!r}")
        self._agents[name] = agent  # type: ignore[assignment]

    def get(self, name: str) -> AgentProtocol:
        """Look one agent up by name."""
        agent = self._agents.get(name)
        if agent is None:
            raise UnregisteredAgent(
                f"no agent registered as {name!r}; registered: "
                f"{', '.join(sorted(self._agents)) or 'none'}")
        return agent

    def all(self) -> list[AgentProtocol]:
        """Every registered instance, in registration order."""
        return list(self._agents.values())

    def capabilities(self) -> dict[str, Capabilities]:
        """Every stated capability summary, keyed by name."""
        out: dict[str, Capabilities] = {}
        for name, agent in self._agents.items():
            capabilities = getattr(agent, "capabilities", None)
            out[name] = capabilities() if callable(capabilities) \
                else capabilities
        return out

    def __contains__(self, name: object) -> bool:
        return name in self._agents

    def __len__(self) -> int:
        return len(self._agents)


class AgentClasses:
    """Where an agent class registers itself at import time, with a builder
    that says how to make one from what the deployment provides. The registry
    builds instances from these; it never imports an agent module and never
    knows a concrete class."""

    _classes: dict[str, type] = {}

    @classmethod
    def register(cls, agent_class: type) -> type:
        name = getattr(agent_class, "name", None)
        if not isinstance(name, str) or not name:
            raise TypeError(f"agent class {agent_class.__name__} has no name")
        if not callable(getattr(agent_class, "build", None)):
            raise TypeError(f"agent class {agent_class.__name__} has no "
                            "build() to construct it from the deployment")
        cls._classes[name] = agent_class
        return agent_class

    @classmethod
    def classes(cls) -> dict[str, type]:
        return dict(cls._classes)


def register_agent(agent_class: type) -> type:
    """Class decorator: an agent module registers its own class with it. This
    is the half the registry may legitimately know — a class telling the
    registry it exists, not the registry knowing the class."""
    return AgentClasses.register(agent_class)

@dataclass
class AgentContext:
    """What the deployment provides, handed to each agent's ``build`` so an
    agent class states how to construct itself from it and the registry never
    names a concrete class. Every field is optional: an agent that needs one
    it did not receive raises when it runs, not when it is built.

    The per-job pieces arrive as *factories*, not values — the classification
    probe set depends on the job's material, and a job's plan source depends
    on where its copy of the plan was staged — because a registry instance is
    a blueprint, and what varies per job must not be baked into it."""

    # The backend registry every model call is resolved through.
    pep: Any = None
    # Where job working directories live.
    working_root: Any = Path("var/working")
    # Where events go.
    store: Any = None
    # Who asked for what, from the authenticated session at the boundary.
    actor: Any = None
    # The decision recorder, which refuses a decision with no reason.
    recorder: Any = None
    # The interaction ledger, which records what a model saw.
    ledger: Any = None
    # The controlled vocabulary the metadata agent is bound to.
    vocabulary: Any = None
    # The repository registry the repository agent shortlists from.
    re3data: Any = None
    # The driver the publication agent deposits through, when configured.
    repository_driver: Any = None
    # The name of that driver, for the record.
    repository_name: str | None = None
    # The schema profile validation defaults to.
    schema_profile: Any = None
    # Every selectable profile, keyed by standard name.
    profiles: dict[str, Any] = field(default_factory=dict)
    # The validator the validation agent runs.
    validator: Any = None
    # The default metadata standard when nothing directs otherwise.
    default_standard: str = "datacite"
    # (job_id, staged_path) -> the DmpSource for that job's copy.
    dmp_source_factory: Callable[[str, Any], Any] | None = None
    # (job_id, profile) -> the probe executor for that job.
    probe_executor_factory: Callable[[str, Any], Any] | None = None
    # The highest classification material may reach, from the deployment.
    ceiling: Any = None

      # The deployment's answer to "which profile validates this
        # standard".
    profile_for: Callable | None = None
       # The plan source an agent gets when the deployment has one and the
       # job has not said where the plan is.
    default_plan_source: Any = None
       # Whether the declaration agent may infer sensitivity, from policy.
    infer_sensitivity: bool = True


def build_registry(context: AgentContext) -> AgentRegistry:
    """Construct the deployment's registry from the agent classes that
    registered themselves. This is the only place a class becomes an instance,
    and it knows no class at all: it iterates what the classes gave it."""
    registry = AgentRegistry()
    for name, agent_class in AgentClasses.classes().items():
        registry.register(agent_class.build(context))
    return registry

