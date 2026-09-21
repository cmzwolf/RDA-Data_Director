"""The core API: requirement C5.

The Blueprint mandates a harmonised API for inter-instance interaction and does
not specify one, so what is tested here is our candidate. The tests assert the
properties that make it a candidate worth discussing rather than the shape of
any particular route.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="pip install -e 'packages/datadirector[api]'")

from fastapi.testclient import TestClient  # noqa: E402

from datadirector_contracts import (  # noqa: E402
    GateItem, GateItemKind, ItemDecision, Orcid, SensitivityClass,
)

from datadirector.api.app import build_app  # noqa: E402
from datadirector.api.models import Capabilities, RequirementCoverage  # noqa: E402
from datadirector.api.service import JobService  # noqa: E402
from datadirector.provenance.recorder import Recorder  # noqa: E402
from datadirector.state.store import EventStore  # noqa: E402

TOKEN = "Bearer session-for-carberry"
OTHER = "Bearer session-for-steward"


@pytest.fixture
def service(tmp_path):
    store = EventStore(tmp_path / "state")
    return JobService(store, recorder=Recorder(store, tmp_path / "prov"),
                      config_digest="sha256:test")


@pytest.fixture
def client(service, researcher, data_steward):
    sessions = {TOKEN: researcher, OTHER: data_steward}

    def resolve(authorization: str) -> Orcid:
        return sessions[authorization]

    return TestClient(build_app(
        service, resolve_principal=resolve,
        capabilities=Capabilities(
            coverage=[RequirementCoverage(requirement="R7",
                                          declared_status="Implemented",
                                          components=["ZenodoDriver"])],
            schemas_emitted=["DataCite 4.6", "RO-Crate 1.1"],
            repositories=["zenodo"],
            model_residencies=["on-premise"],
            sensitivity_classes=["public", "internal", "sensitive"],
            accepts_deposits=True)))


def _job(client) -> str:
    return client.post("/jobs", headers={"Authorization": TOKEN}).json()["job_id"]


# -- discovery: what makes inter-instance interaction possible -------------

def test_capabilities_are_readable_without_a_credential(client):
    """An instance that required a credential to say what it accepts could not
    be discovered by one that had not yet been given a credential."""
    response = client.get("/capabilities")
    assert response.status_code == 200
    assert response.json()["accepts_deposits"] is True


def test_capabilities_state_where_material_would_be_processed(client):
    """Residency is a precondition, not a detail: an instance processing
    extra-jurisdiction must not be sent another institution's sensitive data."""
    body = client.get("/capabilities").json()
    assert body["model_residencies"] == ["on-premise"]
    assert body["sensitivity_classes"]


def test_the_service_says_what_it_implements(client):
    """At `/api`. The root is shared with the interface, which negotiates on
    Accept; this is the address for a client that wants the description without
    arguing about headers."""
    body = client.get("/api").json()
    assert "Blueprint" in body["implements"]
    assert body["deployment_profile"]


def test_everything_else_requires_an_identified_caller(client):
    """Reads expose decision records and gate items, which are about identified
    people and their judgements."""
    for path in ("/jobs", "/jobs/whatever", "/jobs/whatever/events"):
        assert client.get(path).status_code == 401
    assert client.post("/jobs").status_code == 401


def test_an_unrecognised_credential_is_refused(client):
    assert client.get("/jobs",
                      headers={"Authorization": "Bearer nope"}).status_code == 401


# -- reads are projections ------------------------------------------------

def test_job_state_is_folded_from_the_log_not_stored(client, service):
    """The same answer after a restart, on another machine, or a week later."""
    job_id = _job(client)
    first = client.get(f"/jobs/{job_id}", headers={"Authorization": TOKEN}).json()

    rebuilt = JobService(service.store, config_digest="sha256:test")
    assert rebuilt.state(job_id).step == first["step"]
    assert first["event_count"] == len(rebuilt.events(job_id))


def test_reading_never_changes_anything(client):
    job_id = _job(client)
    before = client.get(f"/jobs/{job_id}/events",
                        headers={"Authorization": TOKEN}).json()
    for _ in range(3):
        client.get(f"/jobs/{job_id}", headers={"Authorization": TOKEN})
        client.get(f"/jobs/{job_id}/gate", headers={"Authorization": TOKEN})
    after = client.get(f"/jobs/{job_id}/events",
                       headers={"Authorization": TOKEN}).json()
    assert before == after


def test_every_failure_has_the_same_shape(client, service):
    """A client should not need to know which layer refused it to read why."""
    job_id = _job(client)
    responses = [
        client.get("/jobs"),                                        # 401, ours
        client.get("/jobs/nope", headers={"Authorization": TOKEN}),  # 404, ours
        client.post(f"/jobs/{job_id}/deposit",
                    json={"repository": "z", "confirm_irreversible": False},
                    headers={"Authorization": TOKEN}),               # 428
    ]
    for response in responses:
        body = response.json()
        assert set(body) >= {"status", "code", "detail"}, body
        assert body["status"] == response.status_code


