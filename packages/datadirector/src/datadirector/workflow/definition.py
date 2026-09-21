"""The workflow this system actually performs, as a graph.

Every agent the registry holds appears here. That is the point: four agents
— declaration, media, redaction and publication — were built and called from
nowhere, and the flat step list had no way to say so. An agent absent from
this file is an orphan the reachability test will name, and a node naming an
agent the registry has never heard of is refused here, at build time, rather
than failing to run in silence.

Conditions are written as sentences as well as predicates, so the graph can
be drawn and read by someone who does not read Python.
"""

from __future__ import annotations

from datadirector_contracts import EventKind, SensitivityClass

from .graph import Condition, Edge, Node, WorkflowGraph


def _has(state, kind: EventKind) -> bool:
    return kind in getattr(state, "seen_kinds", set())


MATERIAL_RECEIVED = Condition(
    "material has been registered",
    lambda s: bool(getattr(s, "material", ())))

DECLARATION_CONFIRMED = Condition(
    "the depositor has confirmed their statement",
    lambda s: getattr(s, "classification", None) is not None)

HAS_IMAGES = Condition(
    "the submission contains images or audio",
    lambda s: any(str(p).lower().endswith(
        (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".webp",
        ".mp3", ".wav", ".flac", ".dcm"))
        for p in getattr(s, "material", ())))

IS_SENSITIVE = Condition(
    "the material was classified sensitive",
    lambda s: (getattr(s, "classification", None) is not None
                and s.classification.level >= SensitivityClass.SENSITIVE))

NOT_TERMINAL = Condition(
    "the job has not already concluded",
    lambda s: not getattr(s, "terminal", None))

STATEMENT_RECORDED = Condition(
    "the depositor has recorded their own statement",
    lambda s: bool(getattr(s, "has_statement", False)))

RECORD_DRAFTED = Condition(
    "a metadata record has been drafted",
    lambda s: _has(s, EventKind.METADATA_DRAFTED))

RECORD_VALIDATED = Condition(
    "the record has been validated",
    lambda s: _has(s, EventKind.VALIDATION_COMPLETED))


def build(agents=None) -> WorkflowGraph:
    """Assemble the graph, naming for each node the agent that runs it.

     `agents` is the registry — anything that answers `in` by name. The graph
    knows the shape and the names; the registry knows the work. An automatic
    node that names no registered agent is refused here: the mistake the old
    runner-closure design made invisible, made impossible."""
    nodes = [
        Node("ingest", "Take in the submission", agent="ingestion",
            serves=("R1",), produces=(EventKind.MATERIAL_REGISTERED,)),

        Node("dmp", "Read the data management plan", agent="dmp",
            serves=("R8",), produces=(EventKind.DMP_COMMITMENTS_READ,)),

        Node("declaration", "Your statement about the data",
            agent="declaration", serves=("C2", "P2"),
            produces=(EventKind.DECLARATION_PARSED,),
            requires=(STATEMENT_RECORDED,)),

        Node("confirm_declaration", "Confirm the proposed claims",
            human=True, serves=("P4",),
            produces=(EventKind.DECLARATION_CONFIRMED,)),

        Node("classification", "Work out what is sensitive",
            agent="classification", serves=("C2", "P3"),
            produces=(EventKind.CLASSIFICATION_COMPLETED,)),

        Node("media", "Inspect images and audio", agent="media",
            serves=("C2",)),

    Node("redaction", "Propose redactions", agent="redaction",
        serves=("C2",)),

        Node("metadata", "Draft metadata", agent="metadata",
            serves=("R2", "R3"), produces=(EventKind.METADATA_DRAFTED,)),

        Node("documentation", "Draft documentation",
            agent="documentation", serves=("R5",),
            produces=(EventKind.DOCUMENTATION_DRAFTED,),
            requires=(RECORD_DRAFTED,)),

        Node("validation", "Validate the record", agent="validation",
            serves=("R4", "C15"), produces=(EventKind.VALIDATION_COMPLETED,),
            requires=(RECORD_DRAFTED,)),

        Node("repository", "Choose where to deposit", agent="repository",
            serves=("R1",)),

        Node("review", "Your review", human=True, serves=("P4", "C13"),
            produces=(EventKind.REDACTION_DECIDED,)),

        Node("deposit", "Publish", human=True, agent="publication",
            serves=("R7", "R9"), produces=(EventKind.DEPOSIT_COMPLETED,),
            requires=(RECORD_VALIDATED,)),
    ]

    edges = [
        Edge(WorkflowGraph.ENTRY, "ingest"),
        Edge("ingest", "dmp", MATERIAL_RECEIVED),
        Edge("dmp", "declaration", MATERIAL_RECEIVED),
        Edge("declaration", "confirm_declaration"),

         # Nothing that reads the data runs before the depositor has said what
         # it is. That is the §9.3 ordering, and here it is an edge rather
         # than a convention someone must remember.
        Edge("confirm_declaration", "classification", DECLARATION_CONFIRMED),

         # Media inspection is conditional on there being media. A submission
         # of spreadsheets does not visit this node, and the phase display
         # reports it skipped rather than done.
        Edge("classification", "media", HAS_IMAGES),
        Edge("classification", "metadata", NOT_TERMINAL),
        Edge("media", "metadata", NOT_TERMINAL),

         # Redaction is proposed only where there is something to redact.
        Edge("classification", "redaction", IS_SENSITIVE),
        Edge("redaction", "review"),

        Edge("metadata", "documentation"),
        Edge("documentation", "validation"),
        Edge("validation", "repository"),
        Edge("repository", "review"),
        Edge("review", "deposit"),
    ]
    return WorkflowGraph(nodes, edges, agents)
