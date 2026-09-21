"""The workflow as a graph of nodes and conditional edges.

Previously the workflow was an ordered list of steps, each with a predicate, and
the engine ran the first whose predicate held. That is a graph too, but an
implicit one: the topology lived inside lambdas and could not be read, drawn or
checked. Four agents were constructed and called from nowhere, and nothing
noticed, because "is this step in the list" was a question no test asked.

Here the topology is data. Each node declares what it needs before it can run,
what it produces, and which requirements it serves; each edge declares the
condition under which one node leads to another, in words as well as in code.

Three things follow that the list could not give.

**Orphans are detectable.** A node with no path from the entry point is
unreachable, and that is a property of the graph rather than something a person
must notice. It is the failure that prompted this.

**Conformance becomes a deduction.** A requirement is served when a node
claiming it is reachable — not when a class declares it. The declaration is
still a human judgement, because no analysis of Python establishes that a class
satisfies a sentence of English; what is deduced is whether the claim is
connected to anything.

**A graph is not a proof.** Reachability is necessary and not sufficient: a node
reachable in the topology may still never run, because its condition depends on
data no submission provides. Static reachability says a path exists, not that
anyone walks it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from datadirector_contracts import EventKind


@dataclass(frozen=True)
class Condition:
    """A predicate with a sentence explaining it.

    The sentence is not decoration: an edge whose condition can only be read as
    code cannot be drawn, explained to a researcher, or reviewed by anyone who
    does not read Python.
    """

    description: str
    holds: Callable[[object], bool]

    def __call__(self, state) -> bool:
        return self.holds(state)


ALWAYS = Condition("always", lambda _state: True)


@dataclass(frozen=True)
class Node:
    """One step, and what it claims about itself.

    A node names the agent that runs it rather than holding a closure: the
    pipeline used to hand each node a `run` function, so the topology and the
    work could only be assembled together, and a node whose runner was never
    supplied quietly became a node that could never run — the failure this
    file exists to catch. Naming an agent, and having the graph refuse an
    unregistered name at build time, moves that mistake from runtime silence
    to build-time noise."""

    name: str
    label: str
    # The agent that runs this node. When absent, the node is keyed by its
    # own name; a human-only node may carry no agent at all, because what
    # happens there is the person's act, recorded by the service.
    agent: str | None = None
    serves: tuple[str, ...] = ()
    produces: tuple[EventKind, ...] = ()
    # Conditions this node needs beyond its incoming edges, stated in words
    # and in code, and asked of the projected state before dispatch.
    requires: tuple[Condition, ...] = ()
    human: bool = field(
        default=False,
        metadata={"why": "A node a person must act on. Never auto-run: the "
                         "engine stops here and says what is wanted."})

    @property
    def automatic(self) -> bool:
        return not self.human

    def __call__(self, state) -> bool:
        """Whether this node's own conditions hold on the state."""
        return all(condition(state) for condition in self.requires)


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    when: Condition = ALWAYS


