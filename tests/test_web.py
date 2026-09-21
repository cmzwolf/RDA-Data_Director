"""The web interface, with the gate screen as its centre.

What is tested is mostly what the screen refuses to offer. A gate that can be
cleared with one action has not been reviewed, and the interface is the last
place that rule can be quietly broken.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="pip install -e 'packages/datadirector[api]'")
pytest.importorskip("jinja2", reason="pip install -e 'packages/datadirector[api]'")
pytest.importorskip("multipart",
                    reason="pip install -e 'packages/datadirector[api]' for "
                           "HTML form handling")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from datadirector_contracts import (  # noqa: E402
    AccessRole, EventKind, GateItem, GateItemKind, ItemDecision, Orcid,
    ReasonCode, RedactionProposal, Treatment,
)

from datadirector.api.service import JobService  # noqa: E402
from datadirector.identity.session import COOKIE_NAME, SessionStore  # noqa: E402
from datadirector.state.store import EventStore  # noqa: E402
from datadirector.web.views import build_web  # noqa: E402

AUDITOR = Orcid(value="0000-0003-1111-222X")


@pytest.fixture
def service(tmp_path):
    return JobService(EventStore(tmp_path / "state"))


@pytest.fixture
def sessions(tmp_path):
    return SessionStore(tmp_path / "sessions.json")


@pytest.fixture
def web(service, sessions, researcher, data_steward):
    from datadirector.api.auth import SessionResolver

    resolver = SessionResolver(sessions, {AUDITOR.value: [AccessRole.AUDITOR]})
    app = build_web(FastAPI(), service, resolve_principal=resolver,
                    resolve_roles=resolver.roles_for)
    client = TestClient(app, follow_redirects=False)

    def sign_in(who: Orcid) -> TestClient:
        session = sessions.create(who)
        client.cookies.set(COOKIE_NAME, session.identifier)
        return client

    return client, sign_in


def _job(service, owner: Orcid) -> str:
    return service.create(human=owner)


def _uninspected(name="plate.png"):
    return GateItem(
        item_id=f"uninspected:{name}", kind=GateItemKind.UNINSPECTED_FILE,
        artefact=name,
        summary=f"{name} was not inspected (no-capable-backend). "
                "Treated as sensitive by presumption.",
        detail=["not inspected: no-capable-backend",
                "presumed sensitive on the basis of medium alone, "
                "not on inspection"],
        permitted_decisions=[ItemDecision.PUBLISH_AS_IS,
                             ItemDecision.EXCLUDE_FROM_DEPOSIT,
                             ItemDecision.INSPECTED_EXTERNALLY])


def _redaction():
    return GateItem(
        item_id="redaction:responses.csv:village",
        kind=GateItemKind.REDACTION_PROPOSAL, artefact="responses.csv",
        summary="responses.csv · village: coarsen (personal-data)",
        detail=["village combined with role identifies one person"],
        proposal=RedactionProposal(
            artefact="responses.csv", location="village",
            reason_code=ReasonCode.PERSONAL_DATA, treatment=Treatment.COARSEN,
            evidence="village combined with role identifies one person"),
        permitted_decisions=[ItemDecision.APPROVE, ItemDecision.REJECT])


def _care():
    return GateItem(
        item_id="care:referral", kind=GateItemKind.CARE_REFERRAL,
        summary="This material may engage the CARE Principles.",
        detail=["consult the community or governance body concerned"],
        permitted_decisions=[ItemDecision.CONSULTED,
                             ItemDecision.NOT_APPLICABLE,
                             ItemDecision.EXCLUDE_FROM_DEPOSIT])


# -- access ---------------------------------------------------------------

def test_an_unauthenticated_visitor_is_sent_to_sign_in(web):
    """A sign-in page is the useful response in a browser, not a 401 body."""
    client, _ = web
    response = client.get("/ui/jobs")
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_a_job_you_do_not_own_is_absent(web, service, researcher, data_steward):
    client, sign_in = web
    job_id = _job(service, researcher)
    sign_in(data_steward)
    assert client.get(f"/ui/jobs/{job_id}").status_code == 404


def test_your_jobs_list_shows_only_yours(web, service, researcher,
                                         data_steward):
    client, sign_in = web
    mine = _job(service, researcher)
    theirs = _job(service, data_steward)
    sign_in(researcher)
    body = client.get("/ui/jobs").text
    assert mine in body and theirs not in body


def test_the_sole_owned_list_is_reachable(web, service, researcher):
    """The list a researcher needs before leaving an institution."""
    client, sign_in = web
    job_id = _job(service, researcher)
    sign_in(researcher)
    response = client.get("/ui/jobs/sole-owned")
    assert response.status_code == 200
    assert job_id in response.text


# -- the gate screen ------------------------------------------------------

def test_there_is_no_bulk_accept(web, service, researcher):
    """A reviewer who can clear forty items with one action has reviewed
    nothing, and the interface is the last place that can be broken."""
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_uninspected("a.png"), _uninspected("b.png")])
    sign_in(researcher)
    body = client.get(f"/ui/jobs/{job_id}/gate").text

    lowered = body.lower()
    for phrase in ("select all", "approve all", "accept all", "apply to all",
                   'type="checkbox"'):
        assert phrase not in lowered, f"the gate offers {phrase!r}"

    # One form per item, each posting to that item's own path.
    assert len(re.findall(r'<form[^>]+method="post"', body)) >= 2


def test_no_decision_control_is_preselected(web, service, researcher):
    """A default is a recommendation, and the system does not get to recommend
    what a person should decide."""
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_uninspected()])
    sign_in(researcher)
    body = client.get(f"/ui/jobs/{job_id}/gate").text
    assert "checked" not in body.lower()
    assert "selected" not in body.lower()


def test_an_uninspected_item_reads_as_a_presumption(web, service, researcher):
    """'Not inspected, presumed sensitive' must not read like 'inspected, found
    sensitive'."""
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_uninspected()])
    sign_in(researcher)
    body = client.get(f"/ui/jobs/{job_id}/gate").text
    assert "not inspected" in body.lower()
    assert "presum" in body.lower()


def test_a_care_referral_offers_no_approval(web, service, researcher):
    """Approving would imply the tool had assessed something. It has not."""
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_care()])
    sign_in(researcher)
    body = client.get(f"/ui/jobs/{job_id}/gate").text
    assert 'value="approve"' not in body
    assert 'value="consulted"' in body


def test_a_redaction_shows_what_would_change(web, service, researcher):
    """'Redact column 3' is not reviewable; a before and after is."""
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_redaction()])
    sign_in(researcher)
    body = client.get(f"/ui/jobs/{job_id}/gate").text
    assert "village" in body
    assert "reduced precision" in body.lower()


def test_the_evidence_is_shown_beside_the_proposal(web, service, researcher):
    """The system decides what to show; the human decides."""
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_redaction()])
    sign_in(researcher)
    body = client.get(f"/ui/jobs/{job_id}/gate").text
    assert "identifies one person" in body


