"""Delegated repository credentials through the authorisation code flow.

Cluster 4 Part C2. Zenodo issues its own tokens with `deposit:write` and
`deposit:actions`; ORCID is an identity, not a deposit credential, and the two
are separate concerns that a single sign-in would blur.

Tokens are held per user per repository, never as configuration. A shared
deployment token would make every deposit appear to come from the service rather
than from the researcher, which defeats the attribution the whole provenance
chain rests on.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlencode

import httpx
from datadirector_contracts import Orcid

from ..errors import AuthorityError, CredentialError, ExternalServiceError
from .broker import Secret


class DelegatedTokenStore:
    SERVES = ("C1", "P5")
    """Per-user, per-repository tokens.

    Stored with restrictive permissions and never in the event log, the
    provenance record, or any configuration file. What is recorded of a deposit
    is that a call was made under a named user's delegation, not the delegation
    itself.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _key(orcid: Orcid, repository: str) -> str:
        return f"{orcid.value}@{repository}"

    def put(self, orcid: Orcid, repository: str, token: str) -> None:
        data = self._load()
        data[self._key(orcid, repository)] = token
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def get(self, orcid: Orcid, repository: str) -> Secret:
        token = self._load().get(self._key(orcid, repository))
        if not token:
            raise CredentialError(
                f"no {repository} credential is held for {orcid.value}. The "
                "researcher must authorise this deployment to deposit on their "
                "behalf before a deposit can be made."
            )
        return Secret(token, f"{repository}:deposit")

    def has(self, orcid: Orcid, repository: str) -> bool:
        return bool(self._load().get(self._key(orcid, repository)))

    def forget(self, orcid: Orcid, repository: str) -> None:
        """Revoke locally. The repository-side revocation is the user's to make.

        Kept explicit so a deployment can drop a delegation without waiting for
        the researcher, and so the limits of doing so are visible: a token
        removed here is still valid at the repository.
        """
        data = self._load()
        data.pop(self._key(orcid, repository), None)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


class ZenodoOAuthFlow:
    """Authorisation code flow for a Zenodo deposit token."""

    SCOPES = "deposit:write deposit:actions"

    def __init__(self, client_id: str, client_secret: Secret, redirect_uri: str,
                 store: DelegatedTokenStore, *, base_url: str,
                 timeout: float = 30.0) -> None:
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._pending: dict[str, Orcid] = {}

    def authorize_url(self, on_behalf_of: Orcid) -> tuple[str, str]:
        """Return the URL and the state, the state bound to the requesting user.

        Binding matters: a redemption arriving with a state we issued for someone
        else would otherwise store one researcher's token under another's name,
        and every subsequent deposit would be misattributed.
        """
        state = secrets.token_urlsafe(24)
        self._pending[state] = on_behalf_of
        url = f"{self.base_url}/oauth/authorize?" + urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "scope": self.SCOPES,
            "redirect_uri": self.redirect_uri,
            "state": state,
        })
        return url, state

    def exchange(self, code: str, state: str) -> Orcid:
        orcid = self._pending.pop(state, None)
        if orcid is None:
            raise AuthorityError(
                "the authorisation state is unknown or has already been used; "
                "start the authorisation again"
            )
        try:
            response = httpx.post(
                f"{self.base_url}/oauth/token",
                data={"client_id": self.client_id,
                      "client_secret": self._client_secret.reveal(),
                      "grant_type": "authorization_code",
                      "code": code,
                      "redirect_uri": self.redirect_uri},
                timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ExternalServiceError(
                f"Zenodo token exchange failed: {type(exc).__name__}"
            ) from None

        if response.status_code >= 400:
            raise AuthorityError(
                f"Zenodo refused the authorisation code (HTTP "
                f"{response.status_code})"
            )
        token = response.json().get("access_token")
        if not token:
            raise AuthorityError("Zenodo returned no access token")
        self.store.put(orcid, "zenodo", token)
        return orcid
