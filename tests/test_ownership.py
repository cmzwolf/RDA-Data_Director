"""Ownership and sessions.

Access is append-only: rights are granted and never withdrawn. Most of these
tests are therefore about what cannot be expressed rather than what happens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from datadirector_contracts import (
    AccessRole, Event, EventKind, Orcid, Ownership, OwnershipGrant,
)

from datadirector.api.ownership import (
    NotVisible, add_owner, jobs_for, ownership_of, require_actor,
    require_visible, sole_owned_by,
)
from datadirector.errors import AuthorityError
from datadirector.identity.session import COOKIE_NAME, SessionStore
from datadirector.api.auth import SessionResolver, _identifier
from datadirector.state.store import EventStore

AUDITOR = Orcid(value="0000-0003-1111-222X")


@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path / "state")


def _create(store, job_id, creator):
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.WORKFLOW_CREATED, agent="test/1.0",
                       payload={"created_by": creator.value}))


# -- the model ------------------------------------------------------------

def test_the_creator_owns_the_job_from_its_first_event(store, job_id,
                                                       researcher):
    """Not from a later grant: a log recording creation and nothing else still
    has an owner."""
    _create(store, job_id, researcher)
    ownership = ownership_of(store, job_id)
    assert ownership.owned_by(researcher)
    assert ownership.creator == researcher


def test_an_owner_may_add_an_owner(store, job_id, researcher, data_steward):
    _create(store, job_id, researcher)
    ownership = add_owner(store, job_id, data_steward, by=researcher,
                          reason="taking over while I am on leave")
    assert {o.value for o in ownership.owners} == {researcher.value,
                                                   data_steward.value}


def test_ownership_is_by_identifier_not_by_session(store, job_id, researcher):
    """A colleague can be added before they have ever signed in."""
    never_seen = Orcid(value="0000-0001-2345-6789")
    _create(store, job_id, researcher)
    ownership = add_owner(store, job_id, never_seen, by=researcher)
    assert ownership.owned_by(never_seen)


def test_no_route_removes_an_owner():
    """The absence is the contract, as it is for the event store and the gate."""
    forbidden = {"remove", "revoke", "drop", "delete", "transfer"}
    assert not forbidden & {m for m in dir(Ownership) if not m.startswith("_")}

    import datadirector.api.ownership as module
    assert not forbidden & {m for m in dir(module) if not m.startswith("_")}


def test_a_non_owner_cannot_add_owners(store, job_id, researcher, data_steward):
    """The right to extend access is part of ownership; a system where anyone
    could grant it would have no access control."""
    _create(store, job_id, researcher)
    with pytest.raises(PermissionError, match="does not own"):
        ownership_of(store, job_id).granting(AUDITOR, by=data_steward)


def test_adding_an_existing_owner_changes_nothing(store, job_id, researcher):
    _create(store, job_id, researcher)
    before = len(store.load(job_id))
    add_owner(store, job_id, researcher, by=researcher)
    assert len(store.load(job_id)) == before


def test_the_grant_records_who_and_why(store, job_id, researcher, data_steward):
    """'Added M. Aroa' is weaker than the reason, and the reason is what an
    auditor reading it in two years needs."""
    _create(store, job_id, researcher)
    add_owner(store, job_id, data_steward, by=researcher,
              reason="covering my leave until March")
    grant = [g for g in ownership_of(store, job_id).grants
             if g.granted_by is not None][0]
    assert grant.granted_by == researcher
    assert "covering my leave" in grant.reason

    events = [e for e in store.load(job_id) if e.kind is EventKind.OWNER_ADDED]
    assert events[0].human == researcher


# -- visibility -----------------------------------------------------------

def test_a_non_owner_cannot_see_the_job(store, job_id, researcher, data_steward):
    _create(store, job_id, researcher)
    with pytest.raises(NotVisible):
        require_visible(store, job_id, data_steward)


def test_a_job_you_do_not_own_is_absent_not_forbidden(store, job_id, researcher,
                                                      data_steward):
    """A 403 confirms the job exists, and existence is itself a disclosure."""
    _create(store, job_id, researcher)
    assert jobs_for(store, data_steward) == []


def test_listing_returns_only_your_own(store, researcher, data_steward):
    mine = "job-" + "A" * 26
    theirs = "job-" + "B" * 26
    _create(store, mine, researcher)
    _create(store, theirs, data_steward)
    assert jobs_for(store, researcher) == [mine]
    assert jobs_for(store, data_steward) == [theirs]


# -- the auditor ----------------------------------------------------------

def test_an_auditor_reads_any_job(store, job_id, researcher):
    """An audit that could see only work it had been invited to would not be
    an audit."""
    _create(store, job_id, researcher)
    require_visible(store, job_id, AUDITOR, roles={AccessRole.AUDITOR})
    assert jobs_for(store, AUDITOR, roles={AccessRole.AUDITOR}) == [job_id]


def test_an_auditors_reading_is_recorded(store, job_id, researcher):
    """A role whose use is invisible is an unlogged back door with a
    respectable name."""
    _create(store, job_id, researcher)
    require_visible(store, job_id, AUDITOR, roles={AccessRole.AUDITOR})
    audited = [e for e in store.load(job_id)
               if e.kind is EventKind.ACCESS_AUDITED]
    assert audited and audited[0].human == AUDITOR


def test_an_owners_reading_is_not_recorded(store, job_id, researcher):
    """It is unremarkable, and recording it would bury the readings that matter."""
    _create(store, job_id, researcher)
    require_visible(store, job_id, researcher)
    assert not [e for e in store.load(job_id)
                if e.kind is EventKind.ACCESS_AUDITED]


def test_an_auditor_cannot_act(store, job_id, researcher):
    _create(store, job_id, researcher)
    with pytest.raises(AuthorityError, match="may not act"):
        require_actor(store, job_id, AUDITOR, roles={AccessRole.AUDITOR})


def test_an_auditor_is_told_the_truth_about_why(store, job_id, researcher):
    """They can see the job, so saying it does not exist would be a lie."""
    _create(store, job_id, researcher)
    try:
        require_actor(store, job_id, AUDITOR, roles={AccessRole.AUDITOR})
    except AuthorityError as exc:
        assert "requires ownership" in str(exc)


# -- the departing researcher ---------------------------------------------

def test_sole_ownership_is_reportable(store, researcher, data_steward):
    """The list a researcher needs before leaving, and one nobody can assemble
    for them."""
    alone = "job-" + "C" * 26
    shared = "job-" + "D" * 26
    _create(store, alone, researcher)
    _create(store, shared, researcher)
    add_owner(store, shared, data_steward, by=researcher, reason="shared work")

    assert sole_owned_by(store, researcher) == [alone]
    assert sole_owned_by(store, data_steward) == []


def test_a_departed_owners_job_is_readable_and_unworkable(store, job_id,
                                                          researcher,
                                                          data_steward):
    """The intended outcome, not a gap: the alternative is an administrative
    back door created for a rare case and available in every other."""
    _create(store, job_id, researcher)

    # The researcher has gone. Nobody else owns it, and nobody can be added.
    with pytest.raises(NotVisible):
        require_visible(store, job_id, data_steward)
    with pytest.raises(PermissionError):
        ownership_of(store, job_id).granting(data_steward, by=data_steward)

    # An auditor can still read it, for accountability.
    require_visible(store, job_id, AUDITOR, roles={AccessRole.AUDITOR})
    with pytest.raises(AuthorityError):
        require_actor(store, job_id, AUDITOR, roles={AccessRole.AUDITOR})


# -- sessions -------------------------------------------------------------

@pytest.fixture
def sessions(tmp_path):
    return SessionStore(tmp_path / "sessions.json", lifetime_hours=1)


def test_a_session_resolves_to_the_orcid_it_was_issued_for(sessions, researcher):
    session = sessions.create(researcher)
    assert sessions.resolve(session.identifier).orcid == researcher


def test_the_cookie_carries_no_identity(sessions, researcher):
    """A self-describing token would put identity in the client's hands, and
    identity is what every accountability claim here rests on."""
    session = sessions.create(researcher)
    assert researcher.value not in session.identifier


def test_an_unknown_identifier_resolves_to_nothing(sessions):
    """Never to a default user."""
    assert sessions.resolve("forged") is None
    assert sessions.resolve(None) is None
    assert sessions.resolve("") is None


def test_expiry_is_enforced_on_read(sessions, researcher):
    """A sweep that has not run yet is a session that is still valid."""
    session = sessions.create(researcher)
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    assert sessions.resolve(session.identifier, now=later) is None


def test_revocation_is_immediate(sessions, researcher):
    session = sessions.create(researcher)
    sessions.revoke(session.identifier)
    assert sessions.resolve(session.identifier) is None


def test_a_person_can_be_signed_out_everywhere(sessions, researcher,
                                               data_steward):
    first = sessions.create(researcher)
    second = sessions.create(researcher)
    theirs = sessions.create(data_steward)
    assert sessions.revoke_all_for(researcher) == 2
    assert sessions.resolve(first.identifier) is None
    assert sessions.resolve(second.identifier) is None
    assert sessions.resolve(theirs.identifier) is not None


def test_sessions_survive_a_restart(tmp_path, researcher):
    """Restarting the service should not sign everyone out; on a laptop that is
    the difference between a tool and an irritation."""
    path = tmp_path / "sessions.json"
    session = SessionStore(path).create(researcher)
    assert SessionStore(path).resolve(session.identifier).orcid == researcher


def test_there_is_no_way_to_mint_a_session_without_an_orcid(sessions):
    """A development mode that minted one would always still be present in
    production."""
    import inspect
    signature = inspect.signature(sessions.create)
    assert "orcid" in signature.parameters
    assert signature.parameters["orcid"].default is inspect.Parameter.empty


def test_both_cookie_and_bearer_forms_resolve(sessions, researcher):
    """The same service answers a browser and another Data Director."""
    session = sessions.create(researcher)
    resolver = SessionResolver(sessions)
    assert resolver(f"Bearer {session.identifier}") == researcher
    assert resolver(f"{COOKIE_NAME}={session.identifier}") == researcher


def test_roles_are_granted_by_the_deployment_not_the_identity_provider(
        sessions, researcher):
    """ORCID says who someone is; an institution says what they may do here."""
    session = sessions.create(researcher)
    resolver = SessionResolver(sessions,
                               {researcher.value: [AccessRole.AUDITOR]})
    assert AccessRole.AUDITOR in resolver.roles_for(
        f"Bearer {session.identifier}")
    assert resolver.roles_for("Bearer nonsense") == set()