def test_one_item_is_resolved_per_request(web, service, researcher):
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_uninspected("a.png"), _uninspected("b.png")])
    sign_in(researcher)

    response = client.post(f"/ui/jobs/{job_id}/gate/uninspected:a.png",
                           data={"decision": "exclude-from-deposit"})
    assert response.status_code == 303
    assert len(service.gate(job_id).unresolved()) == 1


def test_a_decision_needing_a_reason_is_refused_without_one(web, service,
                                                            researcher):
    """The domain layer refuses it; the form says so too, because a rejection
    after the fact teaches a user to type whatever passes."""
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_uninspected()])
    sign_in(researcher)

    body = client.get(f"/ui/jobs/{job_id}/gate").text
    assert "required" in body.lower()

    response = client.post(f"/ui/jobs/{job_id}/gate/uninspected:plate.png",
                           data={"decision": "publish-as-is", "reason": ""})
    assert response.status_code == 409
    assert service.gate(job_id).blocks_deposit()


def test_an_invented_decision_is_refused(web, service, researcher):
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_uninspected()])
    sign_in(researcher)
    response = client.post(f"/ui/jobs/{job_id}/gate/uninspected:plate.png",
                           data={"decision": "looks-fine"})
    assert response.status_code == 422


# -- the measurement §4.1 depends on --------------------------------------

def test_displaying_items_is_recorded(web, service, researcher):
    """Without a display time there is no interval, and §4.1's claim that
    rubber-stamping is detectable from approval latency does not hold."""
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_uninspected()])
    sign_in(researcher)
    client.get(f"/ui/jobs/{job_id}/gate")

    shown = [e for e in service.events(job_id)
             if e.kind is EventKind.GATE_ITEMS_DISPLAYED]
    assert shown and shown[0].human == researcher
    assert shown[0].payload == {"items": 1}


def test_an_auditors_view_is_not_counted_as_a_display(web, service, researcher):
    """An auditor is not reviewing for approval, and counting their view would
    open an interval no decision will ever close."""
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_uninspected()])
    sign_in(AUDITOR)
    assert client.get(f"/ui/jobs/{job_id}/gate").status_code == 200

    assert not [e for e in service.events(job_id)
                if e.kind is EventKind.GATE_ITEMS_DISPLAYED]


def test_an_auditor_cannot_resolve(web, service, researcher):
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_uninspected()])
    sign_in(AUDITOR)
    response = client.post(f"/ui/jobs/{job_id}/gate/uninspected:plate.png",
                           data={"decision": "exclude-from-deposit"})
    assert response.status_code == 409


# -- the deposit screen ----------------------------------------------------

def test_deposit_names_what_will_and_will_not_be_uploaded(web, service,
                                                          researcher):
    """The last point at which a mistake is cheap."""
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_uninspected("secret.png")])
    service.resolve(job_id, "uninspected:secret.png",
                    ItemDecision.EXCLUDE_FROM_DEPOSIT, human=researcher)
    sign_in(researcher)

    body = client.get(f"/ui/jobs/{job_id}/deposit").text
    assert "secret.png" in body
    assert "not be" in body.lower() or "excluded" in body.lower()


def test_deposit_states_that_it_cannot_be_undone(web, service, researcher):
    client, sign_in = web
    job_id = _job(service, researcher)
    sign_in(researcher)
    body = client.get(f"/ui/jobs/{job_id}/deposit").text.lower()
    assert "cannot be undone" in body or "irreversible" in body


def test_deposit_requires_an_explicit_acknowledgement(web, service, researcher):
    client, sign_in = web
    job_id = _job(service, researcher)
    sign_in(researcher)
    response = client.post(f"/ui/jobs/{job_id}/deposit", data={})
    assert response.status_code == 428


def test_an_open_gate_is_visible_on_the_deposit_screen(web, service,
                                                       researcher):
    client, sign_in = web
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_care()])
    sign_in(researcher)
    body = client.get(f"/ui/jobs/{job_id}/deposit").text
    assert "CARE" in body



# -- reaching publication from the interface --------------------------------
#
# `deposit` is a human node, so the engine's "Continue" (advance) can never run
# it: a job that has finished review and validation would sit at "Publishing"
# offering only a button that does nothing, and the researcher would never reach
# the deposit screen at all. What keeps that from recurring is the job page and
# the cleared gate page offering the link themselves.

class _Node:
    """A graph node, named, for a pipeline stand-in."""

    def __init__(self, name):
        self.name = name


class _Graph:
    def __init__(self, next_auto=None, blocking="deposit"):
        self._next = next_auto
        self._blocking = None if blocking is None else _Node(blocking)

    def next_node(self, state, *, done=None):
        return self._next

    def blocking_human_node(self, state, *, done=None):
        return self._blocking


class _Pipeline:
    def __init__(self, next_auto=None, blocking="deposit"):
        self.graph = _Graph(next_auto=next_auto, blocking=blocking)

    def completed(self, job_id):
        return {"review"}


class _State:
    def __init__(self, terminal=False, halted=None, deposit_pid=None):
        self.terminal = terminal
        self.halted_reason = halted
        self.deposit_pid = deposit_pid


class _Gate:
    def __init__(self, unresolved=()):
        self._unresolved = list(unresolved)

    def unresolved(self):
        return self._unresolved


def test_publish_is_offered_only_when_publishing_is_all_that_remains():
    """Read off the graph, not a phase label: publish only when the deposit node
    — and nothing else — is what stands between the job and a PID."""
    from datadirector.web.views import _ready_to_publish

    assert _ready_to_publish(_Pipeline(), _State(), _Gate(), "j", True) is True
    cases = {
        "no pipeline": (None, _State(), _Gate(), "j", True),
        "terminal": (_Pipeline(), _State(terminal=True), _Gate(), "j", True),
        "halted": (_Pipeline(), _State(halted="boom"), _Gate(), "j", True),
        "already published": (_Pipeline(), _State(deposit_pid="10.5/x"),
                              _Gate(), "j", True),
        "gate open": (_Pipeline(), _State(), _Gate(["item"]), "j", True),
        "not an owner": (_Pipeline(), _State(), _Gate(), "j", False),
        "auto work remains": (_Pipeline(next_auto=_Node("metadata")),
                              _State(), _Gate(), "j", True),
        "still reviewing": (_Pipeline(blocking="review"), _State(), _Gate(),
                            "j", True),
    }
    for label, args in cases.items():
        assert _ready_to_publish(*args) is False, label


def _client_with_pipeline(service, sessions, who):
    """The interface wired to a pipeline whose graph says only publishing is
    left, so the link can be exercised without running any agent."""
    from fastapi import FastAPI

    from datadirector.api.auth import SessionResolver

    resolver = SessionResolver(sessions, {AUDITOR.value: [AccessRole.AUDITOR]})
    app = build_web(FastAPI(), service, pipeline=_Pipeline(),
                    resolve_principal=resolver, resolve_roles=resolver.roles_for)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(COOKIE_NAME, sessions.create(who).identifier)
    return client