def test_an_unknown_job_is_a_named_problem(client):
    body = client.get("/jobs/job-does-not-exist",
                      headers={"Authorization": TOKEN}).json()
    assert body["code"] == "no-such-job"
    assert body["status"] == 404


# -- human acts -----------------------------------------------------------

def test_a_confirmation_names_the_human_in_the_log(client, service, researcher):
    job_id = _job(client)
    client.post(f"/jobs/{job_id}/declaration/confirm", json={"sensitivity": 1},
                headers={"Authorization": TOKEN})
    confirmations = [e for e in service.events(job_id)
                     if e.kind.value == "declaration.confirmed"]
    assert confirmations and confirmations[0].human == researcher


def test_omitting_the_level_assumes_the_most_restrictive(client):
    """An absent statement is not a statement of openness (ADR-023)."""
    job_id = _job(client)
    body = client.post(f"/jobs/{job_id}/declaration/confirm", json={},
                       headers={"Authorization": TOKEN}).json()
    assert body["sensitivity"] == "sensitive"
    assert body["assumed_most_restrictive"] is True


def test_the_gate_survives_a_restart(client, service, researcher):
    """An approval surface that forgot its items on restart would make the
    human gate the least durable part of a system built around durability."""
    job_id = _job(client)
    service.add_gate_items(job_id, [GateItem(
        item_id="uninspected:plate.png", kind=GateItemKind.UNINSPECTED_FILE,
        artefact="plate.png", summary="not inspected",
        permitted_decisions=[ItemDecision.PUBLISH_AS_IS,
                             ItemDecision.EXCLUDE_FROM_DEPOSIT])])

    rebuilt = JobService(service.store)
    assert len(rebuilt.gate(job_id).unresolved()) == 1

    body = client.get(f"/jobs/{job_id}/gate",
                      headers={"Authorization": TOKEN}).json()
    assert body["blocks_deposit"] is True


def test_items_are_resolved_one_at_a_time(client, service, researcher):
    """There is no endpoint taking a list: a client that could resolve forty
    items in one call has reviewed nothing."""
    job_id = _job(client)
    service.add_gate_items(job_id, [
        GateItem(item_id=f"uninspected:{n}.png",
                 kind=GateItemKind.UNINSPECTED_FILE, artefact=f"{n}.png",
                 summary="not inspected",
                 permitted_decisions=[ItemDecision.EXCLUDE_FROM_DEPOSIT])
        for n in ("a", "b")])

    routes = [r.path for r in client.app.routes]
    assert not any(r.endswith("/gate/resolve") for r in routes)

    client.post(f"/jobs/{job_id}/gate/uninspected:a.png/resolve",
                json={"decision": "exclude-from-deposit"},
                headers={"Authorization": TOKEN})
    gate = client.get(f"/jobs/{job_id}/gate",
                      headers={"Authorization": TOKEN}).json()
    assert gate["unresolved"] == 1


def test_a_decision_the_item_does_not_offer_is_refused(client, service,
                                                       researcher):
    job_id = _job(client)
    service.add_gate_items(job_id, [GateItem(
        item_id="care:referral", kind=GateItemKind.CARE_REFERRAL,
        summary="CARE may apply",
        permitted_decisions=[ItemDecision.CONSULTED,
                             ItemDecision.NOT_APPLICABLE])])
    response = client.post(f"/jobs/{job_id}/gate/care:referral/resolve",
                           json={"decision": "approve"},
                           headers={"Authorization": TOKEN})
    assert response.status_code == 409
    assert response.json()["code"] == "refused"


def test_an_invented_decision_is_refused(client, service):
    job_id = _job(client)
    service.add_gate_items(job_id, [GateItem(
        item_id="x", kind=GateItemKind.UNINSPECTED_FILE, summary="s",
        permitted_decisions=[ItemDecision.APPROVE])])
    response = client.post(f"/jobs/{job_id}/gate/x/resolve",
                           json={"decision": "definitely-fine"},
                           headers={"Authorization": TOKEN})
    assert response.status_code == 422
    assert response.json()["code"] == "unknown-decision"


# -- the irreversible operation -------------------------------------------

def test_depositing_requires_acknowledging_that_it_cannot_be_undone(client):
    """A client that reached this endpoint by accident should not succeed."""
    job_id = _job(client)
    response = client.post(f"/jobs/{job_id}/deposit",
                           json={"repository": "zenodo",
                                 "confirm_irreversible": False},
                           headers={"Authorization": TOKEN})
    assert response.status_code == 428
    assert response.json()["requirement"] == "C13"


