"""Ownership: who may see and act on a job.

Access is decided by ownership rather than by a role hierarchy, and the model is
**append-only**. Rights can be granted and never withdrawn.

That single property removes a layer. Revocable rights need arbitration — who
may remove whom, what happens to work in progress, which administrator
adjudicates — and none of that exists here. It is the same shape as the event
log, applied to access.

It also removes a presumption. An earlier design gave a data steward standing
rights over work they had never been invited to, which is convenient and
somewhat high-handed. A steward is now added by the researcher, and the addition
is a recorded human act: consent rather than hierarchy.

Ownership is by **identifier**. A colleague can be added before they have ever
signed in, because what is granted is a right attached to an ORCID and not to a
session. The identifier is checksum-validated; that it belongs to a person who
exists is not verified and is not claimed to be.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .primitives import Orcid, utc_now


class AccessRole(StrEnum):
    """Roles that are not ownership.

    Deliberately short. Ownership covers the workflow; these two cover what
    ownership cannot.
    """

    AUDITOR = "auditor"
    """Reads any job, acts on none.

    An audit that could only see work it had been invited to would not be an
    audit, so this role is exempt from the ownership filter on reads and from
    nothing else."""

    ADMINISTRATOR = "administrator"
    """Manages configuration and plugins. Has no route to a job at all.

    Whoever can reconfigure the system cannot read or approve with it."""


class OwnershipGrant(BaseModel):
    """One owner added to one job. Never reversed."""

    model_config = ConfigDict(frozen=True)

    owner: Orcid
    granted_by: Orcid | None = Field(
        default=None,
        description="None for the creator, who owns the job from its first "
        "event rather than by a later grant.")
    reason: str | None = Field(
        default=None,
        description="Why this person was added. Prompted for, not enforced: "
        "'added M. Aroa' is weaker than 'added M. Aroa, taking over while I am "
        "on leave', and the second is what an auditor reading it in two years "
        "needs.")
    granted_at: datetime = Field(default_factory=utc_now)


class Ownership(BaseModel):
    """Who owns a job. Grows, never shrinks.

    There is no method that removes an owner, and the absence is the contract,
    as it is for the event store and the approval gate.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    grants: list[OwnershipGrant] = Field(default_factory=list)

    @property
    def owners(self) -> list[Orcid]:
        seen: dict[str, Orcid] = {}
        for grant in self.grants:
            seen.setdefault(grant.owner.value, grant.owner)
        return list(seen.values())

    @property
    def creator(self) -> Orcid | None:
        for grant in self.grants:
            if grant.granted_by is None:
                return grant.owner
        return None

    @property
    def is_sole_owned(self) -> bool:
        """Whether one person is the only owner.

        The list a researcher needs before leaving an institution, and one
        nobody can assemble for them.
        """
        return len(self.owners) == 1

    def owned_by(self, orcid: Orcid) -> bool:
        return any(o.value == orcid.value for o in self.owners)

    def granting(self, owner: Orcid, *, by: Orcid,
                 reason: str | None = None) -> "Ownership":
        """Add an owner. Returns a new value; the original is unchanged.

        Refuses a grant from someone who is not already an owner: the right to
        extend access is itself part of ownership, and a system where anyone
        could grant it would have no access control at all.
        """
        if not self.owned_by(by):
            raise PermissionError(
                f"{by.value} does not own {self.job_id} and cannot add owners "
                "to it")
        if self.owned_by(owner):
            return self
        return Ownership(
            job_id=self.job_id,
            grants=[*self.grants, OwnershipGrant(owner=owner, granted_by=by,
                                                 reason=reason)])