def test_the_job_page_links_to_publish_when_only_publishing_remains(
        service, sessions, researcher):
    """The dead end that prompted this: at the publishing phase the job page
    offered a Continue that ran nothing and no way to the deposit screen."""
    job_id = _job(service, researcher)
    body = _client_with_pipeline(service, sessions, researcher).get(
        f"/ui/jobs/{job_id}").text
    assert f'href="/ui/jobs/{job_id}/deposit"' in body, (
        "the publishing phase offers no link to the deposit screen")
    assert ">Publish<" in body
    assert "/advance" not in body, (
        "the no-op Continue form is still shown where it can do nothing")


def test_a_cleared_gate_links_to_publish(service, sessions, researcher):
    """After the last decision the gate screen said only 'Nothing is waiting'
    and gave no way forward; the deposit node being human, that was a dead end."""
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_uninspected()])
    service.resolve(job_id, "uninspected:plate.png",
                    ItemDecision.EXCLUDE_FROM_DEPOSIT, human=researcher)
    body = _client_with_pipeline(service, sessions, researcher).get(
        f"/ui/jobs/{job_id}/gate").text
    assert f'href="/ui/jobs/{job_id}/deposit"' in body


def test_the_publish_link_is_an_anchor_not_a_form_button(service, sessions,
                                                         researcher):
    """Publishing itself stays a deliberate, confirmed action on the deposit
    screen; from the job page it is a link to that screen, not a one-click post."""
    job_id = _job(service, researcher)
    body = _client_with_pipeline(service, sessions, researcher).get(
        f"/ui/jobs/{job_id}").text
    link = re.search(
        r'<a class="button"[^>]*href="/ui/jobs/%s/deposit"' % job_id, body)
    assert link, ("the publish entry point is not a link styled like the other"
                  " navigation links")

# -- ownership through the interface --------------------------------------

def test_an_owner_can_add_an_owner(web, service, researcher, data_steward):
    client, sign_in = web
    job_id = _job(service, researcher)
    sign_in(researcher)
    response = client.post(f"/ui/jobs/{job_id}/owners",
                           data={"orcid": data_steward.value,
                                 "reason": "covering my leave"})
    assert response.status_code == 303

    sign_in(data_steward)
    assert client.get(f"/ui/jobs/{job_id}").status_code == 200


def test_no_route_removes_an_owner(web):
    client, _ = web
    routes = [(r.path, m) for r in client.app.routes
              for m in getattr(r, "methods", set())]
    assert not [(p, m) for p, m in routes
                if "owners" in p and m in ("DELETE", "PUT", "PATCH")]


# ==========================================================================
# Accessibility (C6)
# ==========================================================================
#
# WCAG 2.1 AA is a requirement we have cited as a *reason* for two design
# decisions. Checking it is therefore not optional: building an interface and
# not testing it would make C6 another claimed-but-unverified row, which is the
# failure Appendix B.5 is about.
#
# These checks are structural and find perhaps a third of WCAG failures. What
# they cannot establish is stated in `test_the_limits_of_these_checks_are_stated`
# below, and in the cluster 5 specification.

def _pages(client, service, researcher, sign_in):
    job_id = _job(service, researcher)
    service.add_gate_items(job_id, [_uninspected(), _redaction(), _care()])
    sign_in(researcher)
    return {
        "jobs": client.get("/ui/jobs").text,
        "job": client.get(f"/ui/jobs/{job_id}").text,
        "gate": client.get(f"/ui/jobs/{job_id}/gate").text,
        "deposit": client.get(f"/ui/jobs/{job_id}/deposit").text,
        # The metadata editor is where a person types what the
        # repository will publish, so it is held to the same bar.
        "metadata": client.get(f"/ui/jobs/{job_id}/metadata").text,
    }


def test_every_page_declares_a_language(web, service, researcher):
    """A screen reader needs it to choose a voice; without it, it guesses."""
    client, sign_in = web
    for name, body in _pages(client, service, researcher, sign_in).items():
        assert re.search(r'<html[^>]+lang="[a-z]{2}', body), name


def test_every_page_has_exactly_one_h1(web, service, researcher):
    """Heading structure is how a screen reader user navigates a page at all."""
    client, sign_in = web
    for name, body in _pages(client, service, researcher, sign_in).items():
        assert len(re.findall(r"<h1[ >]", body)) == 1, name


def test_every_page_offers_a_skip_link(web, service, researcher):
    """Without it, a keyboard user tabs through the navigation on every page."""
    client, sign_in = web
    for name, body in _pages(client, service, researcher, sign_in).items():
        assert 'href="#main"' in body, name
        assert 'id="main"' in body, name


def test_every_form_control_is_labelled(web, service, researcher):
    """An unlabelled control is announced as 'edit text, blank'."""
    client, sign_in = web
    pages = _pages(client, service, researcher, sign_in)
    for name, body in pages.items():
        for match in re.finditer(r"<(input|select|textarea)\b[^>]*>", body):
            tag = match.group(0)
            if re.search(r'type="(hidden|submit)"', tag):
                continue
            identifier = re.search(r'id="([^"]+)"', tag)
            assert identifier, f"{name}: control with no id: {tag}"
            assert (f'for="{identifier.group(1)}"' in body
                    or "aria-label" in tag), f"{name}: unlabelled: {tag}"


def test_meaning_never_depends_on_colour_alone(web, service, researcher):
    """A presumption and a finding must be distinguishable in text."""
    client, sign_in = web
    gate = _pages(client, service, researcher, sign_in)["gate"]
    assert "presum" in gate.lower(), (
        "the presumption is conveyed by styling alone, which is invisible to a "
        "screen reader and to anyone who does not perceive the colour")


def test_the_stylesheet_does_not_remove_focus_indication(web):
    """Removing the focus outline makes a page unusable by keyboard, and is the
    single most common accessibility regression in hand-written CSS."""
    from datadirector.web.views import STATIC
    css = (STATIC / "style.css").read_text()
    offending = re.findall(r"outline\s*:\s*(none|0)", css)
    assert not offending or ":focus" not in css.split("outline")[0][-200:], (
        "the stylesheet removes focus indication")
    assert ":focus" in css, "no visible focus style is defined"


def test_the_limits_of_these_checks_are_stated():
    """What automated checking cannot establish, said plainly.

    Automated rules find roughly a third of WCAG failures. Claiming AA on their
    strength would be exactly the overstatement this project keeps finding in
    itself, so the specification names what remains unverified and this test
    fails if that admission is removed.
    """
    from pathlib import Path
    spec = (Path(__file__).parent.parent / "docs" /
            "implementation-cluster-5.md").read_text()
    assert "screen reader pass" in spec
    assert "third of WCAG failures" in spec


# ==========================================================================
# Submitting from the browser
# ==========================================================================

