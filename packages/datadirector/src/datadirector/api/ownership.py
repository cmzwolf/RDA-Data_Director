"""Resolving ownership from the event log, and enforcing it.

Ownership is derived the way everything else in this system is derived: by
folding the log. There is no ownership table to fall out of step with the
events, and a job restored on another machine has the same owners.
"""

from __future__ import annotations

from datadirector_contracts import (
    AccessRole, Event, EventKind, Orcid, Ownership, OwnershipGrant,
)

from ..errors import AuthorityError
from ..state.store import EventStore


class NotVisible(Exception):
    """The caller may not see this job.

    Raised rather than returning a refusal, and translated to **404** at the
    API boundary rather than 403. A 403 confirms the job exists, and for a
    system holding sensitive material existence is itself a disclosure.
    """


def ownership_of(store: EventStore, job_id: str) -> Ownership:
    """Fold the grants out of the log.

    The creator owns the job from its first event rather than from a later
    grant, so a log that records creation and nothing else still has an owner.
    """
    grants: list[OwnershipGrant] = []
    for event in store.load(job_id):
        if event.kind is EventKind.WORKFLOW_CREATED:
            created_by = event.payload.get("created_by")
            if created_by:
                grants.append(OwnershipGrant(owner=Orcid(value=created_by)))
        elif event.kind is EventKind.OWNER_ADDED and event.human:
            grants.append(OwnershipGrant(
                owner=Orcid(value=event.payload["owner"]),
                granted_by=event.human,
                reason=event.payload.get("reason"),
                granted_at=event.occurred_at))
    return Ownership(job_id=job_id, grants=grants)


def add_owner(store: EventStore, job_id: str, owner: Orcid, *, by: Orcid,
              reason: str | None = None, agent: str = "ownership/0.1.0") -> Ownership:
    """Grant ownership. A human act, so it names one.

    The grant is validated against current ownership before it is written: the
    right to extend access is part of ownership, and a system where anyone could
    grant it would have no access control.
    """
    current = ownership_of(store, job_id)
    updated = current.granting(owner, by=by, reason=reason)
    if updated is current or len(updated.owners) == len(current.owners):
        return current
    store.append(Event(
        sequence=1, job_id=job_id, kind=EventKind.OWNER_ADDED, agent=agent,
        human=by, payload={"owner": owner.value, "reason": reason}))
    return ownership_of(store, job_id)


def require_visible(store: EventStore, job_id: str, caller: Orcid, *,
                    roles: set[AccessRole] | None = None,
                    agent: str = "ownership/0.1.0") -> Ownership:
    """Refuse a read the caller is not entitled to, and record an audited one.

    An auditor may read any job. Their reading is recorded, because a role whose
    use is invisible is an unlogged back door with a respectable name; an
    owner's reading is not, because it is unremarkable.
    """
    ownership = ownership_of(store, job_id)
    if ownership.owned_by(caller):
        return ownership
    if roles and AccessRole.AUDITOR in roles:
        store.append(Event(
            sequence=1, job_id=job_id, kind=EventKind.ACCESS_AUDITED,
            agent=agent, human=caller, payload={}))
        return ownership
    raise NotVisible(job_id)


def require_actor(store: EventStore, job_id: str, caller: Orcid, *,
                  roles: set[AccessRole] | None = None) -> Ownership:
    """Refuse an action by anyone who is not an owner.

    Checked before anything else a mutating operation does. A refusal that
    arrives after a side effect is not a refusal.

    An auditor reaching this point is refused explicitly rather than falling
    through to "not visible": they can see the job, so telling them it does not
    exist would be a lie, and the accurate refusal is that reading is all the
    role grants.
    """
    ownership = ownership_of(store, job_id)
    if ownership.owned_by(caller):
        return ownership
    if roles and AccessRole.AUDITOR in roles:
        raise AuthorityError(
            f"{caller.value} may read {job_id} as an auditor but may not act "
            "on it; acting requires ownership")
    raise NotVisible(job_id)


def jobs_for(store: EventStore, caller: Orcid, *,
             roles: set[AccessRole] | None = None) -> list[str]:
    """Jobs the caller may see. For an auditor, all of them."""
    auditing = bool(roles and AccessRole.AUDITOR in roles)
    out = []
    for job_id in store.list_jobs():
        try:
            if auditing or ownership_of(store, job_id).owned_by(caller):
                out.append(job_id)
        except Exception:
            continue
    return out


def sole_owned_by(store: EventStore, caller: Orcid) -> list[str]:
    """Jobs where the caller is the only owner.

    The list a researcher needs before leaving an institution. Nobody can
    assemble it for them, and once they have gone nobody can act on those jobs
    at all: they remain readable for accountability and workable by no one.
    """
    out = []
    for job_id in store.list_jobs():
        try:
            ownership = ownership_of(store, job_id)
        except Exception:
            continue
        if ownership.is_sole_owned and ownership.owned_by(caller):
            out.append(job_id)
    return out