def test_an_open_gate_refuses_the_deposit(client, service):
    job_id = _job(client)
    service.add_gate_items(job_id, [GateItem(
        item_id="care:referral", kind=GateItemKind.CARE_REFERRAL,
        summary="CARE may apply", permitted_decisions=[ItemDecision.CONSULTED])])
    response = client.post(f"/jobs/{job_id}/deposit",
                           json={"repository": "zenodo",
                                 "confirm_irreversible": True},
                           headers={"Authorization": TOKEN})
    assert response.status_code == 409
    assert "CARE" in response.json()["detail"]


def test_repeating_a_deposit_returns_the_existing_identifier(client, service,
                                                             researcher):
    """A client that lost the response must be able to ask again without
    creating a second record."""
    from datadirector_contracts import Event, EventKind
    job_id = _job(client)
    service.store.append(Event(
        sequence=1, job_id=job_id, kind=EventKind.DEPOSIT_COMPLETED,
        agent="test/1.0", human=researcher,
        payload={"pid": "10.5072/zenodo.1"}))

    response = client.post(f"/jobs/{job_id}/deposit",
                           json={"repository": "zenodo",
                                 "confirm_irreversible": True},
                           headers={"Authorization": TOKEN})
    assert response.status_code == 200
    body = response.json()
    assert body["pid"] == "10.5072/zenodo.1"
    assert body["already_published"] is True


# -- provenance -----------------------------------------------------------

def test_provenance_defaults_to_the_open_partition(client, service, researcher):
    """A default returning restricted content would make the partition
    decorative (§7.5)."""
    from datadirector_contracts import Event, EventKind, ProvActivity, Visibility
    from datadirector_contracts.provenance import ProvAgent
    job_id = _job(client)
    agent = ProvAgent(software="test/1.0", human=researcher)
    for visibility in (Visibility.OPEN, Visibility.CONFIDENTIAL):
        service.recorder.record(job_id, ProvActivity(
            activity_id=f"a-{visibility.value}", activity_type="dd:Act",
            agent=agent, visibility=visibility),
            Event(sequence=1, job_id=job_id,
                  kind=EventKind.VALIDATION_COMPLETED, agent="test/1.0"))

    default = client.get(f"/jobs/{job_id}/provenance",
                         headers={"Authorization": TOKEN}).json()
    assert len(default["@graph"]) == 1

    asked = client.get(f"/jobs/{job_id}/provenance",
                       params={"visibility": "confidential"},
                       headers={"Authorization": TOKEN}).json()
    assert len(asked["@graph"]) == 2


# -- the descriptor is generated, not written -----------------------------

def test_the_openapi_descriptor_comes_from_the_runtime_models(client):
    """ADR-003: a hand-maintained descriptor drifts from the code within weeks,
    and publishing a specification the software does not implement would be the
    same failure as a conformance row claiming a component that does not exist.
    """
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "Data Director core API"
    assert "/jobs/{job_id}/deposit" in spec["paths"]

    schemas = spec["components"]["schemas"]
    assert "DepositRequest" in schemas
    assert "confirm_irreversible" in schemas["DepositRequest"]["properties"]
    assert "ProblemDetail" in schemas


def test_capabilities_are_derived_from_the_conformance_report(tmp_path):
    """A hand-written statement of what an instance can do is one nobody checks,
    and this one is read by other instances deciding what to send."""
    from datadirector.api.service import capabilities_from
    from datadirector.conformance.report import generate

    report = generate("docs/architecture.md")
    capabilities = capabilities_from(report, repositories=["zenodo"],
                                     residencies=["on-premise"],
                                     accepts_deposits=True)

    by_requirement = {c.requirement: c for c in capabilities.coverage}
    assert by_requirement["R7"].declared_status == report.claims["R7"]
    assert by_requirement["R7"].components == report.coverage["R7"]
    assert by_requirement["R12"].components == [], "absent means absent"


def test_the_descriptor_documents_how_requests_fail(client):
    """A descriptor showing only success teaches a client to handle only success."""
    spec = client.get("/openapi.json").json()
    deposit = spec["paths"]["/jobs/{job_id}/deposit"]["post"]["responses"]
    assert "409" in deposit and "428" in deposit
    assert deposit["428"]["content"]["application/json"]["schema"]["$ref"] \
        .endswith("ProblemDetail")


# ==========================================================================
# Ownership through the API
# ==========================================================================

AUDIT = "Bearer session-for-auditor"


@pytest.fixture
def owned_client(service, researcher, data_steward):
    """A client with three identities: two researchers and an auditor."""
    from datadirector_contracts import AccessRole
    auditor = Orcid(value="0000-0003-1111-222X")
    sessions = {TOKEN: researcher, OTHER: data_steward, AUDIT: auditor}
    roles = {AUDIT: {AccessRole.AUDITOR}}
    return TestClient(build_app(
        service, resolve_principal=lambda a: sessions[a],
        resolve_roles=lambda a: roles.get(a, set()))), auditor