import io  # noqa: E402
import json as _json  # noqa: E402
import zipfile  # noqa: E402


@pytest.fixture
def full_web(tmp_path, sessions):
    """The interface with a real pipeline behind it.

    Distinct from the `web` fixture, which has none: without a pipeline the
    interface can review work created elsewhere but not start any, which is
    what it could do before this.
    """
    from fastapi import FastAPI

    from datadirector.api.auth import SessionResolver
    from datadirector.api.service import JobService
    from datadirector.config.loader import load_policy, load_wiring, resolve
    from datadirector.pipeline import Pipeline
    from datadirector.plugins.discovery import PluginRegistry
    from datadirector.runtime import Runtime

    root = Path(__file__).parent.parent
    resolved = resolve(load_wiring(root / "config/wiring.example.yaml"),
                       load_policy(root / "config/policy.example.yaml"),
                       PluginRegistry(), application_version="0.1.0")
    runtime = Runtime(resolved, state_root=tmp_path / "state",
                      working_root=tmp_path / "work")
    service = JobService(runtime.store, recorder=runtime.recorder)
    # The auditor grant must be here too, or that ORCID is simply a stranger
    # and every auditor assertion tests the wrong refusal.
    resolver = SessionResolver(sessions,
                               {AUDITOR.value: [AccessRole.AUDITOR]})
    app = build_web(FastAPI(), service, pipeline=Pipeline(runtime),
                    resolve_principal=resolver, resolve_roles=resolver.roles_for)
    client = TestClient(app, follow_redirects=False)

    def sign_in(who: Orcid) -> TestClient:
        client.cookies.set(COOKIE_NAME, sessions.create(who).identifier)
        return client

    return client, sign_in, runtime


def _job_id_from(response) -> str:
    """The job identifier from a redirect.

    Extracted by pattern rather than by taking the last path segment: submission
    now redirects to the declaration screen, and the last segment became
    "declaration".
    """
    return re.search(r"(job-[0-9A-HJKMNP-TV-Z]{26})",
                     response.headers["location"]).group(1)


def _upload(members: dict) -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    buffer.seek(0)
    return buffer


MADMP = _json.dumps({"dmp": {"dataset": [{"distribution": [
    {"data_access": "open", "host": {"title": "Zenodo"}}]}]}})


def test_submitting_requires_signing_in(full_web):
    client, _, _ = full_web
    assert client.get("/ui/submit").status_code == 303


def test_a_researcher_can_start_a_job_from_the_browser(full_web, researcher):
    """The definition of done: reaching work without the command line.

    Before this the interface could review jobs created at a command line and
    start none, which made it a viewer rather than a way in.
    """
    client, sign_in, runtime = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("submission.zip", _upload(
            {"observations.csv": "station,temp\nS14,4.1\n"}), "application/zip")},
        data={"instruction": "", "dmp": ""})

    assert response.status_code == 303
    job_id = _job_id_from(response)
    assert client.get(f"/ui/jobs/{job_id}").status_code == 200
    kinds = [e.kind.value for e in runtime.store.load(job_id)]
    assert "material.registered" in kinds


def test_the_submitter_owns_what_they_submit(full_web, researcher,
                                             data_steward):
    client, sign_in, runtime = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)

    sign_in(data_steward)
    assert client.get(f"/ui/jobs/{job_id}").status_code == 404


def test_an_instruction_is_recorded_as_coming_from_the_depositor(full_web,
                                                                 researcher):
    """The directive channel. The same words inside a file carry no authority."""
    client, sign_in, runtime = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "deposit this in EUDAT", "dmp": ""})
    job_id = _job_id_from(response)

    received = [e for e in runtime.store.load(job_id)
                if e.kind.value == "instructions.received"]
    assert received
    assert received[0].human == researcher
    assert received[0].payload["channel"] == "authenticated-depositor"


def test_a_plan_inside_the_upload_is_found(full_web, researcher):
    client, sign_in, runtime = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n",
                                            "maDMP.json": MADMP}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)

    plan = [e for e in runtime.store.load(job_id)
            if e.kind.value == "dmp.commitments-read"]
    assert plan[-1].payload["route"] == "found-in-submission"


def test_the_submit_page_says_what_the_installation_cannot_do(full_web,
                                                              researcher):
    """Said before the researcher commits their time. Finding out at the end
    that a step could never have run is worse than knowing at the start."""
    client, sign_in, runtime = full_web
    sign_in(researcher)
    body = client.get("/ui/submit").text
    if runtime.unavailable():
        assert "cannot do" in body


def test_the_instruction_field_explains_the_trust_boundary(full_web,
                                                           researcher):
    """A researcher should know why their words count and a file's do not."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    body = client.get("/ui/submit").text.lower()
    assert "instruction from you" in body
    assert "inside your files" in body


def test_submission_uses_the_same_pipeline_as_the_command_line(full_web):
    """A second route into ingestion would be a second place for the rules to
    be applied differently."""
    import inspect

    from datadirector.web import views
    source = inspect.getsource(views)
    assert "pipeline.ingest(" in source
    assert "IngestionAgent(" not in source, (
        "the interface constructs its own ingestion path")


# ==========================================================================
# Progress and history
# ==========================================================================

def test_the_list_names_the_phase_not_the_agent(full_web, researcher):
    """'Working out what is sensitive' is something a researcher understands;
    'classification' is the name of an agent."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    client.post("/ui/submit",
                files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                                  "application/zip")},
                data={"instruction": "", "dmp": ""})
    body = client.get("/ui/jobs").text
    assert "Progress" in body
    assert any(word in body for word in
               ("Your statement about the data", "Working out what is sensitive",
                "Received"))


def test_every_phase_links_to_its_own_history(full_web, researcher):
    """'What happened during classification?' is the question a person has, and
    a flat log of forty entries does not answer it."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)

    body = client.get(f"/ui/jobs/{job_id}").text
    # Only phases that have something in them, or are where the job is now: a
    # link to an empty page promises content that is not there.
    assert "history?phase=received" in body
    assert "history?phase=declaration" in body

    filtered = client.get(f"/ui/jobs/{job_id}/history",
                          params={"phase": "received"})
    assert filtered.status_code == 200
    assert "Your files were taken in" in filtered.text, (
        "the history shows event names rather than what happened")


def test_history_is_owner_scoped(full_web, researcher, data_steward):
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)
    sign_in(data_steward)
    assert client.get(f"/ui/jobs/{job_id}/history").status_code == 404


def test_phase_state_is_given_in_words_not_only_colour(full_web, researcher):
    """Styling is invisible to a screen reader and to anyone who does not
    perceive the colour."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)
    body = client.get(f"/ui/jobs/{job_id}").text
    assert "waiting on you" in body or "pending" in body or "done" in body


def test_an_empty_phase_says_nothing_is_recorded_not_nothing_happened(
        full_web, researcher):
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)
    body = client.get(f"/ui/jobs/{job_id}/history",
                      params={"phase": "deposit"}).text
    assert "Nothing has happened in this part yet" in body


