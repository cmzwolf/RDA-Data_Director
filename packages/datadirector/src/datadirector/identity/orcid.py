"""ORCID as an identity provider.

Cluster 4 Part C1. Identity only, never authorisation.

Affiliation recorded in an ORCID profile is self-asserted: a researcher types it
in themselves and nobody verifies it. It is evidence about a person, not a claim
an institution has made about them, so it cannot ground a decision about what
they may do here (§10). Roles are held locally.

The authorisation code flow is used rather than an implicit or device flow
because the token exchange happens server-side and the client secret never
reaches a browser.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlencode

import httpx
from datadirector_contracts import CapabilityManifest, Orcid

from ..credentials.broker import CredentialBroker
from ..errors import AuthorityError, ExternalServiceError

SANDBOX = "https://sandbox.orcid.org"
PRODUCTION = "https://orcid.org"


class OrcidIdentityProvider:
    SERVES = ("R11",)
    """OAuth 2.0 authorisation code flow against ORCID."""

    def __init__(self, client_id: str, broker: CredentialBroker,
                 redirect_uri: str, *, base_url: str = SANDBOX,
                 scope: str = "orcid:client-secret", timeout: float = 30.0) -> None:
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.base_url = base_url.rstrip("/")
        self._broker = broker
        self._scope = scope
        self.timeout = timeout
        # State values issued but not yet redeemed. Held in memory deliberately:
        # a state that survives a restart is a state an attacker has had longer
        # to replay, and losing them costs a user one repeated sign-in.
        self._pending: set[str] = set()

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="identity-orcid", version="0.1.0", protocol="IdentityProvider",
            requirements_supported=list(self.SERVES), offline_capable=False,
            requires_network=True,
        )

    def new_state(self) -> str:
        """A single-use CSRF state parameter."""
        state = secrets.token_urlsafe(24)
        self._pending.add(state)
        return state

    def authorize_url(self, state: str) -> str:
        return f"{self.base_url}/oauth/authorize?" + urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "scope": "/authenticate",
            "redirect_uri": self.redirect_uri,
            "state": state,
        })

    def exchange(self, code: str, *, state: str | None = None) -> Orcid:
        """Exchange an authorisation code for the user's ORCID.

        The state is consumed on use. An unrecognised or reused state is refused
        rather than warned about: a redemption we cannot tie to a request we
        issued is exactly what a cross-site attack looks like.
        """
        if state is not None:
            if state not in self._pending:
                raise AuthorityError(
                    "the authorisation state is unknown or has already been "
                    "used; sign in again"
                )
            self._pending.discard(state)

        secret = self._broker.get(self._scope)
        try:
            response = httpx.post(
                f"{self.base_url}/oauth/token",
                data={"client_id": self.client_id,
                      "client_secret": secret.reveal(),
                      "grant_type": "authorization_code",
                      "code": code,
                      "redirect_uri": self.redirect_uri},
                headers={"Accept": "application/json"},
                timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ExternalServiceError(
                f"ORCID token exchange failed: {type(exc).__name__}"
            ) from None

        if response.status_code >= 400:
            raise AuthorityError(
                f"ORCID refused the authorisation code (HTTP "
                f"{response.status_code}). The code may have expired or already "
                "been used; sign in again."
            )
        payload = response.json()
        identifier = payload.get("orcid")
        if not identifier:
            raise AuthorityError(
                "ORCID returned no identifier for this authorisation. Without "
                "one, no action can be attributed to a person, and the workflow "
                "cannot proceed."
            )
        # Validated on construction, including the MOD 11-2 checksum: an ORCID
        # is the subject of every accountability claim in the system.
        return Orcid(value=identifier)
