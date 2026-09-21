"""Deposit against the real Zenodo sandbox.

The cluster-4 definition of done. Skipped unless DD_LIVE_TESTS=1 and a sandbox
token is present.

**The purpose is divergence.** The offline suite runs against a fake that
encodes what we believe the API does, and the fake has already been wrong once:
it omitted the file list a real deposition returns, which made a resumed deposit
re-upload. Every difference this test finds is a finding to record rather than a
bug to paper over quietly.

Deposits are made and left unpublished by default. Publishing to the sandbox is
harmless but produces a real DOI that cannot be withdrawn, so it happens only
when DD_LIVE_PUBLISH=1 is set as well: an irreversible action should take a
deliberate act to trigger, in a test suite as much as in the system.
"""

from __future__ import annotations

import os

import pytest
from datadirector_contracts import (
    Affiliation, CanonicalRecord, Creator, Description, DescriptionKind, Orcid,
    ResourceType, Rights,
)

from datadirector.credentials.broker import CredentialBroker
from datadirector.errors import ExternalServiceError
from datadirector.repositories.zenodo import (
    SANDBOX, DepositionRegistry, ZenodoDriver,
)
from datadirector.schemas.datacite import ZenodoDataCiteProfile

requires_live = pytest.mark.skipif(
    os.environ.get("DD_LIVE_TESTS") != "1",
    reason="set DD_LIVE_TESTS=1 to run")

requires_token = pytest.mark.skipif(
    not os.environ.get("DD_ZENODO_TOKEN"),
    reason="set DD_ZENODO_TOKEN to a sandbox token with deposit:write "
           "and deposit:actions")

CARBERRY = Orcid(value="0000-0002-1825-0097")


@pytest.fixture
def live_driver(tmp_path):
    broker = CredentialBroker({"zenodo:deposit": "DD_ZENODO_TOKEN"})
    base = os.environ.get("DD_ZENODO_BASE", SANDBOX)
    return ZenodoDriver(broker, DepositionRegistry(tmp_path / "depositions.json"),
                        base_url=base, timeout=120.0)


@pytest.fixture
def live_record():
    return CanonicalRecord(
        title="Data Director conformance test deposit (please ignore)",
        creators=[Creator(name="Carberry, Josiah", orcid=CARBERRY.value,
                          affiliations=[Affiliation(name="Brown University")])],
        publication_year=2026,
        resource_type=ResourceType.DATASET,
        descriptions=[Description(
            text="Automated test deposit produced by an implementation of the "
                 "RDA Data Director Agentic AI Blueprint. Not research data.",
            kind=DescriptionKind.ABSTRACT)],
        rights=Rights(licence_id="CC-BY-4.0",
                      uri="https://creativecommons.org/licenses/by/4.0/"),
    )


@pytest.fixture
def payload_file(tmp_path):
    path = tmp_path / "observations.csv"
    path.write_text("station,date,temp_c\nS14,2019-03-02,4.1\nS14,2019-03-03,5.6\n")
    return path


@requires_live
@requires_token
def test_create_and_upload_against_the_sandbox(live_driver, live_record,
                                               payload_file, record_measurement,
                                               capsys):
    """Create, set metadata, upload. Stops short of publishing.

    Records what the real API returned so that divergence from the fake is
    visible rather than assumed away.
    """
    problems = live_driver.preflight(live_record, [payload_file],
                                     profile=ZenodoDataCiteProfile())
    assert not problems, (
        f"preflight refused a record the repository would accept: {problems}. "
        "A profile stricter than its target refuses deposits that would succeed."
    )

    deposition_id = live_driver.begin("live-job-a")
    live_driver.set_metadata(deposition_id, live_record)
    live_driver.upload(deposition_id, payload_file)

    payload = live_driver._deposition(deposition_id)
    files_key_present = "files" in payload
    file_names = [f.get("filename") or f.get("key")
                  for f in payload.get("files", [])]

    record_measurement(
        measurement="zenodo-live", model="n/a", fixture="create-upload",
        deposition_id=deposition_id,
        deposition_keys=sorted(payload.keys()),
        link_keys=sorted(payload.get("links", {}).keys()),
        files_key_present=files_key_present, file_names=file_names,
        state=payload.get("state"),
    )
    with capsys.disabled():
        print(f"\n  sandbox deposition {deposition_id}: state={payload.get('state')}")
        print(f"      top-level keys: {sorted(payload.keys())}")
        print(f"      link keys: {sorted(payload.get('links', {}).keys())}")
        print(f"      files: {file_names}")

    # Compare against the recorded shape, so a change in the API surfaces here
    # rather than as a puzzling failure much later.
    import json as _j
    from pathlib import Path as _P
    shape_path = (_P(__file__).parent.parent / "fixtures" / "zenodo" /
                  "deposition-shape.json")
    shape = _j.loads(shape_path.read_text(encoding="utf-8"))
    new_keys = sorted(set(payload) - set(shape["top_level"]))
    lost_keys = sorted(set(shape["top_level"]) - set(payload))
    if new_keys or lost_keys:
        with capsys.disabled():
            print(f"      shape drift: added {new_keys}, removed {lost_keys}")
            print(f"      update {shape_path.name} from this response")

    # The fake's error, made into a check against the real thing.
    assert files_key_present, (
        "the real deposition carries no 'files' key, so the driver's "
        "resume-without-re-upload logic rests on an assumption that does not hold"
    )
    assert payload_file.name in file_names


