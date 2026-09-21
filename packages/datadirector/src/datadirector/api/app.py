"""The core API.

One implementation note that costs a reader five minutes if it is not said.
This module uses `from __future__ import annotations`, which turns every
annotation into a string that FastAPI later evaluates against *module* globals.
A dependency defined inside `build_app` is not in those globals, so
`Annotated[Principal, Depends(principal)]` silently fails to resolve and the
parameter is treated as a query field. `Depends` therefore appears in the
default value, where it is a real object at definition time rather than a name
to be looked up later.

Requirement C5. The OpenAPI descriptor is generated from the same pydantic
models the runtime validates against (ADR-003), so the published specification
cannot describe something the software does not do.

Three rules shape the surface.

**Reads never change anything.** Every GET is a projection of the event log, so
polling is safe, a client may disappear for a week, and two clients see the same
thing.

**Human acts are authenticated and singular.** Confirming, resolving and
depositing all require an identified ORCID, and none of them accepts a list.
A client that could resolve forty gate items in one call has reviewed nothing.

**The irreversible operation says so.** Depositing requires an explicit
confirmation flag, and repeating it returns the existing identifiers rather than
creating a second record.

Every failure has one shape. An early version returned problems at the top level
from its own handlers and nested under `detail` from framework ones, so a client
would have needed to know which layer refused it in order to read why — which is
exactly the information a refusal exists to convey.
"""

from __future__ import annotations

from typing import Annotated, Callable

from datadirector_contracts import AccessRole, ItemDecision, Orcid, Visibility
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import AuthorityError, CredentialError, DataDirectorError
from .ownership import (
    NotVisible, add_owner, jobs_for, require_actor, require_visible,
    sole_owned_by,
)
from .models import (
    Capabilities, ConfirmDeclarationRequest, DepositRequest, DepositResponse,
    AddOwnerRequest, EventView, GateItemView, GateView, JobDetail, JobSummary,
    ProblemDetail, ResolveRequest, ServiceDescription,
)
from .service import JobService

API_VERSION = "0.1.0"


class Principal:
    """The authenticated human on whose behalf a request is made."""

    def __init__(self, orcid: Orcid, roles: set[AccessRole] | None = None) -> None:
        self.orcid = orcid
        self.roles = roles or set()