def test_a_fresh_job_shows_the_phase_it_is_actually_at(full_web, researcher):
    """Falling back to the last phase showed 'Publishing — pending' for a job
    that had just arrived, which is worse than saying nothing."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    client.post("/ui/submit",
                files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                                  "application/zip")},
                data={"instruction": "", "dmp": ""})
    body = client.get("/ui/jobs").text
    assert "Publishing" not in body, (
        "a newly submitted job is shown as being at the publishing phase")
    assert "Your statement about the data" in body
    assert "waiting on you" in body


# ==========================================================================
# Advancing from the browser
# ==========================================================================

def _submitted(client, sign_in, who) -> str:
    sign_in(who)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    return _job_id_from(response)


def test_an_owner_is_offered_a_way_to_continue(full_web, researcher):
    client, sign_in, _ = full_web
    job_id = _submitted(client, sign_in, researcher)
    body = client.get(f"/ui/jobs/{job_id}").text
    assert f"/ui/jobs/{job_id}/advance" in body
    assert "Continue" in body


def test_advancing_is_a_post_not_a_link(full_web, researcher):
    """A GET that changed state would run on every refresh and on every link a
    browser chose to prefetch."""
    client, sign_in, _ = full_web
    job_id = _submitted(client, sign_in, researcher)
    methods = {m for r in client.app.routes
               if getattr(r, "path", "") == "/ui/jobs/{job_id}/advance"
               for m in r.methods}
    assert methods == {"POST"}
    assert client.get(f"/ui/jobs/{job_id}/advance").status_code == 405


def test_advancing_returns_to_the_job(full_web, researcher):
    client, sign_in, _ = full_web
    job_id = _submitted(client, sign_in, researcher)
    response = client.post(f"/ui/jobs/{job_id}/advance")
    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/jobs/{job_id}"


def test_a_non_owner_cannot_advance(full_web, researcher, data_steward):
    client, sign_in, _ = full_web
    job_id = _submitted(client, sign_in, researcher)
    sign_in(data_steward)
    assert client.post(f"/ui/jobs/{job_id}/advance").status_code == 404


def test_an_auditor_is_not_offered_the_button(full_web, researcher):
    """A button an auditor cannot use is a button that teaches them the
    interface is broken."""
    client, sign_in, _ = full_web
    job_id = _submitted(client, sign_in, researcher)
    sign_in(AUDITOR)
    body = client.get(f"/ui/jobs/{job_id}").text
    assert f"/ui/jobs/{job_id}/advance" not in body


def test_an_auditor_cannot_advance_even_by_posting(full_web, researcher):
    client, sign_in, _ = full_web
    job_id = _submitted(client, sign_in, researcher)
    sign_in(AUDITOR)
    assert client.post(f"/ui/jobs/{job_id}/advance").status_code == 409


def test_a_halted_job_offers_to_try_again_and_says_why_that_may_help(
        full_web, researcher):
    """Retrying is worth doing when the cause was temporary, and a person
    cannot judge that without being told what stopped it."""
    from datadirector_contracts import Event, EventKind
    client, sign_in, runtime = full_web
    job_id = _submitted(client, sign_in, researcher)
    runtime.store.append(Event(
        sequence=1, job_id=job_id, kind=EventKind.WORKFLOW_HALTED,
        agent="test/1.0",
        payload={"reason": "classification: the model timed out"}))

    body = client.get(f"/ui/jobs/{job_id}").text
    assert "Try again" in body
    assert "timed out" in body
    assert "temporary" in body


def test_advancing_a_halted_job_does_not_show_an_error_screen(full_web,
                                                              researcher):
    """The engine records the halt with its reason, and the job page is where a
    person should read it."""
    from datadirector_contracts import Event, EventKind
    client, sign_in, runtime = full_web
    job_id = _submitted(client, sign_in, researcher)
    runtime.store.append(Event(
        sequence=1, job_id=job_id, kind=EventKind.WORKFLOW_HALTED,
        agent="test/1.0", payload={"reason": "the model could not be reached"}))
    response = client.post(f"/ui/jobs/{job_id}/advance")
    assert response.status_code == 303


# ==========================================================================
# The front door, and who the instance belongs to
# ==========================================================================

def test_the_landing_page_is_reachable_without_signing_in(full_web):
    """A visitor who has never been here needs to know what this is, not a bare
    redirect to an identity provider they did not ask for."""
    client, _, _ = full_web
    response = client.get("/")
    assert response.status_code == 200
    assert "Data Director" in response.text


def test_the_landing_page_offers_sign_in_when_it_is_configured(tmp_path,
                                                               sessions):
    from fastapi import FastAPI

    from datadirector.api.auth import SessionResolver
    from datadirector.api.service import JobService
    from datadirector.state.store import EventStore

    resolver = SessionResolver(sessions)
    app = build_web(FastAPI(), JobService(EventStore(tmp_path / "s")),
                    sign_in_available=True, resolve_principal=resolver,
                    resolve_roles=resolver.roles_for)
    body = TestClient(app).get("/").text
    assert "/auth/login" in body
    assert "your own ORCID" in body


def test_it_says_so_rather_than_offering_a_button_that_leads_nowhere(full_web):
    """`sign_in_available` is false in this fixture: no ORCID application."""
    client, _, _ = full_web
    body = client.get("/").text
    assert "/auth/login" not in body
    assert "not configured" in body


def test_the_landing_page_says_the_instance_serves_anyone_with_an_orcid(
        tmp_path, sessions):
    """The instance belongs to no one ORCID. Application credentials identify
    the installation; every visitor signs in as themselves."""
    from fastapi import FastAPI

    from datadirector.api.auth import SessionResolver
    from datadirector.api.service import JobService
    from datadirector.state.store import EventStore

    resolver = SessionResolver(sessions)
    app = build_web(FastAPI(), JobService(EventStore(tmp_path / "s")),
                    sign_in_available=True, resolve_principal=resolver,
                    resolve_roles=resolver.roles_for)
    body = TestClient(app).get("/").text
    assert "serves anyone with one" in body


def test_a_signed_in_visitor_is_shown_their_own_work(full_web, researcher):
    client, sign_in, _ = full_web
    sign_in(researcher)
    body = client.get("/").text
    assert "/ui/jobs" in body
    assert researcher.value in body


def test_two_people_hold_separate_sessions_on_one_instance(full_web,
                                                           researcher,
                                                           data_steward):
    """The point of the criticism that produced the landing page: one instance,
    many people, each seeing only their own work."""
    client, sign_in, _ = full_web

    sign_in(researcher)
    first = client.post(
        "/ui/submit",
        files={"upload": ("a.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    mine = _job_id_from(first)

    sign_in(data_steward)
    second = client.post(
        "/ui/submit",
        files={"upload": ("b.zip", _upload({"b.csv": "y\n2\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    theirs = _job_id_from(second)

    assert mine != theirs
    assert client.get(f"/ui/jobs/{mine}").status_code == 404
    listed = client.get("/ui/jobs").text
    assert theirs in listed and mine not in listed

    sign_in(researcher)
    assert client.get(f"/ui/jobs/{theirs}").status_code == 404


# ==========================================================================
# The declaration: the second screen
# ==========================================================================

def test_submitting_leads_to_the_declaration_not_the_job(full_web, researcher):
    """Nothing can read the data until the researcher has said what it is, so
    showing them a job waiting for something they have not been asked for would
    be a dead end."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    assert response.headers["location"].endswith("/declaration")


