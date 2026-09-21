"""ORCID sign-in against the real service.

The offline tests stub the token endpoint, so they verify our half of the flow —
state single-use, the ORCID taken from the response and never the request,
cookie flags, immediate revocation — and have never touched ORCID's servers.
That is the position the Zenodo driver was in before it was run live, and that
run found five divergences between what we believed and what the API did.

These need a registered application, so they skip without one rather than fail.
The redirect step cannot be automated: it requires a person to sign in at ORCID
and consent. What can be checked without that is everything up to the redirect,
and the shape of what comes back after it.
"""

from __future__ import annotations

import os

import pytest

from datadirector.credentials.broker import CredentialBroker
from datadirector.identity.orcid import SANDBOX, OrcidIdentityProvider

requires_live = pytest.mark.skipif(
    os.environ.get("DD_LIVE_TESTS") != "1", reason="set DD_LIVE_TESTS=1")

requires_app = pytest.mark.skipif(
    not (os.environ.get("DD_ORCID_CLIENT_ID")
         and os.environ.get("DD_ORCID_CLIENT_SECRET")),
    reason="register an application at sandbox.orcid.org and set "
           "DD_ORCID_CLIENT_ID and DD_ORCID_CLIENT_SECRET")


@pytest.fixture
def provider():
    broker = CredentialBroker({"orcid:client-secret": "DD_ORCID_CLIENT_SECRET"})
    return OrcidIdentityProvider(
        os.environ["DD_ORCID_CLIENT_ID"], broker,
        os.environ.get("DD_ORCID_REDIRECT_URI",
                       "http://127.0.0.1:8000/auth/callback"),
        base_url=os.environ.get("DD_ORCID_BASE", SANDBOX))


@requires_live
@requires_app
def test_the_authorize_url_is_accepted_by_orcid(provider, capsys):
    """ORCID should serve the sign-in page rather than reject our parameters.

    A wrong client id or an unregistered redirect URI is rejected here, and
    finding that out now is cheaper than finding it out with a researcher
    waiting.
    """
    import httpx

    url = provider.authorize_url(provider.new_state())
    response = httpx.get(url, follow_redirects=False, timeout=20.0)

    with capsys.disabled():
        print(f"\n  authorize -> HTTP {response.status_code}")
        print(f"      {url[:120]}")
        if response.status_code in (301, 302, 303, 307):
            print(f"      redirected to {response.headers.get('location', '')[:120]}")

    assert response.status_code < 400, (
        f"ORCID refused the authorisation request (HTTP "
        f"{response.status_code}). Check the client id and that the redirect "
        f"URI registered with the application matches {provider.redirect_uri!r}.")
    location = response.headers.get("location", "")
    assert "error" not in location.lower(), (
        f"ORCID redirected to an error: {location}")


@requires_live
@requires_app
def test_an_invalid_code_is_refused_the_way_we_expect(provider, capsys):
    """What a failed exchange actually looks like.

    Our code turns any 4xx into an AuthorityError saying the code may have
    expired. Whether that is the right message depends on what ORCID really
    returns, and this is the only way to find out.
    """
    from datadirector.errors import AuthorityError

    state = provider.new_state()
    try:
        provider.exchange("definitely-not-a-valid-code", state=state)
        refused, message = False, ""
    except AuthorityError as exc:
        refused, message = True, str(exc)
    except Exception as exc:
        refused, message = True, f"{type(exc).__name__}: {exc}"

    with capsys.disabled():
        print(f"\n  invalid code refused: {refused}")
        print(f"      {message[:200]}")

    assert refused, (
        "ORCID accepted an invalid authorisation code, which would mean our "
        "exchange is not checking what it thinks it is")


@requires_live
@requires_app
def test_the_client_secret_never_appears_in_an_error(provider):
    """A credential in an error message reaches logs, screens and bug reports."""
    from datadirector.errors import AuthorityError

    secret = os.environ["DD_ORCID_CLIENT_SECRET"]
    try:
        provider.exchange("invalid", state=provider.new_state())
    except (AuthorityError, Exception) as exc:
        assert secret not in str(exc)