@requires_live
@requires_token
def test_the_real_api_refuses_metadata_we_expect_it_to_refuse(live_driver,
                                                              payload_file,
                                                              record_measurement,
                                                              capsys):
    """Whether our idea of invalid matches Zenodo's.

    The fake refuses a record with no creators. If the sandbox accepts one, our
    preflight is stricter than the repository, which is a safe direction but
    worth knowing; if it refuses differently, the message shape matters for what
    a researcher is shown.
    """
    incomplete = CanonicalRecord(title="Conformance test: no creators")
    deposition_id = live_driver.begin("live-job-b")
    try:
        live_driver.set_metadata(deposition_id, incomplete)
        refused_at_draft, draft_message = False, ""
    except ExternalServiceError as exc:
        refused_at_draft, draft_message = True, str(exc)

    record_measurement(
        measurement="zenodo-live", model="n/a", fixture="invalid-metadata",
        refused_at_draft=refused_at_draft, message=draft_message[:300])
    with capsys.disabled():
        print(f"\n  metadata without creators refused at draft: {refused_at_draft}")
        if refused_at_draft:
            print(f"      {draft_message[:200]}")
        else:
            print("      (the API validates on publish, not on update; our own "
                  "preflight is what refuses this before a round trip)")


@requires_live
@requires_token
@pytest.mark.skipif(os.environ.get("DD_LIVE_PUBLISH") != "1",
                    reason="set DD_LIVE_PUBLISH=1 to publish; a sandbox DOI is "
                           "real and cannot be withdrawn")
def test_publish_produces_a_resolvable_identifier(live_driver, live_record,
                                                  payload_file,
                                                  record_measurement, capsys):
    """The end of the path: a file becomes a DOI."""
    receipt = live_driver.deposit("live-job-c", live_record, [payload_file],
                                  on_behalf_of=CARBERRY)
    record_measurement(
        measurement="zenodo-live", model="n/a", fixture="publish",
        pid=receipt.pid, concept_pid=receipt.concept_pid,
        landing_page=receipt.landing_page)
    with capsys.disabled():
        print(f"\n  published: {receipt.pid}")
        print(f"      concept: {receipt.concept_pid}")
        print(f"      landing: {receipt.landing_page}")

    assert receipt.pid.startswith("10.5072/"), "sandbox DOIs use the 10.5072 prefix"
    assert receipt.concept_pid and receipt.concept_pid != receipt.pid, (
        "the concept identifier must differ from the version identifier (§7.1)"
    )


@requires_live
@requires_token
@pytest.mark.skipif(os.environ.get("DD_LIVE_PUBLISH") != "1",
                    reason="set DD_LIVE_PUBLISH=1 to publish")
def test_publishing_twice_is_not_an_error_live(live_driver, live_record,
                                               payload_file, capsys):
    """The offline suite asserts this against the fake. Whether the real API
    returns 409 or something else decides whether the driver's retry path is
    correct in production."""
    first = live_driver.deposit("live-job-d", live_record, [payload_file],
                                on_behalf_of=CARBERRY)
    second = live_driver.deposit("live-job-d", live_record, [payload_file],
                                 on_behalf_of=CARBERRY)
    with capsys.disabled():
        print(f"\n  republish returned the same identifier: "
              f"{first.pid == second.pid}")
    assert first.pid == second.pid