def test_the_declaration_screen_shows_what_arrived(full_web, researcher):
    """A statement written with the material in view is about the material; one
    written blind is about the researcher's memory of it."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n",
                                            "b.csv": "y\n2\n",
                                            "plate.png": "\x89PNG"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)

    body = client.get(f"/ui/jobs/{job_id}/declaration").text
    assert "What we received" in body
    assert "csv" in body and "png" in body


def test_it_asks_for_prose_not_a_form(full_web, researcher):
    """The things that matter are rarely the ones a form anticipates."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)
    body = client.get(f"/ui/jobs/{job_id}/declaration").text
    assert "<textarea" in body
    assert "your own words" in body.lower()


def test_the_statement_is_recorded_before_it_is_read(full_web, researcher):
    """If the parse is wrong, the evidence of what was actually said is still
    there."""
    client, sign_in, runtime = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)

    statement = "Interviews with traditional owners; consent excludes quotation."
    client.post(f"/ui/jobs/{job_id}/declaration", data={"statement": statement})

    recorded = [e for e in runtime.store.load(job_id)
                if e.payload.get("statement")]
    assert recorded
    assert recorded[0].payload["statement"] == statement
    assert recorded[0].human == researcher


def test_a_failed_parse_keeps_the_statement(full_web, researcher):
    """A model that cannot be reached must not lose what the researcher wrote."""
    client, sign_in, runtime = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)

    # No model is reachable in the test environment, so the parse fails.
    result = client.post(f"/ui/jobs/{job_id}/declaration",
                         data={"statement": "Some careful prose."})
    assert result.status_code in (303, 502)
    recorded = [e for e in runtime.store.load(job_id)
                if e.payload.get("statement")]
    assert recorded, "the statement was lost when the parse failed"
    if result.status_code == 502:
        assert "recorded as you wrote it" in result.text


def test_only_an_owner_may_declare(full_web, researcher, data_steward):
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)

    sign_in(data_steward)
    assert client.get(f"/ui/jobs/{job_id}/declaration").status_code == 404
    assert client.post(f"/ui/jobs/{job_id}/declaration",
                       data={"statement": "mine now"}).status_code == 404


def test_confirming_is_per_claim_with_nothing_preselected(full_web, researcher):
    """A single button confirming everything would make this a formality, which
    is the one thing it must not be."""
    client, sign_in, runtime = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)

    body = client.get(f"/ui/jobs/{job_id}/declaration/confirm").text
    assert "checked" not in body.lower()
    assert "selected>" not in body.lower()


def test_leaving_the_level_alone_applies_the_most_restrictive(full_web,
                                                              researcher):
    """An absent statement is not a statement that the data is open."""
    client, sign_in, runtime = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)

    client.post(f"/ui/jobs/{job_id}/declaration/confirm",
                data={"sensitivity": ""})
    state = runtime.store.load(job_id)
    confirmed = [e for e in state if e.kind.value == "declaration.confirmed"]
    assert confirmed
    assert confirmed[0].payload["sensitivity"] == 2
    assert confirmed[0].payload["assumed_most_restrictive"] is True


def test_confirmation_names_the_human(full_web, researcher):
    client, sign_in, runtime = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)
    client.post(f"/ui/jobs/{job_id}/declaration/confirm", data={})

    confirmed = [e for e in runtime.store.load(job_id)
                 if e.kind.value == "declaration.confirmed"]
    assert confirmed[0].human == researcher


# ==========================================================================
# Choosing a backend, within what policy permits
# ==========================================================================

def test_the_submit_page_lists_the_models_this_installation_has(full_web,
                                                                researcher):
    client, sign_in, runtime = full_web
    sign_in(researcher)
    body = client.get("/ui/submit").text
    for name in runtime.backends:
        assert name in body


def test_it_says_the_choice_can_only_narrow(full_web, researcher):
    """A page offering a choice must not imply it can reach something policy
    forbids."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    body = client.get("/ui/submit").text.lower()
    assert "stricter, never looser" in body
    assert "cannot reach a model policy forbids" in body


def test_a_preference_is_recorded_against_the_job(full_web, researcher):
    """'The model said sensitive' and 'the model they picked said sensitive'
    are different claims."""
    client, sign_in, runtime = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": "", "residency": "on-premise",
              "backend": ""})
    job_id = _job_id_from(response)

    recorded = [e for e in runtime.store.load(job_id)
                if e.payload.get("channel") == "backend-preference"]
    assert recorded
    assert recorded[0].payload["residency_at_most"] == "on-premise"
    assert recorded[0].human == researcher


def test_no_preference_records_nothing(full_web, researcher):
    client, sign_in, runtime = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": "", "residency": "", "backend": ""})
    job_id = _job_id_from(response)
    assert not [e for e in runtime.store.load(job_id)
                if e.payload.get("channel") == "backend-preference"]


def test_an_unrecognised_residency_does_not_refuse_the_submission(full_web,
                                                                  researcher):
    """Refusing a submission over a form value would be a poor trade: policy
    still governs regardless of what arrives here."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": "", "residency": "the-moon",
              "backend": ""})
    assert response.status_code == 303


# ==========================================================================
# The root, for whoever is at it
# ==========================================================================

def test_a_browser_at_the_root_gets_a_page(full_web):
    """The API held `/` and the interface registered it second, so a researcher
    opening the service address was handed a JSON object."""
    client, _, _ = full_web
    response = client.get("/", headers={"Accept": "text/html"})
    assert response.headers["content-type"].startswith("text/html")
    assert "Data Director" in response.text


def test_a_client_asking_for_json_gets_the_description(full_web):
    """A Data Director discovering another instance starts here too."""
    client, _, _ = full_web
    response = client.get("/", headers={"Accept": "application/json"})
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["service"] == "data-director"
    assert "Blueprint" in response.json()["implements"]


def test_the_description_has_an_unambiguous_address_as_well(tmp_path, sessions):
    """Negotiation is a convenience; a client that wants the description
    without arguing about Accept headers has somewhere plain to ask.

    Built with both surfaces, because `/api` belongs to the API and the
    interface fixture mounts only the interface.
    """
    from fastapi import FastAPI

    from datadirector.api.app import build_app
    from datadirector.api.auth import SessionResolver
    from datadirector.api.service import JobService
    from datadirector.state.store import EventStore

    resolver = SessionResolver(sessions)
    service = JobService(EventStore(tmp_path / "state"))
    app = build_app(service, resolve_principal=resolver)
    build_web(app, service, resolve_principal=resolver)

    response = TestClient(app).get("/api")
    assert response.status_code == 200
    assert response.json()["api_version"]


