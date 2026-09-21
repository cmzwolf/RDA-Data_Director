"""Sign-in routes, and the principal resolver the API already expects.

The core API takes a `resolve_principal` callable and has since it was written.
This supplies a real one, so nothing about the API's shape changes.
"""

from __future__ import annotations

from typing import Iterable

from datadirector_contracts import AccessRole, Orcid

from ..errors import AuthorityError
from ..identity.session import COOKIE_NAME, Session, SessionStore


class SessionResolver:
    """Resolves a cookie or bearer value to an ORCID and its roles.

    Accepts both forms because the same service answers a browser and another
    Data Director. The value means the same thing in each: a session identifier
    that the server looks up, never a claim the client makes about itself.
    """

    def __init__(self, store: SessionStore,
                 role_grants: dict[str, Iterable[AccessRole]] | None = None) -> None:
        self.store = store
        # Roles are granted per ORCID by the deployment, not by the identity
        # provider: ORCID says who someone is, an institution says what they may
        # do here (§10).
        self.role_grants = {k: set(v) for k, v in (role_grants or {}).items()}

    def session_for(self, credential: str | None) -> Session | None:
        return self.store.resolve(_identifier(credential))

    def __call__(self, credential: str | None) -> Orcid:
        session = self.session_for(credential)
        if session is None:
            raise AuthorityError("no valid session")
        return session.orcid

    def roles_for(self, credential: str | None) -> set[AccessRole]:
        session = self.session_for(credential)
        if session is None:
            return set()
        granted = self.role_grants.get(session.orcid.value, set())
        return set(session.roles) | set(granted)


def _identifier(credential: str | None) -> str | None:
    """Extract the session identifier from a header or cookie value."""
    if not credential:
        return None
    value = credential.strip()
    for prefix in ("Bearer ", "bearer "):
        if value.startswith(prefix):
            return value[len(prefix):].strip()
    if f"{COOKIE_NAME}=" in value:
        for part in value.split(";"):
            part = part.strip()
            if part.startswith(f"{COOKIE_NAME}="):
                return part[len(COOKIE_NAME) + 1:]
    return value


def cookie_parameters(*, secure: bool) -> dict:
    """Cookie attributes.

    `HttpOnly` because no script needs the session identifier; `SameSite=Lax`
    because the only cross-site arrival is the ORCID callback, which is a
    top-level navigation; `Secure` everywhere but a local deployment, where
    requiring HTTPS on a laptop would only teach people to disable it.
    """
    return {"key": COOKIE_NAME, "httponly": True, "samesite": "lax",
            "secure": secure, "path": "/"}
