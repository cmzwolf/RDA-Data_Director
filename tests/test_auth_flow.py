"""Signing in with ORCID.

Untested until an audit noticed: reachable from `serve`, and the one path where
a defect means either nobody gets in or the wrong person does.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="pip install -e 'packages/datadirector[api]'")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from datadirector.credentials.broker import CredentialBroker  # noqa: E402
from datadirector.identity.orcid import OrcidIdentityProvider  # noqa: E402
from datadirector.identity.session import COOKIE_NAME, SessionStore  # noqa: E402
from datadirector.web.auth_routes import build_auth  # noqa: E402

ALICE = "0000-0002-1825-0097"


class _Exchange:
    """Stands in for ORCID's token endpoint."""

    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status = status
        self.calls: list[dict] = []

    def post(self, url, data=None, headers=None, timeout=None):
        from urllib.parse import parse_qs
        self.calls.append(data if isinstance(data, dict)
                          else {k: v[0] for k, v in
                                parse_qs(data or "").items()})

        class _Response:
            status_code = self.status
            _payload = self.payload

            @classmethod
            def json(cls):
                return cls._payload

        return _Response()


@pytest.fixture
def flow(tmp_path, monkeypatch):
    import httpx

    sessions = SessionStore(tmp_path / "sessions.json")
    broker = CredentialBroker({"orcid:client-secret": "DD_ORCID_SECRET"},
                              environ={"DD_ORCID_SECRET": "shh"})
    provider = OrcidIdentityProvider("APP-1", broker,
                                     "https://example.org/auth/callback")
    exchange = _Exchange({"orcid": ALICE})
    monkeypatch.setattr(httpx, "post", exchange.post)

    app = build_auth(FastAPI(), provider, sessions, secure_cookies=False)
    return TestClient(app, follow_redirects=False), sessions, provider, exchange


def test_signing_in_sends_the_visitor_to_orcid(flow):
    client, _, _, _ = flow
    response = client.get("/auth/login")
    assert response.status_code == 303
    location = response.headers["location"]
    assert "orcid.org/oauth/authorize" in location
    assert "state=" in location


def test_the_callback_issues_a_session_and_sets_a_cookie(flow):
    client, sessions, provider, _ = flow
    state = provider.new_state()
    response = client.get("/auth/callback",
                          params={"code": "abc", "state": state})
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/jobs"

    identifier = response.cookies.get(COOKIE_NAME)
    assert identifier
    assert sessions.resolve(identifier).orcid.value == ALICE


def test_the_cookie_is_not_readable_by_script(flow):
    """No script needs the session identifier, and one that has it can send it
    anywhere."""
    client, _, provider, _ = flow
    response = client.get("/auth/callback",
                          params={"code": "abc", "state": provider.new_state()})
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header


def test_a_state_we_did_not_issue_is_refused(flow):
    """A redemption we cannot tie to a request we issued is what a cross-site
    attack looks like."""
    client, sessions, _, _ = flow
    with pytest.raises(Exception):
        client.get("/auth/callback",
                   params={"code": "abc", "state": "never-issued"})


def test_a_state_cannot_be_replayed(flow):
    client, _, provider, _ = flow
    state = provider.new_state()
    assert client.get("/auth/callback",
                      params={"code": "a", "state": state}).status_code == 303
    with pytest.raises(Exception):
        client.get("/auth/callback", params={"code": "b", "state": state})


def test_the_orcid_comes_from_the_exchange_not_the_request(flow):
    """The client never says who it is; that is the whole point of the flow."""
    client, sessions, provider, exchange = flow
    response = client.get("/auth/callback",
                          params={"code": "abc", "state": provider.new_state(),
                                  "orcid": "0000-0001-2345-6789"})
    identifier = response.cookies.get(COOKIE_NAME)
    assert sessions.resolve(identifier).orcid.value == ALICE


def test_the_client_secret_is_sent_and_never_returned(flow):
    client, _, provider, exchange = flow
    client.get("/auth/callback",
               params={"code": "abc", "state": provider.new_state()})
    assert exchange.calls[0]["client_secret"] == "shh"


def test_signing_out_ends_the_session_immediately(flow):
    """Not at its expiry: a person who signs out has said they are finished."""
    client, sessions, provider, _ = flow
    response = client.get("/auth/callback",
                          params={"code": "abc", "state": provider.new_state()})
    identifier = response.cookies.get(COOKIE_NAME)

    client.cookies.set(COOKIE_NAME, identifier)
    out = client.post("/auth/logout")
    assert out.status_code == 303
    assert sessions.resolve(identifier) is None


def test_repository_delegation_routes_are_absent_unless_configured(flow):
    """An instance with no repository client cannot offer to delegate, and
    offering a route that cannot work is worse than not offering it."""
    client, _, _, _ = flow
    paths = {r.path for r in client.app.routes if hasattr(r, "path")}
    assert "/auth/repository" not in paths


