"""Sign-in, sign-out, and the ORCID exchange.

Specified as Part B2 of cluster 5 and not built until an assembly audit found
`identity/orcid.py` and `credentials/oauth.py` unreachable: the session store
existed, the resolver existed, and nothing issued a session.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..api.auth import SessionResolver, cookie_parameters
from ..credentials.oauth import ZenodoOAuthFlow
from ..identity.orcid import OrcidIdentityProvider
from ..identity.session import COOKIE_NAME, SessionStore


def build_auth(app: FastAPI, provider: OrcidIdentityProvider,
               sessions: SessionStore, *, secure_cookies: bool = True,
               deposit_flow: ZenodoOAuthFlow | None = None,
               resolver: SessionResolver | None = None) -> FastAPI:
    @app.get("/auth/login", response_class=HTMLResponse, tags=["auth"])
    def login():
        """Send the visitor to ORCID.

        The state is issued here and consumed at the callback, so a redemption
        we cannot tie to a request we issued is refused — which is what a
        cross-site attack looks like.
        """
        state = provider.new_state()
        return RedirectResponse(provider.authorize_url(state), status_code=303)

    @app.get("/auth/callback", tags=["auth"])
    def callback(code: str, state: str):
        """Exchange the code, issue a session, set the cookie.

        The ORCID comes from the exchange and never from the request: that is
        the whole point of the flow, and the reason no development mode mints a
        session without it.
        """
        orcid = provider.exchange(code, state=state)
        session = sessions.create(orcid)
        response = RedirectResponse("/ui/jobs", status_code=303)
        response.set_cookie(value=session.identifier,
                            **cookie_parameters(secure=secure_cookies))
        return response

    @app.post("/auth/logout", tags=["auth"])
    def logout(request: Request):
        """End the session now, not at its expiry."""
        identifier = request.cookies.get(COOKIE_NAME)
        if identifier:
            sessions.revoke(identifier)
        response = RedirectResponse("/auth/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    if deposit_flow is not None and resolver is not None:
        @app.get("/auth/repository", tags=["auth"])
        def authorise_repository(request: Request):
            """Ask the repository for a token on this researcher's behalf.

            Separate from signing in, because ORCID is an identity and a
            deposit token is a delegation: conflating them would mean every
            deposit appeared to come from the service rather than from the
            person, which defeats the attribution the provenance chain rests on.
            """
            orcid = resolver(request.cookies.get(COOKIE_NAME))
            url, _ = deposit_flow.authorize_url(orcid)
            return RedirectResponse(url, status_code=303)

        @app.get("/auth/repository/callback", tags=["auth"])
        def repository_callback(code: str, state: str):
            """Store the token against the ORCID the state was issued for.

            The state binding matters: a redemption arriving with someone
            else's state would file one researcher's token under another's name
            and misattribute every subsequent deposit.
            """
            deposit_flow.exchange(code, state)
            return RedirectResponse("/ui/jobs", status_code=303)

    return app