def test_the_root_is_not_a_redirect(full_web):
    """A root that sends people elsewhere makes the address they share the
    wrong one."""
    client, _, _ = full_web
    assert client.get("/", headers={"Accept": "text/html"}).status_code == 200


# ==========================================================================
# What the pages say
# ==========================================================================

def test_the_history_shows_no_internal_vocabulary(full_web, researcher):
    """The page rendered `declaration.parsed by declaration/0.1.0`, then
    `authority: proposed` and a Python dict of the claims. That is a log file in
    a browser: legible to whoever wrote the event and to nobody else."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "keep this local", "dmp": ""})
    job_id = _job_id_from(response)

    body = client.get(f"/ui/jobs/{job_id}/history").text
    for leak in ("material.registered", "workflow.created", "source_digest",
                 "claim_count", "authority", "{'", "0.1.0"):
        assert leak not in body, f"the history page shows {leak!r}"


def test_the_history_says_what_happened(full_web, researcher):
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)
    body = client.get(f"/ui/jobs/{job_id}/history").text
    assert "Your files were taken in" in body
    assert "Job started" in body


def test_the_job_page_leads_with_what_is_happening(full_web, researcher):
    """Ownership and identifiers were at the top and pushed the actual state of
    the job below the fold."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)

    body = client.get(f"/ui/jobs/{job_id}").text
    heading = body.index("<h1")
    owners = body.index("Who can see this job")
    progress = body.index("Progress")
    assert heading < progress < owners, (
        "ownership appears before the job's own state")


def test_adding_owners_is_not_the_first_thing_asked(full_web, researcher):
    """Asking everywhere is aggressive: it is real and rarely urgent."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)
    body = client.get(f"/ui/jobs/{job_id}").text
    assert "<details" in body and "Who can see this job" in body, (
        "the ownership form is not collapsed")


def test_a_stopped_job_says_so_at_the_top(full_web, researcher):
    from datadirector_contracts import Event, EventKind

    client, sign_in, runtime = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)
    runtime.store.append(Event(
        sequence=1, job_id=job_id, kind=EventKind.WORKFLOW_HALTED,
        agent="t/1.0", payload={"reason": "the model timed out"}))

    body = client.get(f"/ui/jobs/{job_id}").text
    assert body.index("This job stopped") < body.index("Progress")
    assert "timed out" in body


def test_a_link_is_not_dressed_as_a_button(full_web):
    """A link styled identically to a button leaves a reader unable to tell
    which of them will change something."""
    from pathlib import Path

    css = (Path(__file__).parent.parent / "packages/datadirector/src"
           / "datadirector/web/static/style.css").read_text()
    assert "a.button" in css
    assert "background: transparent" in css, (
        "the link-as-button is filled, so it looks like an action")


def test_the_irreversible_action_looks_different(full_web, researcher):
    """Publishing cannot be undone, and a button that looks like every other
    button says otherwise."""
    from pathlib import Path

    css = (Path(__file__).parent.parent / "packages/datadirector/src"
           / "datadirector/web/static/style.css").read_text()
    assert "button.irreversible" in css

    deposit = (Path(__file__).parent.parent / "packages/datadirector/src"
               / "datadirector/web/templates/deposit.html").read_text()
    assert "irreversible" in deposit


def test_phase_links_say_what_they_open(full_web, researcher):
    """"what happened" six times tells a reader nothing about which of the six
    they are choosing."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)
    body = client.get(f"/ui/jobs/{job_id}").text
    assert body.count("what\n        happened") == 0
    assert "The full record of this job" in body


def test_a_phase_with_nothing_in_it_offers_no_link(full_web, researcher):
    """A link to an empty page is a promise of content that is not there."""
    client, sign_in, _ = full_web
    sign_in(researcher)
    response = client.post(
        "/ui/submit",
        files={"upload": ("d.zip", _upload({"a.csv": "x\n1\n"}),
                          "application/zip")},
        data={"instruction": "", "dmp": ""})
    job_id = _job_id_from(response)
    body = client.get(f"/ui/jobs/{job_id}").text
    assert "history?phase=deposit" not in body, (
        "the publishing phase offers a details link before anything happened")



# -- reviewing and correcting the record before it is published ----------
#
# Publishing mints an identifier for the metadata as much as the data, so the
# deposit screen must show the record, not only the file names, and must show
# the reason it would be refused *before* a researcher presses publish and is
# told no. Among the required fields, `creators` is the one no agent may
# invent -- so the screen must let the person supply it and re-check.


def _draft(service, job_id, creators=(), title="A study of tide pools"):
    import json as json_
    from datadirector_contracts import CanonicalRecord, Creator, Event
    made = [Creator(name=n) for n in creators]
    record = CanonicalRecord(title=title, creators=made, publication_year=2026,
        resource_type="Dataset")
    service.store.append(Event(
        sequence=1, job_id=job_id, kind=EventKind.METADATA_DRAFTED,
        agent="metadata/0.1.0",
        payload={"record": json_.loads(record.model_dump_json())}))
    return record


def _blocked_on(service, job_id, field, message):
    import json as json_
    from datadirector_contracts import Event, ValidationFinding
    finding = ValidationFinding(severity="error", field=field, message=message)
    service.store.append(Event(
        sequence=1, job_id=job_id, kind=EventKind.VALIDATION_COMPLETED,
        agent="validation/0.1.0",
        payload={"findings": [json_.loads(finding.model_dump_json())],
            "blocking": True}))


def test_the_deposit_review_shows_the_metadata_not_only_the_files(
        web, service, researcher):
    client, sign_in = web
    job_id = _job(service, researcher)
    _draft(service, job_id, creators=["Aroa, Miriam"])
    sign_in(researcher)
    body = client.get(f"/ui/jobs/{job_id}/deposit").text
    assert "A study of tide pools" in body
    assert "Aroa, Miriam" in body, (
        "the screen publishes the record but never shows its creators")


def test_a_blocking_finding_is_shown_and_publish_is_withheld(
        web, service, researcher):
    client, sign_in = web
    job_id = _job(service, researcher)
    _draft(service, job_id, creators=[])
    _blocked_on(service, job_id, "creators", "required by DataCite")
    sign_in(researcher)
    body = client.get(f"/ui/jobs/{job_id}/deposit").text
    assert "creators" in body.lower()
    assert "required by datacite" in body.lower(), (
        "the reason a deposit would be refused is hidden from the researcher")
    assert 'name="confirm_irreversible"' not in body, (
        "publish is offered while a blocking finding still stands")