def test_the_credential_scope_matches_the_example_configuration():
    """The provider asks the broker for a scope by name, and the example wiring
    must declare it.

    It did not. Sign-in would have mounted, sent a visitor to ORCID, brought
    them back and failed at the token exchange — a confusing way to discover a
    missing line of configuration, and invisible to every test because the
    tests supply the broker directly.
    """
    import inspect
    from pathlib import Path

    import yaml

    signature = inspect.signature(OrcidIdentityProvider.__init__)
    scope = signature.parameters["scope"].default

    wiring = yaml.safe_load(
        (Path(__file__).parent.parent / "config/wiring.example.yaml")
        .read_text(encoding="utf-8"))
    assert scope in (wiring.get("credential_env_vars") or {}), (
        f"the provider asks for {scope!r} and the example wiring does not "
        "declare it")


# ==========================================================================
# The local session command
# ==========================================================================
#
# It exists because the ORCID sandbox is not always able to issue tokens, which
# leaves a local deployment unable to sign anyone in. It is an impersonation
# tool, so what is tested is mostly the confinement.

def test_no_route_issues_a_session_without_orcid():
    """A command can be confined to the machine; a route cannot.

    The whole guard rests on this: if any HTTP route could mint a session, the
    profile check would be decoration.
    """
    import inspect

    from datadirector.web import auth_routes, views

    for module in (auth_routes, views):
        source = inspect.getsource(module)
        assert "sessions.create(" not in source or module is auth_routes, (
            f"{module.__name__} issues sessions outside the ORCID callback")

    callback_only = inspect.getsource(auth_routes)
    assert callback_only.count("sessions.create(") == 1, (
        "a second place issues sessions; only the ORCID callback may")


def test_it_is_refused_outside_the_single_user_profile(tmp_path, monkeypatch,
                                                       capsys):
    """On a shared deployment this would be a way to become anybody."""
    import yaml

    from datadirector.cli import cmd_session

    wiring = tmp_path / "wiring.yaml"
    wiring.write_text(yaml.safe_dump({
        "profile": "institutional",
        "storage": {"state_root": str(tmp_path / "state"),
                    "working_root": str(tmp_path / "work"),
                    "restricted_root": str(tmp_path / "restricted"),
                    "watched_folder": str(tmp_path / "deposit")},
        "backends": [], "plugins": [], "credential_env_vars": {}}))

    args = type("Args", (), {"wiring": str(wiring),
                             "orcid": "0000-0002-1825-0097"})()
    assert cmd_session(args) == 2
    assert "refused" in capsys.readouterr().err


def test_a_malformed_orcid_is_refused(tmp_path, capsys):
    from datadirector.cli import cmd_session
    import yaml

    wiring = tmp_path / "wiring.yaml"
    wiring.write_text(yaml.safe_dump({
        "profile": "single-user-local",
        "storage": {"state_root": str(tmp_path / "state"),
                    "working_root": str(tmp_path / "work"),
                    "restricted_root": str(tmp_path / "restricted"),
                    "watched_folder": str(tmp_path / "deposit")},
        "backends": [], "plugins": [], "credential_env_vars": {}}))
    args = type("Args", (), {"wiring": str(wiring),
                             "orcid": "0000-0002-1825-0098"})()
    assert cmd_session(args) == 2
    assert "not a valid ORCID" in capsys.readouterr().err


def test_it_says_the_identity_was_not_proven(tmp_path, capsys):
    """A session that did not come from ORCID must not be mistaken for one that
    did, by the person using it or by anyone reading the log."""
    import yaml

    from datadirector.cli import cmd_session

    wiring = tmp_path / "wiring.yaml"
    wiring.write_text(yaml.safe_dump({
        "profile": "single-user-local",
        "storage": {"state_root": str(tmp_path / "state"),
                    "working_root": str(tmp_path / "work"),
                    "restricted_root": str(tmp_path / "restricted"),
                    "watched_folder": str(tmp_path / "deposit")},
        "backends": [], "plugins": [], "credential_env_vars": {}}))
    args = type("Args", (), {"wiring": str(wiring),
                             "orcid": "0000-0002-1825-0097"})()
    assert cmd_session(args) == 0
    out = capsys.readouterr().out
    assert "did not come from ORCID" in out
    assert "asserted, not proven" in out


# ==========================================================================
# Registration constraints ORCID imposes
# ==========================================================================

def test_secure_cookies_follow_the_scheme_not_the_profile():
    """A single-user deployment behind a tunnel is served over HTTPS, and a
    session cookie without Secure there would be sent in clear on any
    accidental downgrade."""
    from datadirector.cli import _env_flag

    assert _env_flag("DD_DOES_NOT_EXIST", default=True) is True
    assert _env_flag("DD_DOES_NOT_EXIST", default=False) is False