def build_app(service: JobService, *, resolve_principal: Callable[[str], Orcid],
              deployment_profile: str = "single-user-local",
              capabilities: Capabilities | None = None,
              resolve_roles: Callable[[str], set] | None = None) -> FastAPI:
    app = FastAPI(
        title="Data Director core API",
        version=API_VERSION,
        summary="A candidate harmonised API for requirement C5.",
        description=__doc__,
    )

    def principal(authorization: Annotated[str | None, Header()] = None
                  ) -> Principal:
        """Resolve the caller, or refuse.

        Anonymous access is refused for every endpoint rather than only for the
        mutating ones. Reads expose decision records, provenance and gate items,
        which are about identified people and their judgements; §5.4 requires
        that agents never act anonymously, and a reader who cannot be named is
        not obviously better.
        """
        if not authorization:
            raise HTTPException(
                status_code=401,
                detail=ProblemDetail(
                    status=401, code="not-authenticated",
                    detail="this API identifies every caller by ORCID; supply "
                           "an Authorization header",
                    requirement="P5").model_dump())
        try:
            orcid = resolve_principal(authorization)
            roles = resolve_roles(authorization) if resolve_roles else set()
            return Principal(orcid, roles)
        except Exception:
            raise HTTPException(
                status_code=401,
                detail=ProblemDetail(
                    status=401, code="not-authenticated",
                    detail="the credential was not recognised",
                    requirement="P5").model_dump()) from None

    ERROR_RESPONSES = {
        401: {"model": ProblemDetail, "description": "Caller not identified"},
        404: {"model": ProblemDetail, "description": "No such job or item"},
        409: {"model": ProblemDetail, "description": "Refused by a rule"},
        422: {"model": ProblemDetail, "description": "Unusable request"},
        428: {"model": ProblemDetail,
              "description": "Confirmation required for an irreversible act"},
    }

    @app.exception_handler(StarletteHTTPException)
    def _http(request, exc: StarletteHTTPException):
        """Give framework refusals the same shape as ours.

        A client should not have to know which layer refused it in order to find
        out why.
        """
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=ProblemDetail(status=exc.status_code, code="error",
                                  detail=str(exc.detail)).model_dump())

    @app.exception_handler(NotVisible)
    def _not_visible(request, exc: NotVisible):
        """404, not 403.

        A 403 confirms the job exists, and for a system holding sensitive
        material existence is itself a disclosure. A caller who does not own a
        job learns only that they have no job by that name.
        """
        return JSONResponse(
            status_code=404,
            content=ProblemDetail(
                status=404, code="no-such-job",
                detail=f"no job {exc.args[0]!r} in this instance").model_dump())

    @app.exception_handler(AuthorityError)
    def _authority(request, exc: AuthorityError):
        return JSONResponse(
            status_code=409,
            content=ProblemDetail(
                status=409, code="refused", detail=str(exc),
                requirement="P4").model_dump())

    @app.exception_handler(CredentialError)
    def _credential(request, exc: CredentialError):
        return JSONResponse(
            status_code=412,
            content=ProblemDetail(status=412, code="credential-missing",
                                  detail=str(exc), requirement="C1").model_dump())

    @app.exception_handler(DataDirectorError)
    def _general(request, exc: DataDirectorError):
        return JSONResponse(
            status_code=500,
            content=ProblemDetail(status=500, code="internal",
                                  detail=str(exc)).model_dump())

    # -- discovery ---------------------------------------------------------

    @app.get("/api", response_model=ServiceDescription, tags=["discovery"])
    def describe() -> ServiceDescription:
        """What this instance is, for a client that has just found it.

        At `/api` rather than `/`. Both a machine discovering the service and a
        person arriving in a browser have a claim on the root, and the API held
        it: a researcher opening the address was handed a JSON object. The root
        now answers whichever is asking (see the interface), and this is the
        unambiguous address for a client that wants the description regardless.
        """
        return ServiceDescription(api_version=API_VERSION,
                                  implementation="datadirector",
                                  deployment_profile=deployment_profile)

    @app.get("/capabilities", response_model=Capabilities, tags=["discovery"])
    def get_capabilities() -> Capabilities:
        """What this instance can do, before anything is sent to it.

        Unauthenticated, deliberately and as the only such endpoint: an instance
        that required a credential to say what it accepts could not be
        discovered by another instance that had not yet been given one, which
        would make C5's inter-instance interaction impossible to bootstrap.
        Nothing here is about a person or a dataset.
        """
        if capabilities is not None:
            return capabilities
        return Capabilities(coverage=[], accepts_deposits=service.driver is not None)

    # -- jobs --------------------------------------------------------------

    @app.post("/jobs", response_model=JobSummary, status_code=201, tags=["jobs"],
              responses=ERROR_RESPONSES)
    def create_job(caller: Principal = Depends(principal)) -> JobSummary:
        job_id = service.create(human=caller.orcid)
        state = service.state(job_id)
        return JobSummary(job_id=job_id, step=state.step)

    @app.get("/jobs", response_model=list[JobSummary], tags=["jobs"],
             responses=ERROR_RESPONSES)
    def list_jobs(caller: Principal = Depends(principal)
                  ) -> list[JobSummary]:
        out = []
        for job_id in jobs_for(service.store, caller.orcid, roles=caller.roles):
            state = service.state(job_id)
            out.append(JobSummary(
                job_id=job_id, step=state.step,
                sensitivity=(state.classification.level.label
                             if state.classification else None),
                halted_reason=state.halted_reason, terminal=state.terminal))
        return out

    @app.get("/jobs/sole-owned", response_model=list[JobSummary], tags=["jobs"],
             responses=ERROR_RESPONSES)
    def list_sole_owned(caller: Principal = Depends(principal)
                        ) -> list[JobSummary]:
        """Jobs where the caller is the only owner.

        The list a researcher needs before leaving an institution, and one
        nobody can assemble for them. Once they have gone, these jobs remain
        readable for accountability and workable by no one.
        """
        return [JobSummary(job_id=job_id, step=service.state(job_id).step)
                for job_id in sole_owned_by(service.store, caller.orcid)]

    # Declared before /jobs/{job_id}: a literal path must be registered ahead of
    # the parameterised one, or "sole-owned" is matched as a job identifier.
    @app.get("/jobs/{job_id}", response_model=JobDetail, tags=["jobs"],
             responses=ERROR_RESPONSES)
    def get_job(job_id: str,
                caller: Principal = Depends(principal)) -> JobDetail:
        state = _state_or_404(service, job_id)
        require_visible(service.store, job_id, caller.orcid, roles=caller.roles)
        gate = service.gate(job_id)
        return JobDetail(
            job_id=job_id, step=state.step,
            sensitivity=(state.classification.level.label
                         if state.classification else None),
            halted_reason=state.halted_reason, terminal=state.terminal,
            event_count=len(service.events(job_id)),
            config_digest=state.config_digest, material=list(state.material),
            deposit_pid=state.deposit_pid,
            gate_unresolved=len(gate.unresolved()),
            blocks_deposit=gate.blocks_deposit())

    @app.get("/jobs/{job_id}/events", response_model=list[EventView],
             tags=["audit"])
    def get_events(job_id: str,
                   caller: Principal = Depends(principal)
                   ) -> list[EventView]:
        """The audit record. Verified on read: the store refuses a broken chain."""
        require_visible(service.store, job_id, caller.orcid, roles=caller.roles)
        return [EventView(sequence=e.sequence, kind=e.kind.value,
                          occurred_at=e.occurred_at, agent=e.agent,
                          human=e.human.value if e.human else None,
                          payload=e.payload)
                for e in _events_or_404(service, job_id)]

    @app.get("/jobs/{job_id}/provenance", tags=["audit"])
    def get_provenance(job_id: str,
                       visibility: Visibility = Query(default=Visibility.OPEN),
                       caller: Principal = Depends(principal)) -> dict:
        """PROV-O at a clearance level.

        Defaults to `open`. A caller asking for more must say so, because the
        restricted partitions hold justifications withheld from the open record
        (§7.5) and a default that returned them would make the partition
        decorative.
        """
        require_visible(service.store, job_id, caller.orcid, roles=caller.roles)
        if service.recorder is None:
            raise HTTPException(status_code=501, detail=ProblemDetail(
                status=501, code="not-configured",
                detail="no provenance recorder is configured",
                requirement="R10").model_dump())
        return service.recorder.export(job_id, up_to=visibility)

    @app.get("/jobs/{job_id}/owners", tags=["jobs"], responses=ERROR_RESPONSES)
    def get_owners(job_id: str, caller: Principal = Depends(principal)) -> dict:
        _state_or_404(service, job_id)
        ownership = require_visible(service.store, job_id, caller.orcid,
                                    roles=caller.roles)
        return {"owners": [o.value for o in ownership.owners],
                "creator": ownership.creator.value if ownership.creator else None,
                "sole_owned": ownership.is_sole_owned,
                "grants": [{"owner": g.owner.value,
                            "granted_by": g.granted_by.value if g.granted_by
                            else None,
                            "reason": g.reason,
                            "granted_at": g.granted_at.isoformat()}
                           for g in ownership.grants]}

    @app.post("/jobs/{job_id}/owners", tags=["human-acts"],
              responses=ERROR_RESPONSES)
    def post_owner(job_id: str, body: AddOwnerRequest,
                   caller: Principal = Depends(principal)) -> dict:
        """Add an owner. Never removes one; no route does.

        Ownership is by identifier, so a colleague may be added before they have
        ever signed in.
        """
        _state_or_404(service, job_id)
        require_actor(service.store, job_id, caller.orcid, roles=caller.roles)
        try:
            owner = Orcid(value=body.orcid)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=ProblemDetail(
                status=422, code="malformed-orcid", detail=str(exc),
                requirement="P5").model_dump()) from None
        ownership = add_owner(service.store, job_id, owner, by=caller.orcid,
                              reason=body.reason)
        return {"owners": [o.value for o in ownership.owners],
                "sole_owned": ownership.is_sole_owned}

    # -- the gate ----------------------------------------------------------

    @app.get("/jobs/{job_id}/gate", response_model=GateView, tags=["gate"],
             responses=ERROR_RESPONSES)
    def get_gate(job_id: str,
                 caller: Principal = Depends(principal)) -> GateView:
        _state_or_404(service, job_id)
        require_visible(service.store, job_id, caller.orcid, roles=caller.roles)
        gate = service.gate(job_id)
        resolved = {r.item_id: r for r in gate.state.resolutions}
        items = []
        for item in gate.state.items:
            resolution = resolved.get(item.item_id)
            items.append(GateItemView(
                item_id=item.item_id, kind=item.kind.value,
                artefact=item.artefact, summary=item.summary,
                detail=item.detail,
                permitted_decisions=[d.value for d in item.permitted_decisions],
                resolved=resolution is not None,
                resolved_by=resolution.decided_by.value if resolution else None,
                decision=resolution.decision.value if resolution else None))
        return GateView(items=items, unresolved=len(gate.unresolved()),
                        blocks_deposit=gate.blocks_deposit())

    @app.post("/jobs/{job_id}/gate/{item_id}/resolve", status_code=200,
              tags=["gate"], responses=ERROR_RESPONSES)
    def resolve_item(job_id: str, item_id: str, body: ResolveRequest,
                     caller: Principal = Depends(principal)) -> dict:
        """Decide one item.

        One item per call, by design. The path carries the identifier and the
        body carries the decision; there is no endpoint that takes a list.
        """
        _state_or_404(service, job_id)
        # Ownership before anything else: a refusal that arrives after a side
        # effect is not a refusal.
        require_actor(service.store, job_id, caller.orcid, roles=caller.roles)
        try:
            decision = ItemDecision(body.decision)
        except ValueError:
            raise HTTPException(status_code=422, detail=ProblemDetail(
                status=422, code="unknown-decision",
                detail=f"{body.decision!r} is not a decision this system "
                       "recognises",
                requirement="P4").model_dump()) from None
        try:
            service.resolve(job_id, item_id, decision, human=caller.orcid,
                            reason=body.reason)
        except KeyError:
            raise HTTPException(status_code=404, detail=ProblemDetail(
                status=404, code="no-such-item",
                detail=f"no gate item {item_id!r} in job {job_id}"
            ).model_dump()) from None
        gate = service.gate(job_id)
        return {"item_id": item_id, "decision": decision.value,
                "unresolved": len(gate.unresolved()),
                "blocks_deposit": gate.blocks_deposit()}

    # -- human acts --------------------------------------------------------

    @app.post("/jobs/{job_id}/declaration/confirm", tags=["human-acts"],
              responses=ERROR_RESPONSES)
    def confirm_declaration(job_id: str, body: ConfirmDeclarationRequest,
                            caller: Principal = Depends(principal)
                            ) -> dict:
        _state_or_404(service, job_id)
        require_actor(service.store, job_id, caller.orcid, roles=caller.roles)
        level = service.confirm_declaration(
            job_id, human=caller.orcid, sensitivity=body.sensitivity,
            note=body.note)
        return {"sensitivity": level.label,
                "assumed_most_restrictive": body.sensitivity is None}

    @app.post("/jobs/{job_id}/metadata/approve", tags=["human-acts"],
              responses=ERROR_RESPONSES)
    def approve_metadata(job_id: str,
                         caller: Principal = Depends(principal)
                         ) -> dict:
        _state_or_404(service, job_id)
        require_actor(service.store, job_id, caller.orcid, roles=caller.roles)
        service.approve_metadata(job_id, human=caller.orcid)
        return {"approved_by": caller.orcid.value}

    @app.post("/jobs/{job_id}/deposit", response_model=DepositResponse,
              tags=["human-acts"], responses=ERROR_RESPONSES)
    def deposit(job_id: str, body: DepositRequest,
                caller: Principal = Depends(principal)
                ) -> DepositResponse:
        """Publish. Irreversible, and the only operation that says so.

        Requires an explicit acknowledgement: a client that reached this
        endpoint by accident should not succeed at it (C13).
        """
        _state_or_404(service, job_id)
        require_actor(service.store, job_id, caller.orcid, roles=caller.roles)
        if not body.confirm_irreversible:
            raise HTTPException(status_code=428, detail=ProblemDetail(
                status=428, code="confirmation-required",
                detail="publication cannot be undone; set confirm_irreversible "
                       "to proceed",
                requirement="C13").model_dump())
        result = service.deposit(job_id, human=caller.orcid,
                                 repository=body.repository)
        return DepositResponse(**result)

    return app


def _state_or_404(service: JobService, job_id: str):
    try:
        return service.state(job_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail=ProblemDetail(
            status=404, code="no-such-job",
            detail=f"no job {job_id!r} in this instance").model_dump()) from None


def _events_or_404(service: JobService, job_id: str):
    events = service.events(job_id)
    if not events:
        raise HTTPException(status_code=404, detail=ProblemDetail(
            status=404, code="no-such-job",
            detail=f"no job {job_id!r} in this instance").model_dump())
    return events