def test_a_job_you_do_not_own_is_absent_not_forbidden(owned_client):
    """A 403 confirms the job exists, and existence is itself a disclosure."""
    client, _ = owned_client
    job_id = client.post("/jobs",
                         headers={"Authorization": TOKEN}).json()["job_id"]
    response = client.get(f"/jobs/{job_id}", headers={"Authorization": OTHER})
    assert response.status_code == 404
    assert response.json()["code"] == "no-such-job"


def test_listing_shows_only_your_own(owned_client):
    client, _ = owned_client
    mine = client.post("/jobs", headers={"Authorization": TOKEN}).json()["job_id"]
    client.post("/jobs", headers={"Authorization": OTHER})
    listed = client.get("/jobs", headers={"Authorization": TOKEN}).json()
    assert [j["job_id"] for j in listed] == [mine]


def test_an_added_owner_can_see_and_act(owned_client, data_steward):
    client, _ = owned_client
    job_id = client.post("/jobs",
                         headers={"Authorization": TOKEN}).json()["job_id"]
    client.post(f"/jobs/{job_id}/owners",
                json={"orcid": data_steward.value,
                      "reason": "covering my leave"},
                headers={"Authorization": TOKEN})

    assert client.get(f"/jobs/{job_id}",
                      headers={"Authorization": OTHER}).status_code == 200
    assert client.post(f"/jobs/{job_id}/declaration/confirm", json={},
                       headers={"Authorization": OTHER}).status_code == 200


def test_no_route_removes_an_owner(owned_client):
    client, _ = owned_client
    routes = [(r.path, m) for r in client.app.routes
              for m in getattr(r, "methods", set())]
    assert not [(p, m) for p, m in routes
                if "owners" in p and m in ("DELETE", "PUT", "PATCH")]


def test_a_non_owner_cannot_add_owners(owned_client, data_steward):
    client, _ = owned_client
    job_id = client.post("/jobs",
                         headers={"Authorization": TOKEN}).json()["job_id"]
    response = client.post(f"/jobs/{job_id}/owners",
                           json={"orcid": data_steward.value},
                           headers={"Authorization": OTHER})
    assert response.status_code == 404, "they cannot see it, so they cannot add"


def test_a_malformed_orcid_is_refused(owned_client):
    client, _ = owned_client
    job_id = client.post("/jobs",
                         headers={"Authorization": TOKEN}).json()["job_id"]
    response = client.post(f"/jobs/{job_id}/owners",
                           json={"orcid": "0000-0002-1825-0098"},
                           headers={"Authorization": TOKEN})
    assert response.status_code == 422
    assert response.json()["code"] == "malformed-orcid"


def test_an_auditor_reads_but_cannot_act(owned_client):
    client, _ = owned_client
    job_id = client.post("/jobs",
                         headers={"Authorization": TOKEN}).json()["job_id"]
    assert client.get(f"/jobs/{job_id}",
                      headers={"Authorization": AUDIT}).status_code == 200
    response = client.post(f"/jobs/{job_id}/declaration/confirm", json={},
                           headers={"Authorization": AUDIT})
    assert response.status_code == 409
    assert "requires ownership" in response.json()["detail"]


def test_the_sole_owned_list_is_available_before_leaving(owned_client,
                                                         data_steward):
    client, _ = owned_client
    alone = client.post("/jobs",
                        headers={"Authorization": TOKEN}).json()["job_id"]
    shared = client.post("/jobs",
                         headers={"Authorization": TOKEN}).json()["job_id"]
    client.post(f"/jobs/{shared}/owners", json={"orcid": data_steward.value},
                headers={"Authorization": TOKEN})

    listed = client.get("/jobs/sole-owned",
                        headers={"Authorization": TOKEN}).json()
    assert [j["job_id"] for j in listed] == [alone]


def test_the_owners_endpoint_shows_who_granted_what(owned_client, researcher,
                                                    data_steward):
    client, _ = owned_client
    job_id = client.post("/jobs",
                         headers={"Authorization": TOKEN}).json()["job_id"]
    client.post(f"/jobs/{job_id}/owners",
                json={"orcid": data_steward.value, "reason": "covering leave"},
                headers={"Authorization": TOKEN})
    body = client.get(f"/jobs/{job_id}/owners",
                      headers={"Authorization": TOKEN}).json()
    assert body["creator"] == researcher.value
    assert body["sole_owned"] is False
    granted = [g for g in body["grants"] if g["granted_by"]]
    assert granted[0]["reason"] == "covering leave"