def test_a_non_https_redirect_is_refused_on_production(tmp_path, monkeypatch,
                                                       capsys):
    """ORCID will not register one, so starting would produce a sign-in that
    fails after the researcher has been sent away and brought back — the most
    confusing possible moment to fail.
    """
    import yaml

    from datadirector.cli import cmd_serve

    wiring = tmp_path / "wiring.yaml"
    wiring.write_text(yaml.safe_dump({
        "profile": "single-user-local",
        "storage": {"state_root": str(tmp_path / "state"),
                    "working_root": str(tmp_path / "work"),
                    "restricted_root": str(tmp_path / "restricted"),
                    "watched_folder": str(tmp_path / "deposit")},
        # A backend the policy can name: the loader refuses a policy that
        # permits one the wiring never declared, which is the check working.
        "backends": [{"name": "local", "kind": "ollama",
                      "endpoint": "http://localhost:11434", "model": "test",
                      "residency": "on-premise"}],
        "plugins": [],
        "credential_env_vars": {"orcid:client-secret": "DD_ORCID_CLIENT_SECRET"}}))
    policy = tmp_path / "policy.yaml"
    # Every sensitivity class needs a permitted backend; the policy type refuses
    # a partial one, which is correct and caught this fixture.
    policy.write_text(yaml.safe_dump({
        "backend_by_sensitivity": {"public": ["local"], "internal": ["local"],
                                   "sensitive": ["local"]}}))

    monkeypatch.setenv("DD_ORCID_CLIENT_ID", "APP-1")
    monkeypatch.setenv("DD_ORCID_CLIENT_SECRET", "shh")
    monkeypatch.setenv("DD_ORCID_BASE", "https://orcid.org")
    monkeypatch.setenv("DD_ORCID_REDIRECT_URI",
                       "http://127.0.0.1:8000/auth/callback")
    # Cleared explicitly. This test passed here and failed on a machine where
    # DD_LOCAL_ACCOUNTS happened to be exported: local accounts take precedence,
    # so the ORCID check never ran. A test that depends on the developer's shell
    # is a test that reports on the shell.
    monkeypatch.delenv("DD_LOCAL_ACCOUNTS", raising=False)

    args = type("Args", (), {"wiring": str(wiring), "policy": str(policy),
                             "host": "127.0.0.1", "port": 8000,
                             "matrix": "docs/architecture.md"})()
    assert cmd_serve(args) == 2
    error = capsys.readouterr().err
    assert "only HTTPS" in error
    assert "tunnel" in error
    assert "exactly" in error


def test_every_startup_problem_is_reported_at_once(tmp_path, monkeypatch,
                                                   capsys):
    """Returning on the first problem meant someone with a missing package and
    a bad redirect URI learned about one, fixed it, and met the other."""
    import yaml

    import datadirector.cli as module

    wiring = tmp_path / "wiring.yaml"
    wiring.write_text(yaml.safe_dump({
        "profile": "single-user-local",
        "storage": {"state_root": str(tmp_path / "state"),
                    "working_root": str(tmp_path / "work"),
                    "restricted_root": str(tmp_path / "restricted"),
                    "watched_folder": str(tmp_path / "deposit")},
        "backends": [{"name": "local", "kind": "ollama",
                      "endpoint": "http://localhost:11434", "model": "t",
                      "residency": "on-premise"}],
        "plugins": [],
        "credential_env_vars": {"orcid:client-secret": "DD_ORCID_CLIENT_SECRET"}}))
    policy = tmp_path / "policy.yaml"
    policy.write_text(yaml.safe_dump({
        "backend_by_sensitivity": {"public": ["local"], "internal": ["local"],
                                   "sensitive": ["local"]}}))

    monkeypatch.setenv("DD_ORCID_CLIENT_ID", "APP-1")
    monkeypatch.setenv("DD_ORCID_CLIENT_SECRET", "shh")
    monkeypatch.setenv("DD_ORCID_BASE", "https://orcid.org")
    monkeypatch.setenv("DD_ORCID_REDIRECT_URI",
                       "http://127.0.0.1:8000/auth/callback")
    monkeypatch.delenv("DD_LOCAL_ACCOUNTS", raising=False)

    original = module._missing_web_dependencies
    module._missing_web_dependencies = lambda required=None: ["fastapi (a)"]
    try:
        args = type("Args", (), {"wiring": str(wiring), "policy": str(policy),
                                 "host": "127.0.0.1", "port": 8000,
                                 "matrix": "docs/architecture.md"})()
        assert module.cmd_serve(args) == 2
    finally:
        module._missing_web_dependencies = original

    error = capsys.readouterr().err
    assert "fastapi" in error, "the missing package was not reported"
    assert "only HTTPS" in error, "the redirect problem was not reported"
    assert "datadirector accounts" in error, (
        "the local-accounts route out was not offered")
