"""The web interface: server-rendered HTML over the core API's service layer.

Not a separate implementation. Every route here goes through `JobService` and
the ownership helpers the API uses, so anything the interface can do another
Data Director can do (C5). Where the interface needed something the API lacked,
the API gained it.

Why HTML rather than a client application is argued in the cluster 5
specification. The short version: C6 asks for WCAG 2.1 AA, and forms and
full-page navigation are accessible before anyone works on them, whereas a
client-side application is accessible after sustained effort that stays
invisible until someone tries a screen reader.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Callable

from datadirector_contracts import AccessRole, EventKind, ItemDecision, Orcid
from fastapi import (
    Depends, FastAPI, File, Form, HTTPException, Request, UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..api.ownership import (
    NotVisible, add_owner, jobs_for, require_actor, require_visible,
    sole_owned_by,
)
from ..api.service import JobService
from ..errors import AuthorityError, CredentialError
from ..identity.session import COOKIE_NAME
from .narrate import describe
from .markdown import render_markdown
from .progress import PHASES, events_in_phase, phases_for
from . import recordedit
from ..state.projection import fold
from ..gate.items import PROPOSAL_WORDS
from ..gate import review as review_items

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"

# Decisions that cannot be recorded without a reason. The domain layer already
# refuses them; the form states it too, because a rejection after the fact
# teaches a user to type whatever passes.
REASON_REQUIRED = {
    ItemDecision.PUBLISH_AS_IS: "publishing without inspection",
    ItemDecision.CONSULTED: "recording a consultation",
    ItemDecision.REQUEST_RERUN: "asking for another attempt",
}

DECISION_LABELS = {
    ItemDecision.APPROVE: "Accept this proposal",
    ItemDecision.REJECT: "Reject this proposal",
    ItemDecision.PUBLISH_AS_IS: "Publish it anyway, uninspected",
    ItemDecision.EXCLUDE_FROM_DEPOSIT: "Leave it out of the deposit",
    ItemDecision.CLASSIFY_SENSITIVE: "Treat the job as sensitive",
    ItemDecision.INSPECTED_EXTERNALLY: "I inspected it myself",
    ItemDecision.CONSULTED: "I consulted the community or governance body",
    ItemDecision.NOT_APPLICABLE: "This does not apply here",
       # What a reviewer can do about a model's draft. The third is not a
       # validation, and the label has to say so on the button: the earlier
       # screen invited a person to "retry" and let the rejected draft flow
       # downstream, because every recorded decision looked like an approval.
    ItemDecision.EDITED: "I have written my own version of it",
    ItemDecision.REQUEST_RERUN: "Ask it to write again (this draft stays "
                                   "unvalidated)",
}

PRESUMPTION_MARKERS = ("not inspected", "presumed", "presumption")

# What the job page says after an action settled its outcome, keyed by the query
# parameter the redirect carries. A deposit that published nothing because the
# researcher withheld every file is a decision honoured, and it has to be said in
# those terms: the alternative was an unhandled exception, which reads to a
# researcher as a tool breaking on them rather than as their own choice carried
# through.
NOTICES = {
    "nothing-to-deposit":
         "Nothing was published. Every file was excluded at the review gate, so "
         "this job has been closed without a persistent identifier. Nothing has "
         "left this machine, and the material is still where it was.",
}


class WebInterface:
    """The interface as a declared component.

    Exists so the conformance report has something to point at for C6. The
    report caught its absence within a minute of the matrix row changing, which
    is the whole reason it was written.
    """

    SERVES = ("C6", "P12", "P4")


class Caller:
    def __init__(self, orcid: Orcid, roles: set[AccessRole]) -> None:
        self.orcid = orcid
        self.roles = roles


def templates_for() -> Jinja2Templates:
    """The interface's template environment.

    Shared so the sign-in pages render inside the same layout as everything
    else; a login screen that looked like a different application would be the
    first thing a visitor saw.
    """
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["md"] = render_markdown
    return templates


def build_web(app: FastAPI, service: JobService, *, pipeline=None,
              deployment_profile: str = "single-user-local",
              sign_in_available: bool = False,
              local_accounts: bool = False,
              residencies: list[str] | None = None,
              resolve_principal: Callable[[str | None], Orcid],
              resolve_roles: Callable[[str | None], set] | None = None,
              repository: str = "zenodo") -> FastAPI:
    # The same environment as the sign-in pages, so the `md` filter is
    # registered once and every page has it.
    templates = templates_for()
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    def caller(request: Request) -> Caller:
        """Resolve from the session cookie, or send the visitor to sign in.

        A browser gets a redirect rather than a 401 body, because a sign-in page
        is the useful response to being unauthenticated in a browser.
        """
        credential = request.cookies.get(COOKIE_NAME)
        try:
            orcid = resolve_principal(credential)
        except Exception:
            raise HTTPException(status_code=303, headers={"Location": "/auth/login"})
        roles = resolve_roles(credential) if resolve_roles else set()
        return Caller(orcid, roles)

    def render(request: Request, template: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, template, context)

    @app.get("/", tags=["ui"])
    def landing(request: Request):
        """The front door, for whoever is at it.

        A person gets a page; a client asking for JSON gets the service
        description. Both have a claim on the root — a Data Director discovering
        another instance starts here, and so does a researcher typing the
        address — and answering only one of them meant a researcher opening the
        service was handed a JSON object.

        Negotiated on Accept rather than split into two addresses, because a
        root that redirects humans elsewhere makes the address people share the
        wrong one.

        Unauthenticated by design, and the only page that is. A visitor who has
        never been here needs to know what this is and that they sign in with
        their own ORCID — not a bare redirect to an identity provider they did
        not ask for.

        The instance itself belongs to no one ORCID: the application
        credentials identify *this installation* to ORCID, and every visitor
        signs in as themselves.
        """
        accept = request.headers.get("accept", "")
        if "application/json" in accept and "text/html" not in accept:
            from ..api.models import ServiceDescription
            return JSONResponse(ServiceDescription(
                api_version="0.1.0", implementation="datadirector",
                deployment_profile=deployment_profile).model_dump())

        # Resolved leniently: the landing page must render for a visitor with
        # no session, an expired one, or a valid one, and the three differ only
        # in what it offers them.
        try:
            who = Caller(resolve_principal(request.cookies.get(COOKIE_NAME)),
                         set())
        except Exception:
            who = None
        return render(request, "landing.html", caller=who,
                      sign_in_available=sign_in_available,
                      local_accounts=local_accounts,
                      residencies=residencies)

    @app.post("/ui/jobs/{job_id}/advance", tags=["ui"])
    def ui_advance(request: Request, job_id: str,
                   who: Caller = Depends(caller)):
        """Run whatever this job can run now.

        A person presses it, so it is an action and not a page load: a GET that
        changed state would run on every refresh and on every link a browser
        chose to prefetch.

        Only an owner may. An auditor can watch a job and cannot move it, and
        advancing is moving it.
        """
        _exists(service, job_id)
        require_actor(service.store, job_id, who.orcid, roles=who.roles)
        if pipeline is None:
            return _problem(501, "This instance has no pipeline configured, so "
                                 "jobs cannot be advanced from here.")
        try:
            pipeline.advance(job_id)
        except Exception:
            # The engine records a halt with its reason, and the job page is
            # where a person should read it. An error screen over the top would
            # hide the record that explains what happened.
            pass
        return RedirectResponse(f"/ui/jobs/{job_id}", status_code=303)

    @app.get("/ui/jobs/{job_id}/declaration", response_class=HTMLResponse,
             tags=["ui"])
    def declaration_form(request: Request, job_id: str,
                         who: Caller = Depends(caller)):
        """The second screen: what the researcher says about their data.

        Separate from upload so the profile can be shown first. A statement
        written with "3 CSVs and 40 images" in view is about the actual
        material; one written blind is about the researcher's memory of it.
        """
        _exists(service, job_id)
        require_actor(service.store, job_id, who.orcid, roles=who.roles)
        return render(request, "declaration.html", caller=who, job_id=job_id,
                      summary=_material_summary(service, pipeline, job_id),
                      statement=_last_statement(service, job_id))

    @app.post("/ui/jobs/{job_id}/declaration", tags=["ui"])
    def declaration_submit(request: Request, job_id: str,
                           statement: str = Form(...),
                           who: Caller = Depends(caller)):
        """Read the statement into proposed claims.

        The statement is recorded verbatim before it is parsed, so what the
        researcher wrote survives independently of what was made of it.
        """
        _exists(service, job_id)
        require_actor(service.store, job_id, who.orcid, roles=who.roles)
        if pipeline is None:
            return _problem(501, "This instance has no pipeline configured.")
        pipeline.record_statement(job_id, statement, human=who.orcid)
        try:
            pipeline.parse_declaration(job_id, statement)
        except Exception as exc:
            return _problem(
                502, f"The statement could not be read: {exc}. It is recorded "
                     "as you wrote it; nothing has been inferred from it.")
        return RedirectResponse(f"/ui/jobs/{job_id}/declaration/confirm",
                                status_code=303)

    @app.get("/ui/jobs/{job_id}/declaration/confirm",
             response_class=HTMLResponse, tags=["ui"])
    def declaration_confirm_form(request: Request, job_id: str,
                                 who: Caller = Depends(caller)):
        from datadirector_contracts import SensitivityClass

        _exists(service, job_id)
        require_actor(service.store, job_id, who.orcid, roles=who.roles)
        return render(request, "declaration_confirm.html", caller=who,
                      job_id=job_id,
                      claims=pipeline.proposed_claims(job_id) if pipeline
                      else [],
                      levels=list(SensitivityClass))

    @app.post("/ui/jobs/{job_id}/declaration/confirm", tags=["ui"])
    def declaration_confirm(request: Request, job_id: str,
                            accept: list[str] = Form(default=[]),
                            sensitivity: str = Form(default=""),
                            who: Caller = Depends(caller)):
        """The nodal point: a proposal becomes authority, bound to an ORCID."""
        from datadirector_contracts import SensitivityClass

        _exists(service, job_id)
        require_actor(service.store, job_id, who.orcid, roles=who.roles)
        level = None
        if sensitivity:
            try:
                level = SensitivityClass(int(sensitivity))
            except (ValueError, TypeError):
                level = None
        service.confirm_declaration(
            job_id, human=who.orcid, sensitivity=level,
            note=f"claims confirmed: {sorted(accept)}" if accept
            else "no individual claim was confirmed")
        if pipeline is not None:
            try:
                pipeline.advance(job_id)
            except Exception:
                pass
        return RedirectResponse(f"/ui/jobs/{job_id}", status_code=303)

    @app.get("/ui/jobs/{job_id}/history", response_class=HTMLResponse,
             tags=["ui"])
    def history(request: Request, job_id: str, phase: str = "",
                who: Caller = Depends(caller)):
        """What happened, optionally narrowed to one phase.

        Narrowing is the point: "what happened during classification?" is the
        question a person has, and a flat log of forty entries does not answer
        it.
        """
        _exists(service, job_id)
        require_visible(service.store, job_id, who.orcid, roles=who.roles)
        events = service.events(job_id)
        state = service.state(job_id)
        shown = events_in_phase(events, phase) if phase else events
        label = next((lbl for key, lbl, _ in PHASES if key == phase), None)
        unresolved = len(service.gate(job_id).unresolved())
        return render(request, "history.html", caller=who, job_id=job_id,
                      entries=[{"at": e.occurred_at, "told": describe(e)}
                               for e in shown],
                      phase=phase, phase_label=label,
                      gate_unresolved=unresolved,
                      phases=phases_for(events, state,
                                        unresolved=unresolved),
                      total=len(events))

    @app.get("/ui/submit", response_class=HTMLResponse, tags=["ui"])
    def submit_form(request: Request, who: Caller = Depends(caller)):
        """The way in.

        Without this a researcher could review work in the browser but not
        start any, which made the interface a viewer of jobs created at a
        command line.
        """
        return render(request, "submit.html", caller=who,
                      backends=_backend_choices(pipeline),
                      unavailable=pipeline.runtime.unavailable()
                      if pipeline else [])

    @app.post("/ui/submit", tags=["ui"])
    async def submit(request: Request,
                     upload: UploadFile = File(...),
                     instruction: str = Form(default=""),
                     dmp: str = Form(default=""),
                     residency: str = Form(default=""),
                     backend: str = Form(default=""),
                     who: Caller = Depends(caller)):
        """Take the upload, create the job, and start it.

        The upload is written to a temporary path and handed to the same
        pipeline the command line uses. There is no browser-only path into the
        system: a second route into ingestion would be a second place for the
        rules to be applied differently.
        """
        if pipeline is None:
            return _problem(501, "This instance has no pipeline configured, so "
                                 "it cannot accept submissions.")

        name = Path(upload.filename or "submission").name
        staging = Path(tempfile.mkdtemp(prefix="dd-upload-")) / name
        staging.parent.mkdir(parents=True, exist_ok=True)
        with open(staging, "wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                handle.write(chunk)

        result = pipeline.ingest(
            staging, human=who.orcid,
            dmp_reference=dmp.strip() or None,
            instruction=instruction.strip() or None,
            preference=_preference_from(residency, backend))
        for item in result.gate_items:
            pipeline.runtime.store.append(_gate_event(result.job_id, [item]))

        # Straight to the second screen. Nothing can read the data until the
        # researcher has said what it is, so sending them to the job page would
        # show a job that is waiting for something they have not been asked for.
        return RedirectResponse(f"/ui/jobs/{result.job_id}/declaration",
                                status_code=303)

    @app.exception_handler(NotVisible)
    def _hidden(request: Request, exc: NotVisible):
        # 404 in the interface too: a page saying "you may not see this job"
        # confirms the job exists.
        return _problem(404, "No job by that name.")

    @app.exception_handler(AuthorityError)
    def _refused(request: Request, exc: AuthorityError):
        """A rule refused this, and the reason is the useful part.

        Without this the domain layer's refusals reached the browser as a 500:
        the rules fired correctly and the page crashed, which reads to a user as
        a broken tool rather than as a decision they are not entitled to make.
        """
        return _problem(409, str(exc))

    @app.exception_handler(CredentialError)
    def _credential(request: Request, exc: CredentialError):
        return _problem(412, str(exc))

    # -- jobs --------------------------------------------------------------

    @app.get("/ui/jobs/sole-owned", response_class=HTMLResponse, tags=["ui"])
    def ui_sole_owned(request: Request, who: Caller = Depends(caller)):
        jobs = [_summary(service, job_id)
                for job_id in sole_owned_by(service.store, who.orcid)]
        return render(request, "jobs.html", caller=who, jobs=jobs,
                      heading="Jobs you alone own", sole_owned_notice=True)

    @app.get("/ui/jobs", response_class=HTMLResponse, tags=["ui"])
    def ui_jobs(request: Request, who: Caller = Depends(caller)):
        jobs = [_summary(service, job_id)
                for job_id in jobs_for(service.store, who.orcid, roles=who.roles)]
        return render(request, "jobs.html", caller=who, jobs=jobs,
                      heading="Your jobs", sole_owned_notice=False)

    @app.get("/ui/jobs/{job_id}", response_class=HTMLResponse, tags=["ui"])
    def ui_job(request: Request, job_id: str, who: Caller = Depends(caller)):
        _exists(service, job_id)
        ownership = require_visible(service.store, job_id, who.orcid,
                                    roles=who.roles)
        events = service.events(job_id)
        state = service.state(job_id)
        gate = service.gate(job_id)
        # Offered only to someone who could act on it: a button an auditor
        # cannot use is a button that teaches them the interface is broken.
        can_advance = pipeline is not None and not state.terminal \
            and ownership.owned_by(who.orcid)
        ready_to_publish = _ready_to_publish(pipeline, state, gate, job_id,
                                             ownership.owned_by(who.orcid))
        notice = NOTICES.get(request.query_params.get("notice", ""))
        return render(request, "job.html", caller=who,
                      job=_summary(service, job_id),
                      ownership=_ownership_view(ownership),
                      phases=phases_for(events, state,
                                        unresolved=len(gate.unresolved())),
                      can_advance=can_advance,
                      ready_to_publish=ready_to_publish,
                      notice=notice,
                      events=events)

    @app.post("/ui/jobs/{job_id}/owners", tags=["ui"])
    def ui_add_owner(request: Request, job_id: str, orcid: str = Form(),
                     reason: str = Form(default=""),
                     who: Caller = Depends(caller)):
        _exists(service, job_id)
        require_actor(service.store, job_id, who.orcid, roles=who.roles)
        try:
            add_owner(service.store, job_id, Orcid(value=orcid.strip()),
                      by=who.orcid, reason=reason.strip() or None)
        except ValueError:
            raise HTTPException(status_code=422,
                                detail="That is not a valid ORCID.") from None
        return RedirectResponse(f"/ui/jobs/{job_id}", status_code=303)

    # -- the gate ----------------------------------------------------------

    @app.get("/ui/jobs/{job_id}/gate", response_class=HTMLResponse, tags=["ui"])
    def ui_gate(request: Request, job_id: str, who: Caller = Depends(caller)):
        """Show the items, and record that they were shown.

        §4.1 claims rubber-stamping is detectable from approval latency, and
        that claim only holds if display is recorded as well as decision.
        Without a display time there is no interval to measure.
        """
        _exists(service, job_id)
        require_visible(service.store, job_id, who.orcid, roles=who.roles)
        gate = service.gate(job_id)
        resolved = {r.item_id: r for r in gate.state.resolutions}

        items = [_item_view(item, resolved.get(item.item_id))
                 for item in gate.state.items]
        pending = [i for i in items if not i["resolved"]]

        # Only a person who could act on these items counts as having been shown
        # them. An auditor looking at the gate is not reviewing for approval,
        # and recording their view would put intervals into the latency
        # measurement that no decision will ever close.
        may_act = _ownership_permits_action(service, job_id, who)
        if pending and may_act:
            service.store.append(_displayed_event(job_id, who.orcid,
                                                  len(pending)))
        return render(request, "gate.html", caller=who, job_id=job_id,
                      items=items, unresolved=len(gate.unresolved()),
                      ready_to_publish=_ready_to_publish(pipeline,
                                                         service.state(job_id),
                                                         gate, job_id, may_act))

    @app.post("/ui/jobs/{job_id}/gate/{item_id:path}", tags=["ui"])
    def ui_resolve(request: Request, job_id: str, item_id: str,
                   decision: str = Form(), reason: str = Form(default=""),
                   who: Caller = Depends(caller)):
        """One item, one decision, one request.

        The path carries a single identifier and the form a single decision.
        There is no route that accepts several.
        """
        _exists(service, job_id)
        require_actor(service.store, job_id, who.orcid, roles=who.roles)
        try:
            chosen = ItemDecision(decision)
        except ValueError:
            raise HTTPException(status_code=422,
                                detail="Unknown decision.") from None
        if pipeline is not None and review_items.agent_of(item_id) is not None:
            # A decision about what a model wrote goes through the pipeline,
            # not the core service, for one reason: asking for another attempt
            # has to run the agent again. Both entry points reach the same
            # `Gate.resolve`, so neither can accept what the other would refuse.
            agent = review_items.agent_of(item_id)
            if chosen is ItemDecision.REQUEST_RERUN:
                pipeline.request_rerun(job_id, agent, human=who.orcid,
                                       note=reason.strip())
            else:
                pipeline.resolve_review(job_id, item_id, chosen,
                                          human=who.orcid,
                                          reason=reason.strip() or None)
            return RedirectResponse(f"/ui/jobs/{job_id}/gate",
                                    status_code=303)
        service.resolve(job_id, item_id, chosen, human=who.orcid,
                        reason=reason.strip() or None)
        return RedirectResponse(f"/ui/jobs/{job_id}/gate", status_code=303)

    # -- deposit -----------------------------------------------------------

    @app.get("/ui/jobs/{job_id}/deposit", response_class=HTMLResponse,
             tags=["ui"])
    def ui_deposit_form(request: Request, job_id: str,
                        who: Caller = Depends(caller)):
        _exists(service, job_id)
        require_visible(service.store, job_id, who.orcid, roles=who.roles)
        state = service.state(job_id)
        gate = service.gate(job_id)
        excluded = set(gate.state.excluded_artefacts())
        blocked = [i.summary for i in gate.unresolved()]
        record = recordedit.drafted_record(service, job_id)
        blocking, advisory = recordedit.split_findings(
            service.validation_findings(job_id))
        return render(request, "deposit.html", caller=who, job_id=job_id,
                      repository=repository, blocked=blocked, record=record,
                      blocking=blocking, advisory=advisory,
                      may_act=_ownership_permits_action(service, job_id, who),
                      uploading=[m for m in state.material if m not in excluded],
                      excluded=sorted(excluded))

    @app.post("/ui/jobs/{job_id}/deposit", tags=["ui"])
    def ui_deposit(request: Request, job_id: str,
                   confirm_irreversible: str = Form(default=""),
                   who: Caller = Depends(caller)):
        _exists(service, job_id)
        require_actor(service.store, job_id, who.orcid, roles=who.roles)
        if not confirm_irreversible:
            raise HTTPException(
                status_code=428,
                detail="Publishing cannot be undone; confirm to proceed.")
        result = service.deposit(job_id, human=who.orcid, repository=repository)
        if result.get("nothing_to_deposit"):
              # The researcher withheld every file. That is a decision honoured,
              # not a failure, so the job page says what happened rather than the
              # request ending in an unhandled exception over an upload list that
              # came out empty.
            return RedirectResponse(
                f"/ui/jobs/{job_id}?notice=nothing-to-deposit",
                status_code=303)
        return RedirectResponse(f"/ui/jobs/{job_id}", status_code=303)

    # -- metadata review and correction ---------------------------------

    # The deposit is refused while a validation error stands, and a required
    # field no agent is allowed to invent (creators) means the researcher is
    # the only one who can supply it. So the record is shown here for review
    # and corrected here, then re-checked offline against the same standard
    # the graph runs -- not a second, web-only notion of what is valid.
    # Publishing stays the separate, confirmed act on the deposit screen.
    @app.get("/ui/jobs/{job_id}/metadata", response_class=HTMLResponse,
             tags=["ui"])
    def ui_metadata_edit(request: Request, job_id: str,
                        who: Caller = Depends(caller)):
        _exists(service, job_id)
        require_visible(service.store, job_id, who.orcid, roles=who.roles)
        record = recordedit.drafted_record(service, job_id)
        form = recordedit.record_form(record)
        blocking, advisory = recordedit.split_findings(
            service.validation_findings(job_id))
        if not form["creators"]:
            # Prefill a creator from the signed-in ORCID when the draft has none:
            # the identifier is what the system genuinely knows about this person,
            # and creators is the one field that blocks every deposit because no
            # agent may invent one. The name is left blank, not guessed.
            form["creators"] = [{"name": "", "orcid": who.orcid.value,
                              "given_name": "", "family_name": ""}]
        return render(request, "metadata_edit.html", caller=who,
               job_id=job_id, form=form, blocking=blocking,
                advisory=advisory,
                notes=recordedit.editor_notes(service, job_id),
                resource_types=recordedit.resource_types())

    # Saving an edit appends a new draft (the old stays in the log) and
    # re-runs validation, so the researcher sees the blocking finding cleared
    # or remaining on the review screen rather than only at publish.
    @app.post("/ui/jobs/{job_id}/metadata", tags=["ui"])
    async def ui_metadata_save(request: Request, job_id: str,
                               who: Caller = Depends(caller)):
        _exists(service, job_id)
        require_actor(service.store, job_id, who.orcid, roles=who.roles)
        fields = await request.form()
        if (fields.get("action") or "") == "draft":
               # Draft again, not save: the drafting agents read the log, so a
               # job whose depositor has said more, confirmed claims, or supplied
               # a plan since the first draft gets a record written from what is
               # now known. Nothing is overwritten - a draft is appended, and a
               # record a person last revised is left to them. If the drafting
               # cannot run, the record on the log is untouched and the
               # researcher lands back on the same screen, notes still standing.
            if pipeline is not None:
                try:
                    pipeline.redraft_metadata(job_id)
                except Exception:
                    pass
            return RedirectResponse(f"/ui/jobs/{job_id}/metadata",
                                     status_code=303)

        existing = recordedit.drafted_record(service, job_id)
        revised = recordedit.revised_record(existing, fields, who)
        service.revise_metadata(job_id, revised, human=who.orcid)
        if pipeline is not None:
            try:
                pipeline.revalidate_metadata(job_id)
            except Exception:
                # If the re-check cannot run, nothing publishes unchecked: the deposit
                # still refuses on the findings it was last shown.
                pass
        return RedirectResponse(f"/ui/jobs/{job_id}/deposit",
               status_code=303)

    return app


def _problem(status: int, message: str) -> HTMLResponse:
    """A refusal a person can read.

    Deliberately plain HTML rather than a JSON body: this surface is a browser,
    and a raw payload would tell a researcher nothing about what to do next.
    """
    safe = (message.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
    return HTMLResponse(status_code=status, content=(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>Not permitted</title>"
        "<link rel=\"stylesheet\" href=\"/static/style.css\"></head><body>"
        "<main id=\"main\"><h1>That is not permitted</h1>"
        f"<p role=\"alert\">{safe}</p>"
        "<p><a href=\"/ui/jobs\">Back to your jobs</a></p>"
        "</main></body></html>"))


def _backend_choices(pipeline) -> list[dict]:
    """The models this installation has, for a depositor to choose among.

    Listed with their residency, because where a model runs is the part of this
    choice a researcher is genuinely well placed to judge. Which model
    classifies better is not, and the page does not pretend otherwise.
    """
    if pipeline is None:
        return []
    out = []
    for declaration in pipeline.runtime.config.wiring.backends:
        if declaration.name in pipeline.runtime.backends:
            out.append({"name": declaration.name,
                        "residency": declaration.residency.value,
                        "model": declaration.model})
    return out


def _preference_from(residency: str, backend: str):
    """Build a preference, or nothing.

    An unrecognised residency becomes no ceiling rather than an error: the
    alternative is refusing a submission over a form value, and policy still
    governs regardless.
    """
    from datadirector_contracts import BackendPreference, Residency

    ceiling = None
    if residency:
        try:
            ceiling = Residency(residency)
        except ValueError:
            ceiling = None
    names = [backend] if backend else []
    preference = BackendPreference(residency_at_most=ceiling,
                                   backend_names=names)
    return None if preference.is_empty else preference


def _material_summary(service: JobService, pipeline, job_id: str) -> dict:
    """What arrived, in terms a researcher recognises.

    File counts and extensions rather than a profile dump: the purpose is to
    remind someone what they uploaded, not to show them a data structure.
    """
    from collections import Counter

    state = service.state(job_id)
    material = list(state.material)
    kinds = Counter(Path(name).suffix.lower().lstrip(".") or "no extension"
                    for name in material)

    size = 0
    columns: list[str] = []
    nested: list[str] = []
    if pipeline is not None:
        root = pipeline.runtime.unpacked_root(job_id)
        if root.exists():
            size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        for event in service.events(job_id):
            if event.payload.get("nested_archives"):
                nested = list(event.payload["nested_archives"])

    return {"file_count": len(material),
            "size_human": _human_size(size),
            "kinds": sorted(kinds.items(), key=lambda kv: -kv[1]),
            "columns": columns[:20], "nested": nested}


def _human_size(size: int) -> str:
    for unit in ("bytes", "kB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _last_statement(service: JobService, job_id: str) -> str | None:
    """The statement as last written, so going back does not lose it."""
    for event in reversed(service.events(job_id)):
        if event.payload.get("statement"):
            return event.payload["statement"]
    return None


def _gate_event(job_id: str, items):
    """Record gate items raised at submission, so they survive the request."""
    import json as _json

    from datadirector_contracts import Event, EventKind
    return Event(
        sequence=1, job_id=job_id, kind=EventKind.REDACTION_PROPOSED,
        agent="web/0.1.0",
        payload={"marker": "gate.items-added",
                 "items": [_json.loads(i.model_dump_json()) for i in items]})


# -- view assembly ---------------------------------------------------------

def _ownership_permits_action(service: JobService, job_id: str, who) -> bool:
    try:
        require_actor(service.store, job_id, who.orcid, roles=who.roles)
    except Exception:
        return False
    return True


def _exists(service: JobService, job_id: str) -> None:
    if not service.events(job_id):
        raise HTTPException(status_code=404, detail="No job by that name.")


def _ready_to_publish(pipeline, state, gate, job_id: str, may_act: bool) -> bool:
    """Whether the only thing left before a persistent identifier is the
    human act of publishing.

    Read off the graph rather than off a phase label, in the same way the
    engine decides what may run next. `deposit` is a human node, so `advance`
    can never run it and the "Continue" button would sit there doing nothing
    while the job waits on a person. The job page and the gate page therefore
    have to offer the deposit screen themselves, and they can only know to do
    so when the graph says the deposit node — and nothing else — is what
    stands between this job and being published.
    """
    if (pipeline is None or state.terminal or state.halted_reason
            or state.deposit_pid or not may_act or gate.unresolved()):
        return False
    done = pipeline.completed(job_id)
    if pipeline.graph.next_node(state, done=done) is not None:
        return False    # automatic work still remains: "Continue" is the action
    blocking = pipeline.graph.blocking_human_node(state, done=done)
    return blocking is not None and blocking.name == "deposit"


def _summary(service: JobService, job_id: str):
    """One job as the list and the job page see it.

    Carries the *current phase* rather than the internal step name. "Working out
    what is sensitive" is something a researcher understands; "classification"
    is the name of an agent.
    """
    events = service.events(job_id)
    state = fold(events)
    gate = service.gate(job_id)
    phases = phases_for(events, state)
    # The phase a person should look at: the one in progress, the one needing
    # them, or the one that stopped. Falling back to the *last* phase showed
    # "Publishing — pending" for a job that had just arrived, which is worse
    # than saying nothing.
    current = next(
        (p for p in phases
         if p.state.value in ("current", "waiting-on-you", "halted")),
        next((p for p in phases if p.state.value not in ("done", "skipped")),
             phases[-1]))
    return type("JobView", (), {
        "job_id": job_id, "step": state.step,
        "phase_label": current.label, "phase_state": current.state.value,
        "phase_detail": current.detail,
        "halted_reason": state.halted_reason,
        "sensitivity": (state.classification.level.label
                        if state.classification else None),
        "material": list(state.material), "deposit_pid": state.deposit_pid,
        "gate_unresolved": len(gate.unresolved()),
    })()


def _ownership_view(ownership):
    return {
        "sole_owned": ownership.is_sole_owned,
        "grants": [{"owner": g.owner.value,
                    "granted_by": g.granted_by.value if g.granted_by else None,
                    "reason": g.reason} for g in ownership.grants],
    }


def _item_view(item, resolution) -> dict:
    """Assemble what the screen needs, including what the item is *not*.

    A presumption is detected from the item's own detail lines rather than
    guessed from its kind, because the domain layer already words it: an
    uninspected artefact records that its classification rests on a presumption,
    and the screen repeats that rather than inventing its own phrasing.
    """
    detail = list(item.detail)
    lowered = " ".join(detail + [item.summary]).lower()
    presumption = any(marker in lowered for marker in PRESUMPTION_MARKERS)
    reason_required = [d.value for d in item.permitted_decisions
                       if d in REASON_REQUIRED]
    return {
        "item_id": item.item_id,
        "slug": re.sub(r"[^a-z0-9]+", "-", item.item_id.lower()).strip("-"),
        "kind": item.kind.value,
        "artefact": item.artefact,
        "summary": _display_summary(item),
        "detail": detail,
        "permitted_decisions": [d.value for d in item.permitted_decisions],
        "decision_labels": {d.value: DECISION_LABELS.get(d, d.value)
                            for d in item.permitted_decisions},
        "reason_required": [REASON_REQUIRED[ItemDecision(d)]
                            for d in reason_required],
        "presumption": presumption,
        "presumed_level": _presumed_level(detail),
        "confidence": _confidence(item),
        "diff": _diff(item),
        "resolved": resolution is not None,
        "decision": resolution.decision.value if resolution else None,
        "resolved_by": resolution.decided_by.value if resolution else None,
    }



def _display_summary(item) -> str:
    """The item's summary, with any bare treatment verb removed.

    Gate items are persisted in the log as they were first worded, so a
    proposal logged under an older format would still read as a verb the
    tool performs ("pseudonymise"). Where a proposal is present the line
    is rebuilt from it in the conditional, as the history page rebuilds
    its lines.
    """
    proposal = getattr(item, "proposal", None)
    if proposal is None:
        return item.summary
    treatment = getattr(proposal.treatment, "value",
                        str(proposal.treatment)).lower()
    words = PROPOSAL_WORDS.get(
        treatment, f"a redaction made outside this tool would apply the "
        f"treatment '{treatment}' to the values")
    reason = getattr(proposal.reason_code, "value",
                     str(proposal.reason_code))
    return (f"Proposal for {proposal.artefact} · {proposal.location}: "
            f"{words} (reason: {reason}). This tool edits no file; the "
            f"decision you record is your answer to the proposal")


def _presumed_level(detail: list[str]) -> str | None:
    for line in detail:
        match = re.search(r"presumed (\w+)", line.lower())
        if match:
            return match.group(1)
    return None


def _confidence(item) -> float | None:
    proposal = getattr(item, "proposal", None)
    return getattr(proposal, "confidence", None) if proposal else None


def _diff(item) -> list[dict]:
    """A field-level sketch of what a proposal describes.  No value is changed
    by this tool - the table names what an edit made outside it would cover.


    "Redact column 3" is not reviewable; showing what a value is and what it
    would become is. Only a proposal carries enough to build one.
    """
    proposal = getattr(item, "proposal", None)
    if proposal is None:
        return []
    return [{"location": proposal.location,
             "before": getattr(proposal, "before", "the value, as it stands, untouched"),
             "after": _after(proposal)}]


def _after(proposal) -> str:
    treatment = getattr(proposal.treatment, "value", str(proposal.treatment))
    return {
        "suppress": "(values removed)",
        "generalise": "(values replaced by a broader category)",
        "pseudonymise": "(values replaced by a stable non-identifying code)",
        "coarsen": "(values reduced in precision)",
    }.get(treatment, treatment)


def _displayed_event(job_id: str, who: Orcid, count: int):
    """Record that items were put in front of a person.

    Payload-free beyond the count: this is about the interaction, not the
    material.
    """
    from datadirector_contracts import Event
    return Event(sequence=1, job_id=job_id, kind=EventKind.GATE_ITEMS_DISPLAYED,
                 agent="web/0.1.0", human=who, payload={"items": count})
