"""Sessions: turning an ORCID sign-in into something the API can resolve.

Until now `cmd_serve` raised rather than accept a token, deliberately: an API
that accepted anything would attribute every act to whoever called it, and every
provenance record naming a human would be a lie. This replaces the refusal.

**The cookie holds an identifier and nothing else.** A self-describing token —
a signed JWT carrying the ORCID and the roles — would put identity in the
client's hands, and identity is what every accountability claim in this system
rests on. Server-side sessions also make revocation immediate rather than a
matter of waiting for an expiry.

There is deliberately **no development mode that mints a session** without an
ORCID exchange. Such a mode is always still present in production.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from datadirector_contracts import AccessRole, Orcid
from pydantic import BaseModel, ConfigDict, Field

COOKIE_NAME = "dd_session"
DEFAULT_LIFETIME_HOURS = 12


class Session(BaseModel):
    model_config = ConfigDict(frozen=True)

    identifier: str
    orcid: Orcid
    roles: list[AccessRole] = Field(default_factory=list)
    issued_at: datetime
    expires_at: datetime
    authentication: str = Field(
        default="orcid",
        description="How this identity was established. 'local-accounts' means "
        "it was asserted rather than proven, and every record made under the "
        "session should say so: provenance from a development run must stay "
        "distinguishable from a real researcher's work.")

    def is_valid(self, *, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) < self.expires_at


class SessionStore:
    """Sessions on disk, so they survive a restart of a single-user deployment.

    Restarting the service should not sign everyone out; on a laptop that is the
    difference between a tool and an irritation.
    """

    def __init__(self, path: Path | str, *,
                 lifetime_hours: int = DEFAULT_LIFETIME_HOURS) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lifetime = timedelta(hours=lifetime_hours)

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def create(self, orcid: Orcid, *, roles: list[AccessRole] | None = None,
               authentication: str = "orcid",
               now: datetime | None = None) -> Session:
        """Issue a session for an ORCID that has just been proven.

        The ORCID comes from the identity provider's exchange and is never
        supplied by the client, which is the whole point of the flow.
        """
        now = now or datetime.now(timezone.utc)
        session = Session(
            identifier=secrets.token_urlsafe(32), orcid=orcid,
            roles=list(roles or []), issued_at=now,
            authentication=authentication,
            expires_at=now + self.lifetime)
        data = self._load()
        data[session.identifier] = json.loads(session.model_dump_json())
        self._save(data)
        return session

    def resolve(self, identifier: str | None, *,
                now: datetime | None = None) -> Session | None:
        """Return the session, or nothing.

        An expired or unknown identifier resolves to nothing, never to a default
        user. Expiry is checked on read rather than swept periodically, because
        a sweep that has not run yet is a session that is still valid.
        """
        if not identifier:
            return None
        raw = self._load().get(identifier)
        if raw is None:
            return None
        session = Session.model_validate(raw)
        if not session.is_valid(now=now):
            self.revoke(identifier)
            return None
        return session

    def revoke(self, identifier: str) -> None:
        """End a session immediately, not at its expiry."""
        data = self._load()
        if data.pop(identifier, None) is not None:
            self._save(data)

    def revoke_all_for(self, orcid: Orcid) -> int:
        """Sign a person out everywhere. Used when a delegation is withdrawn."""
        data = self._load()
        doomed = [k for k, v in data.items() if v.get("orcid", {}).get("value")
                  == orcid.value]
        for key in doomed:
            data.pop(key)
        if doomed:
            self._save(data)
        return len(doomed)
