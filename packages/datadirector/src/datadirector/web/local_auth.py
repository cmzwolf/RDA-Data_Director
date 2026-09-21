"""Sign-in against a local accounts file.

Mounted only where a deployment has configured one, and never alongside the
ORCID flow: two ways in would make it ambiguous how any given session was
established, and the record has to be able to say.
"""

from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..api.auth import cookie_parameters
from ..identity.local_accounts import LocalAccounts
from ..identity.session import COOKIE_NAME, SessionStore

DEVELOPMENT_PROFILES = {"single-user-local", "institutional"}


def build_local_auth(app: FastAPI, accounts: LocalAccounts,
                     sessions: SessionStore, templates: Jinja2Templates, *,
                     profile: str = "single-user-local") -> FastAPI:
    if profile not in DEVELOPMENT_PROFILES:
        raise ValueError(
            f"local accounts are refused in the {profile!r} profile: passwords "
            "in a text file are not an authentication system for a shared "
            "instance")

    @app.get("/auth/login", response_class=HTMLResponse, tags=["auth"])
    def login(request: Request, error: str = ""):
        return templates.TemplateResponse(
            request, "local_login.html",
            {"error": error or None, "identifier": None, "caller": None,
             "accounts": sorted(accounts.load().values(),
                                key=lambda a: a.orcid.value)})

    @app.post("/auth/local", tags=["auth"])
    def submit(request: Request, identifier: str = Form(...),
               password: str = Form(...)):
        account = accounts.authenticate(identifier, password)
        if account is None:
            # One message for both failures. Not because an attacker is
            # expected on a laptop, but because a form that distinguishes them
            # teaches its operator to expect that distinction elsewhere.
            return templates.TemplateResponse(
                request, "local_login.html",
                {"error": "That identifier and password do not match.",
                 "identifier": identifier, "caller": None,
                 "accounts": sorted(accounts.load().values(),
                                    key=lambda a: a.orcid.value)},
                status_code=401)

        session = sessions.create(account.orcid, roles=account.roles,
                                  authentication="local-accounts")
        response = RedirectResponse("/ui/jobs", status_code=303)
        response.set_cookie(value=session.identifier,
                            **cookie_parameters(secure=False))
        return response

    @app.post("/auth/logout", tags=["auth"])
    def logout(request: Request):
        identifier = request.cookies.get(COOKIE_NAME)
        if identifier:
            sessions.revoke(identifier)
        response = RedirectResponse("/auth/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    return app