class WorkflowGraph:
    ENTRY = "start"

    def __init__(self, nodes: list[Node], edges: list[Edge],
                 agents=None) -> None:
        self.nodes = {node.name: node for node in nodes}
        self.edges = list(edges)
        self._validate()
        self._require_registered(agents)

    def _validate(self) -> None:
        """Refuse a graph that cannot be walked.

        An edge to a node that does not exist is a typo that would otherwise
        become a silently unreachable branch — the failure mode this file was
        written to remove, reintroduced through the mechanism meant to
        prevent it.
        """
        for edge in self.edges:
            for end in (edge.source, edge.target):
                if end not in self.nodes and end != self.ENTRY:
                    raise ValueError(
                        f"edge {edge.source} -> {edge.target} names {end!r}, "
                        "which is not a node in this graph")

    def _require_registered(self, agents) -> None:
        """Refuse a node that names no registered agent.

        The check belongs here rather than at dispatch, because dispatch is
        where an unregistered name used to surface as a missing `run`
        function — that is, as a node that simply never ran. An automatic
        node must name something the registry can produce; a human node may
        name one (deposit names publication) or name nothing, because what
        happens there is the person's own act.

        `agents` is anything that answers `in` by name: the registry or a
        plain mapping. A graph built without one is drawn, not run — the
        diagram does not need the registry. An empty registry means the same
        thing: no deployment has spoken, so this graph is a shape under test,
        not a job under engine.
        """
        if not agents:
            return

        for node in self.nodes.values():
            if node.human and node.agent is None:
                continue
            key = node.agent or node.name
            if key not in agents:
                raise ValueError(
                    f"node {node.name!r} names no registered agent: {key!r}")

    # -- topology ---------------------------------------------------------

    def successors(self, name: str) -> list[Edge]:
        return [e for e in self.edges if e.source == name]

    def reachable(self) -> set[str]:
        """Nodes with a path from the entry point, ignoring conditions.

        Conditions are ignored deliberately: a node whose condition is never
        satisfied by any real submission is a different problem, and one no
        static analysis can settle.
        """
        seen: set[str] = set()
        stack = [self.ENTRY]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack += [e.target for e in self.successors(current)]
        return seen - {self.ENTRY}

    def orphans(self) -> list[str]:
        """Nodes nothing leads to. The bug this graph exists to make visible."""
        return sorted(set(self.nodes) - self.reachable())

    def serves(self) -> dict[str, list[str]]:
        """Requirements served by nodes that are actually reachable.

        A requirement claimed only by an orphan is not served by this workflow,
        whatever the class says about itself.
        """
        reachable = self.reachable()
        out: dict[str, list[str]] = {}
        for name in sorted(reachable):
            for requirement in self.nodes[name].serves:
                out.setdefault(requirement, []).append(name)
        return out

    def claimed_but_unreachable(self) -> dict[str, list[str]]:
        """Requirements claimed only by orphans: a promise nothing keeps."""
        reachable = self.serves()
        out: dict[str, list[str]] = {}
        for name in self.orphans():
            for requirement in self.nodes[name].serves:
                if requirement not in reachable:
                    out.setdefault(requirement, []).append(name)
        return out

    # -- walking ----------------------------------------------------------

    def next_node(self, state, *, done: set[str] | None = None) -> Node | None:
        """The next node whose incoming condition holds.

        Walked from the entry rather than scanned as a list, so the order is
        a property of the topology and an edge added in the wrong place is
        visible as a wrong path rather than as a mysterious ordering. A node
        whose own conditions do not hold is not taken: the walk continues
        past it, because a node that cannot run yet is not a node that stops
        the workflow."""
        done = done or set()
        frontier = [self.ENTRY]
        seen: set[str] = set()
        while frontier:
            current = frontier.pop(0)
            if current in seen:
                continue
            seen.add(current)
            for edge in self.successors(current):
                if not edge.when(state):
                    continue
                node = self.nodes[edge.target]
                if (edge.target not in done and node.automatic
                        and all(condition(state)
                                for condition in node.requires)):
                    return node
                frontier.append(edge.target)
        return None

    def blocking_human_node(self, state, *, done: set[str] | None = None
                            ) -> Node | None:
        """The node a person must act on before anything else can run."""
        done = done or set()
        frontier = [self.ENTRY]
        seen: set[str] = set()
        while frontier:
            current = frontier.pop(0)
            if current in seen:
                continue
            seen.add(current)
            for edge in self.successors(current):
                if not edge.when(state):
                    continue
                node = self.nodes[edge.target]
                if node.human and edge.target not in done:
                    return node
                frontier.append(edge.target)
        return None

    def to_mermaid(self) -> str:
        """The graph as a diagram.

        Rendered rather than drawn by hand, so a picture in a paper cannot
        describe a workflow the software does not have.
        """
        lines = ["flowchart TD", f"  {self.ENTRY}([start])"]
        for name, node in self.nodes.items():
            shape = f"{name}[{node.label}]" if node.automatic \
                else f"{name}{{{{{node.label}}}}}"
            lines.append(f"  {shape}")
        for edge in self.edges:
            label = "" if edge.when is ALWAYS else f"|{edge.when.description}|"
            lines.append(f"  {edge.source} -->{label} {edge.target}")
        return "\n".join(lines)
