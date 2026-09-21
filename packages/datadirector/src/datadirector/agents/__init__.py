"""The agents, and the seam that lets the orchestration hold them by name.

An **agent** is a named capability: it receives an ``Invocation`` carrying a
narrowed ``JobHandle`` and returns an ``Outcome`` of events, gate items and
artefacts. It never holds the job, the store, the ledger or the file system —
it asks the handle for exactly what its declared capabilities justify.

This package imports concrete agent classes; **the registry never does**.
``registry.build_registry`` discovers agents by subclass inspection, so a new
agent lands in one folder and the orchestration layer never learns its class.
"""

from __future__ import annotations

from .base import (Agent, Authority, Capabilities, Instruction, Invocation,
                   Outcome)
from .registry import (AgentClasses, AgentContext, AgentRegistry,
                       build_registry, register_agent)

# The concrete agents, imported for their self-registration. Importing the
# package is what makes an agent known; nothing else imports these modules by
# name from the orchestration side.
from . import classification    # noqa: E401  registers ClassificationAgent
from . import declaration       # noqa: E401  registers DeclarationAgent
from . import documentation     # noqa: E401  registers DocumentationAgent
from . import dmp               # noqa: E401  registers DmpAgent
from . import ingestion         # noqa: E401  registers IngestionAgent
from . import media             # noqa: E401  registers MediaAgent
from . import metadata          # noqa: E401  registers MetadataAgent
from . import publication       # noqa: E401  registers PublicationAgent
from . import redaction         # noqa: E401  registers RedactionAgent
from . import repository        # noqa: E401  registers RepositoryAgent
from . import validation        # noqa: E401  registers ValidationAgent

__all__ = ["Agent", "AgentClasses", "AgentContext", "AgentRegistry",
           "Authority", "Capabilities", "Instruction", "Invocation",
           "Outcome", "build_registry", "register_agent"]
