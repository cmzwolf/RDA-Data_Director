"""The environment file the command line reads on its way in.

The credential rule is unchanged here: a secret reaches the system from the
environment and from nowhere else — not the wiring, not the policy, not an event
payload, not a provenance record. What this module adds is the half of the rule
that was missing. `.env` was documented as the place a developer puts a token,
in the README and in the credential broker's own error message, but nothing
opened the file. A filled-in `.env` therefore produced "environment variable
DD_ZENODO_TOKEN is not set, which scope 'zenodo:deposit' requires", sending the
developer back to the file that had just been ignored.

So the loader populates the environment and does nothing else. The broker still
resolves from ``os.environ`` alone; no component is handed a credential as a
value, and no value read here can be got back out of it. The result type carries
names and line numbers, because the thing that gets printed is the thing that
eventually gets leaked.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, MutableMapping

DEFAULT_ENV_FILE = ".env"

_SETTING = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(.*)$")
_QUOTES = ("'", '"')


@dataclass(frozen=True)
class EnvFileRead:
    """What consulting the environment file amounted to.

    Names and line numbers only. The values are deliberately absent: this object
    is what ``check`` prints and what a traceback could quote, and a credential
    reaching either is the leak this project exists to prevent.
    """

    path: Path | None = None
    loaded: tuple[str, ...] = ()
    already_set: tuple[str, ...] = ()
    unfilled: tuple[str, ...] = ()
    malformed: tuple[int, ...] = ()

    @property
    def present(self) -> bool:
        """Whether a file was there at all. Its absence is not a failure."""
        return self.path is not None

    def problems(self) -> list[str]:
        """What to tell the operator about the file, one line per problem."""
        if self.path is None or not self.malformed:
            return []
        joined = ", ".join(str(n) for n in self.malformed)
        return [f"{self.path}: line {joined} is not NAME=VALUE and was ignored. "
                 "The line is not repeated back to you, because a line that was "
                 "meant to be a credential is exactly what must not be echoed."]

    def summary(self) -> str:
        """Where the credential names in the environment came from, in one line."""
        if self.path is None:
            return f"none found (looked for {DEFAULT_ENV_FILE})"
        parts = [f"read {self.path}"]
        if self.loaded:
            parts.append("supplied " + ", ".join(self.loaded))
        if self.already_set:
            parts.append("left the shell's values for "
                          + ", ".join(self.already_set))
        if self.unfilled:
            parts.append("left unset, no value on the line: "
                          + ", ".join(self.unfilled))
        return "; ".join(parts)


def parse_env_lines(lines: Iterable[str]) -> tuple[list[tuple[str, str]],
                                                   list[int]]:
    """The settings in an environment file, and the lines that are not settings.

    Deliberately narrow, always in the direction of not surprising the reader: a
    line beginning with ``#`` is a comment; an ``export`` prefix is accepted,
    because the same file is sourced from a shell while a server is restarted; a
    ``#`` inside a value is not a comment, because tokens and URLs contain one and
    silently truncating a credential is the worst thing this parser could do; and
    a value wrapped in matching quotes has them removed, which is what someone
    writes when they believe the value needs them.
    """
    settings: list[tuple[str, str]] = []
    malformed: list[int] = []
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export"):].strip()
        match = _SETTING.match(line)
        if match is None:
            malformed.append(number)
            continue
        name, value = match.group(1), match.group(2).strip()
        if (len(value) >= 2 and value[0] in _QUOTES
                and value[-1] == value[0]):
            value = value[1:-1].strip()
        settings.append((name, value))
    return settings, malformed


def load_env_file(path: Path | str | None = None, *,
                   environ: MutableMapping[str, str] | None = None) -> EnvFileRead:
    """Put the file's settings into ``environ``, and say what was done.

    ``environ`` defaults to the real environment, which is what the command line
    needs; a test passes a dictionary so that it neither depends on nor disturbs
    the environment of the process running it.

    Two rules decide precedence, and both exist because the alternative fails
    quietly. The shell wins: a variable already in the environment is more
    specific than a file provided for convenience, so a one-off override in one
    shell keeps working. An empty line leaves the variable unset: ``TOKEN=`` in a
    template means nobody filled it in, and assigning an empty string would
    present an unfilled template as a credential that happens to be blank.
    Callers do distinguish the two, in the sign-in availability checks and in
    flag handling.
    """
    if environ is None:
        environ = os.environ
    candidate = Path(path) if path is not None else Path(DEFAULT_ENV_FILE)
    if not candidate.is_file():
        return EnvFileRead()

    settings, malformed = parse_env_lines(
        candidate.read_text(encoding="utf-8").splitlines())
    loaded: list[str] = []
    already_set: list[str] = []
    unfilled: list[str] = []
    for name, value in settings:
        if environ.get(name):
            already_set.append(name)
            continue
        if not value:
            unfilled.append(name)
            continue
        environ[name] = value
        loaded.append(name)
    return EnvFileRead(path=candidate, loaded=tuple(loaded),
                        already_set=tuple(already_set),
                        unfilled=tuple(unfilled), malformed=tuple(malformed))
