"""The credential broker.

Document A §6.4 and §10. Plugins request credentials by scope and never receive
a secret as a value. Secrets are read from the environment only; no credential
appears in a configuration file, an event payload, or a provenance record.
"""

from __future__ import annotations

import os
from typing import Mapping

from ..errors import CredentialError


class Secret:
    """A credential that refuses to print itself.

    `repr` and `str` are redacted deliberately: the realistic leak is not a
    developer logging a token on purpose, it is a token reaching a traceback or
    a debug print through an object that happened to contain it.
    """

    __slots__ = ("_value", "scope")

    def __init__(self, value: str, scope: str) -> None:
        self._value = value
        self.scope = scope

    def reveal(self) -> str:
        """The only way to obtain the value. Named to be greppable in review."""
        return self._value

    def __repr__(self) -> str:
        return f"<Secret scope={self.scope!r} value=REDACTED>"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)


class CredentialBroker:
    SERVES = ("C1",)
    """Resolves scopes to secrets held in the environment."""

    def __init__(self, scope_to_env: Mapping[str, str], environ: Mapping[str, str] | None = None) -> None:
        self._scope_to_env = dict(scope_to_env)
        self._environ = environ if environ is not None else os.environ

    def known_scopes(self) -> list[str]:
        return sorted(self._scope_to_env)

    def check_present(self) -> list[str]:
        """Which configured scopes have no value set.

        Called at startup so a missing credential is reported before a workflow
        begins rather than at the deposit step (C7).
        """
        return [s for s, var in self._scope_to_env.items() if not self._environ.get(var)]

    def get(self, scope: str) -> Secret:
        var = self._scope_to_env.get(scope)
        if var is None:
            raise CredentialError(
                f"no credential is configured for scope {scope!r}; "
                f"add it to credential_env_vars in the wiring configuration. "
                f"Configured scopes: {self.known_scopes()}"
            )
        value = self._environ.get(var)
        if not value:
            raise CredentialError(
                f"environment variable {var} is not set, which scope {scope!r} "
                "requires it. Put it in .env (a copy of .env.example, which is "
                     "git-ignored): the command reads that file into the "
                     "environment when it starts, so a value added to the file "
                     "afterwards is invisible to a process already running — "
                     "restart it."
            )
        return Secret(value, scope)