def test_the_review_links_to_the_editor_when_a_record_needs_correcting(
        web, service, researcher):
    client, sign_in = web
    job_id = _job(service, researcher)
    _draft(service, job_id, creators=[])
    _blocked_on(service, job_id, "creators", "required by DataCite")
    sign_in(researcher)
    body = client.get(f"/ui/jobs/{job_id}/deposit").text
    assert f"/ui/jobs/{job_id}/metadata" in body


def test_the_editor_is_prefilled_from_the_signed_in_orcid(
        web, service, researcher):
    client, sign_in = web
    job_id = _job(service, researcher)
    _draft(service, job_id, creators=[])
    sign_in(researcher)
    body = client.get(f"/ui/jobs/{job_id}/metadata").text
    assert researcher.value in body, (
        "the person publishing is not offered as a creator to credit")
    assert 'name="creator_0_name"' in body


def test_supplying_a_creator_clears_the_block_and_offers_publish(full_web,
        researcher):
    client, sign_in, runtime = full_web
    from datadirector.api.service import JobService
    from datadirector.pipeline import Pipeline
    service = JobService(runtime.store)
    job_id = service.create(human=researcher)
    _draft(service, job_id, creators=[])
    Pipeline(runtime).revalidate_metadata(job_id)
    sign_in(researcher)
    assert 'name="confirm_irreversible"' not in client.get(f"/ui/jobs/{job_id}/deposit").text
    response = client.post(f"/ui/jobs/{job_id}/metadata", data={
        "title": "A study of tide pools", "creator_count": "1",
        "creator_0_name": "Carberry, Josiah",
        "creator_0_orcid": researcher.value})
    assert response.status_code == 303
    body = client.get(f"/ui/jobs/{job_id}/deposit").text
    assert "Carberry, Josiah" in body
    assert 'name="confirm_irreversible"' in body, (
        "publish is still withheld after the blocking field was supplied")


def test_an_edit_is_a_new_draft_the_researcher_is_credited_for(full_web,
        researcher):
    client, sign_in, runtime = full_web
    from datadirector.api.service import JobService
    from datadirector_contracts import CanonicalRecord
    service = JobService(runtime.store)
    job_id = service.create(human=researcher)
    _draft(service, job_id, creators=[])
    sign_in(researcher)
    client.post(f"/ui/jobs/{job_id}/metadata", data={
        "title": "A study of tide pools", "creator_count": "1",
        "creator_0_name": "Carberry, Josiah"})
    drafts = [event for event in service.events(job_id)
        if event.kind is EventKind.METADATA_DRAFTED]
    assert len(drafts) == 2, "the original draft must stay beside the revision"
    revised = CanonicalRecord.model_validate(drafts[-1].payload["record"])
    assert revised.creators
    assert revised.creators[0].name == "Carberry, Josiah"
    assert drafts[-1].human is not None
    assert drafts[-1].human.value == researcher.value
    provenance = revised.origin_of("creators")
    assert provenance is not None
    assert provenance.origin.value == "researcher-supplied"


def test_an_undescribed_record_explains_its_own_blanks(web, service,
                                                       researcher):
    """An empty text box tells the researcher nothing: not whether the
    depositor never said it, not whether the README had the words and nothing
    carried them across, and not which box is therefore theirs to fill."""
    client, sign_in = web
    job_id = _job(service, researcher)
    _draft(service, job_id, creators=["Aroa, Miriam"])
    sign_in(researcher)
    body = client.get(f"/ui/jobs/{job_id}/metadata").text
    assert 'class="hint why-abstract"' in body
    assert "nothing has been stated" in body.lower(), (
         "the blank Description is shown without its reason")
    assert 'class="hint why-licence"' in body
    assert 'name="action" value="draft"' in body, (
         "nothing on the screen offers to draft from what the job has since "
         "recorded")


def test_draft_again_is_not_recorded_as_a_researcher_edit(web, service,
                                                          researcher):
    """A researcher pressing Draft again is asking the agents to work, not
    claiming a retyped form as their own revision (C14)."""
    client, sign_in = web
    job_id = _job(service, researcher)
    _draft(service, job_id, creators=["Aroa, Miriam"])
    sign_in(researcher)
    response = client.post(f"/ui/jobs/{job_id}/metadata",
                            data={"action": "draft", "title": "Retyped"})
    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/jobs/{job_id}/metadata"
    drafts = [event for event in service.events(job_id)
              if event.kind is EventKind.METADATA_DRAFTED]
    assert len(drafts) == 1, (
         "Draft again saved the submitted form as a revision")


def test_drafting_again_leaves_the_record_it_shown_readable(
        full_web, researcher):
    """Re-drafting is appended, never written over. Whatever the deployment
    has ready for the models, the draft the researcher was reviewing stays on
    the log unchanged and readable: a fresh machine draft that buries the
    record a person was shown is not a re-draft but a loss."""
    client, sign_in, runtime = full_web
    service = JobService(runtime.store)
    job_id = service.create(human=researcher)
    _draft(service, job_id, creators=["Aroa, Miriam"])
    sign_in(researcher)
    before = [event.payload for event in service.events(job_id)
              if event.kind is EventKind.METADATA_DRAFTED]
    response = client.post(f"/ui/jobs/{job_id}/metadata",
                            data={"action": "draft"})
    assert response.status_code == 303
    after = [event.payload for event in service.events(job_id)
             if event.kind is EventKind.METADATA_DRAFTED]
    assert after[0] == before[0], (
          "re-drafting rewrote the draft that was already on the log")
    from datadirector_contracts import CanonicalRecord
    titles = [CanonicalRecord.model_validate(payload["record"]).title
               for payload in after]
    assert "A study of tide pools" in titles, (
          "the record the researcher was shown is no longer readable")
    assert client.get(f"/ui/jobs/{job_id}/metadata").status_code == 200



def test_drafting_again_yields_to_the_researcher_s_own_revision(full_web,
                                                                researcher):
    """The review screen reads the newest draft. So long as a machine may
    become the newest, the researcher's own wording is one button press away
    from disappearing, which is why the documentation still runs and the
    metadata agent does not."""
    client, sign_in, runtime = full_web
    service = JobService(runtime.store)
    job_id = service.create(human=researcher)
    _draft(service, job_id, creators=["Aroa, Miriam"])
    sign_in(researcher)
    client.post(f"/ui/jobs/{job_id}/metadata", data={
         "title": "A study of tide pools", "creator_count": "1",
         "creator_0_name": "Carberry, Josiah"})
    before = [event for event in service.events(job_id)
              if event.kind is EventKind.METADATA_DRAFTED]
    assert before[-1].human is not None
    client.post(f"/ui/jobs/{job_id}/metadata", data={"action": "draft"})
    after = [event for event in service.events(job_id)
             if event.kind is EventKind.METADATA_DRAFTED]
    assert after[-1].payload == before[-1].payload, (
          "a machine draft became newer than the revision the researcher wrote")
    assert any(event.kind is EventKind.DOCUMENTATION_DRAFTED
                for event in service.events(job_id)), (
          "the README is still worth drafting whatever the record says")

