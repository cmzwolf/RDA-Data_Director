"""Local development accounts.

They exist because configuring an ORCID application, and HTTPS with it, in order
to debug an ingestion is the wrong order to do things in. What is tested is that
they work for several people at once, are refused where they would be
inappropriate, and leave a record that a development run was a development run.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="pip install -e 'packages/datadirector[api]'")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from datadirector_contracts import AccessRole  # noqa: E402

from datadirector.identity.local_accounts import (  # noqa: E402
    LocalAccounts, checksum, mint, write_example,
)
from datadirector.identity.session import COOKIE_NAME, SessionStore  # noqa: E402
from datadirector.web.local_auth import build_local_auth  # noqa: E402
from datadirector.web.views import templates_for  # noqa: E402


@pytest.fixture
def accounts_file(tmp_path):
    return write_example(tmp_path / "accounts.txt")


@pytest.fixture
def accounts(accounts_file):
    return LocalAccounts(accounts_file)


@pytest.fixture
def client(accounts, tmp_path):
    sessions = SessionStore(tmp_path / "sessions.json")
    app = build_local_auth(FastAPI(), accounts, sessions, templates_for())
    return TestClient(app, follow_redirects=False), sessions


# -- the file ---------------------------------------------------------------

def test_minted_identifiers_are_well_formed_and_distinct():
    """ORCID's checksum applies to fictions too, and an earlier version of
    `mint` threw away the seed and produced the same identifier every time."""
    minted = [mint(n) for n in range(1, 6)]
    assert len({m.value for m in minted}) == 5
    for orcid in minted:
        body, check = orcid.value[:-1], orcid.value[-1]
        assert checksum(body) == check


def test_several_people_can_exist(accounts):
    """The point: the workflow needs more than one identity to be exercised at
    all — ownership, delegation, the auditor role."""
    loaded = accounts.load()
    assert len(loaded) == 3
    assert any(AccessRole.AUDITOR in a.roles for a in loaded.values())


def test_roles_come_from_the_file(accounts):
    auditor = next(a for a in accounts.load().values() if a.roles)
    assert auditor.roles == [AccessRole.AUDITOR]


def test_a_malformed_line_is_refused_with_its_number(tmp_path):
    """A file that half-loads would give a confusing partial set of people."""
    path = tmp_path / "bad.txt"
    path.write_text("0009-0000-0000-0017:password\nnonsense\n")
    with pytest.raises(ValueError, match="bad.txt:2"):
        LocalAccounts(path).load()


def test_a_malformed_identifier_is_refused(tmp_path):
    """An identifier that cannot be one is a typo waiting to be filed as a
    person."""
    path = tmp_path / "bad.txt"
    path.write_text("0000-0002-1825-0098:password\n")
    with pytest.raises(ValueError, match="checksum"):
        LocalAccounts(path).load()


def test_an_unknown_role_is_refused(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_text("0009-0000-0000-0017:password:X:superuser\n")
    with pytest.raises(ValueError, match="not a role"):
        LocalAccounts(path).load()


def test_a_missing_file_is_no_accounts_not_an_error(tmp_path):
    assert LocalAccounts(tmp_path / "absent.txt").load() == {}


# -- signing in -------------------------------------------------------------

def test_a_correct_password_issues_a_session(client, accounts):
    http, sessions = client
    identifier = sorted(accounts.load())[0]
    response = http.post("/auth/local", data={"identifier": identifier,
                                              "password": "password"})
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/jobs"
    session = sessions.resolve(response.cookies.get(COOKIE_NAME))
    assert session.orcid.value == identifier


def test_a_wrong_password_does_not(client, accounts):
    http, sessions = client
    identifier = sorted(accounts.load())[0]
    response = http.post("/auth/local", data={"identifier": identifier,
                                              "password": "wrong"})
    assert response.status_code == 401
    assert response.cookies.get(COOKIE_NAME) is None


def test_the_same_message_for_both_failures(client, accounts):
    """Not because an attacker is expected on a laptop, but because a form that
    distinguishes them teaches its operator to expect that distinction
    elsewhere."""
    http, _ = client
    known = sorted(accounts.load())[0]

    unknown_user = http.post("/auth/local",
                             data={"identifier": "0009-0000-0000-0041",
                                   "password": "password"})
    wrong_password = http.post("/auth/local",
                               data={"identifier": known, "password": "wrong"})

    assert unknown_user.status_code == wrong_password.status_code == 401
    assert "do not match" in unknown_user.text
    assert "do not match" in wrong_password.text


def test_roles_reach_the_session(client, accounts):
    http, sessions = client
    auditor = next(a for a in accounts.load().values() if a.roles)
    response = http.post("/auth/local",
                         data={"identifier": auditor.orcid.value,
                               "password": "password"})
    session = sessions.resolve(response.cookies.get(COOKIE_NAME))
    assert AccessRole.AUDITOR in session.roles


def test_the_session_records_that_the_identity_was_not_proven(client, accounts):
    """Provenance from a development run must stay distinguishable from a real
    researcher's work, or the archive quietly acquires fiction."""
    http, sessions = client
    identifier = sorted(accounts.load())[0]
    response = http.post("/auth/local", data={"identifier": identifier,
                                              "password": "password"})
    session = sessions.resolve(response.cookies.get(COOKIE_NAME))
    assert session.authentication == "local-accounts"


def test_an_orcid_session_is_marked_differently(tmp_path):
    from datadirector_contracts import Orcid

    sessions = SessionStore(tmp_path / "s.json")
    session = sessions.create(Orcid(value="0000-0002-1825-0097"))
    assert session.authentication == "orcid"


def test_the_page_says_the_identities_are_asserted(client):
    http, _ = client
    body = http.get("/auth/login").text
    assert "asserted, not proven" in body
    assert "local development accounts" in body.lower()


def test_the_available_accounts_are_listed_without_passwords(client, accounts):
    """Development fictions, not people: hunting for an identifier in a text
    file while debugging a workflow is friction for no benefit."""
    http, _ = client
    body = http.get("/auth/login").text
    for identifier in accounts.load():
        assert identifier in body
    assert 'type="password"' in body, "the password field is not masked"


def test_signing_out_ends_the_session(client, accounts):
    http, sessions = client
    identifier = sorted(accounts.load())[0]
    response = http.post("/auth/local", data={"identifier": identifier,
                                              "password": "password"})
    token = response.cookies.get(COOKIE_NAME)
    http.cookies.set(COOKIE_NAME, token)
    http.post("/auth/logout")
    assert sessions.resolve(token) is None


# -- where they are refused -------------------------------------------------

def test_they_are_refused_on_a_national_deployment(accounts, tmp_path):
    """A shared instance where passwords live in a text file is a different
    thing entirely."""
    sessions = SessionStore(tmp_path / "s.json")
    with pytest.raises(ValueError, match="refused in the 'national' profile"):
        build_local_auth(FastAPI(), accounts, sessions, templates_for(),
                         profile="national")
