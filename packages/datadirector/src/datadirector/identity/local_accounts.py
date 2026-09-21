"""Local accounts: a file of identifiers and passwords, for development.

ORCID sign-in requires a registered application and, in production, HTTPS. That
is the right thing before a deployment goes live and the wrong thing when the
workflow itself is what needs debugging: configuring a tunnel in order to try an
ingestion is backwards.

So a deployment may instead read accounts from a file. Several people can sign
in, each as a distinct identifier, which is what the workflow needs in order to
be exercised at all — ownership, delegation, the auditor role.

**These identities are asserted, not proven**, and the system says so
everywhere it matters: the session records it, the landing page says it, and
every event written under such a session carries `authentication:
local-accounts`. That last part is not a security measure — the file is on
someone's own machine and the threat model there is nobody. It is bookkeeping:
provenance records from a development run must be distinguishable later from
records made by a real researcher, or the archive quietly acquires fiction.

Refused outside the single-user and institutional-development profiles, because
a shared instance where passwords live in a text file is a different thing
entirely.
"""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from datadirector_contracts import AccessRole, Orcid
from pydantic import BaseModel, ConfigDict, Field

# ORCID's own MOD 11-2 checksum applies to these too: the identifier type
# validates it, so a made-up number must still be well formed. `mint` below
# produces valid ones.
ACCOUNTS_ENV = "DD_LOCAL_ACCOUNTS"


class LocalAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    orcid: Orcid
    password: str
    roles: list[AccessRole] = Field(default_factory=list)
    label: str | None = Field(
        default=None,
        description="A human name for the file's own readability. Never used "
        "as an identifier: the ORCID is.")


class LocalAccounts:
    """Accounts read from a file.

    Format, one per line, `#` for comments:

        0000-0002-1825-0097:hunter2:Josiah Carberry
        0000-0001-5109-3700:hunter2:Auditor:auditor

    Fields are identifier, password, optional label, optional roles.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, LocalAccount]:
        if not self.path.exists():
            return {}
        accounts: dict[str, LocalAccount] = {}
        for number, raw in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(":")]
            if len(parts) < 2 or not parts[1]:
                raise ValueError(
                    f"{self.path}:{number}: expected "
                    "identifier:password[:label[:roles]]")
            try:
                orcid = Orcid(value=parts[0])
            except ValueError as exc:
                raise ValueError(f"{self.path}:{number}: {exc}") from None
            roles = []
            if len(parts) > 3 and parts[3]:
                for name in parts[3].split(","):
                    try:
                        roles.append(AccessRole(name.strip()))
                    except ValueError:
                        raise ValueError(
                            f"{self.path}:{number}: {name.strip()!r} is not a "
                            f"role; expected one of "
                            f"{[r.value for r in AccessRole]}") from None
            accounts[orcid.value] = LocalAccount(
                orcid=orcid, password=parts[1],
                label=parts[2] if len(parts) > 2 and parts[2] else None,
                roles=roles)
        return accounts

    def authenticate(self, identifier: str, password: str
                     ) -> LocalAccount | None:
        """Check a password. Constant-time, out of habit rather than need.

        Returns nothing on failure, and does not distinguish an unknown
        identifier from a wrong password — not because an attacker is expected,
        but because a login form that says "no such user" teaches its operator
        to expect that distinction elsewhere.
        """
        account = self.load().get(identifier.strip())
        if account is None:
            # Compared anyway, so the timing does not reveal which failed.
            hmac.compare_digest(secrets.token_hex(16), password)
            return None
        if not hmac.compare_digest(account.password, password):
            return None
        return account


def checksum(base: str) -> str:
    """ORCID's MOD 11-2 check digit, so a made-up identifier is well formed.

    The identifier type validates the checksum, and rightly: an identifier that
    cannot be one is a typo waiting to be filed as a person.
    """
    total = 0
    for digit in base.replace("-", ""):
        total = (total + int(digit)) * 2
    remainder = total % 11
    result = (12 - remainder) % 11
    return "X" if result == 10 else str(result)


def mint(seed: int) -> Orcid:
    """A well-formed identifier for local use.

    Deliberately in the 0009 block, which ORCID has issued into, so these are
    *not* guaranteed unassigned. They are local fictions and must never reach a
    real deposit; the session and the event record both say so.
    """
    # An ORCID is fifteen digits plus a check digit. An earlier version built
    # the body and then stripped its last character to make room for the check,
    # which threw away the seed and produced the same identifier every time.
    digits = ("0009" + f"{seed:011d}")[:15]
    body = f"{digits[0:4]}-{digits[4:8]}-{digits[8:12]}-{digits[12:15]}"
    return Orcid(value=body + checksum(body))


def write_example(path: Path | str, count: int = 3) -> Path:
    """Create a starter file with well-formed identifiers."""
    path = Path(path)
    lines = [
        "# Local development accounts. identifier:password[:label[:roles]]",
        "#",
        "# These identities are asserted, not proven. Every event written under",
        "# them records authentication: local-accounts, so a development run is",
        "# distinguishable from a real one in the provenance record.",
        "#",
        "# Not for a shared machine, and refused outside the local and",
        "# development profiles.",
        "",
    ]
    roles = ["", "", "auditor"]
    labels = ["Researcher", "Colleague", "Auditor"]
    for index in range(count):
        orcid = mint(index + 1)
        suffix = f":{roles[index]}" if index < len(roles) and roles[index] else ""
        label = labels[index] if index < len(labels) else f"User {index + 1}"
        lines.append(f"{orcid.value}:password:{label}{suffix}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
