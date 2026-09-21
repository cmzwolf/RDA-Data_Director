"""Cluster 4 Part A: the canonical record and schema projection.

The claim under test is that the internal record is genuinely
schema-independent. A record that can only be projected one way is a DataCite
record wearing a different name, and that would only become apparent at the
second repository — which is the expensive place to find out.
"""

from datetime import date

import pytest
from datadirector_contracts import (
    Affiliation, CanonicalRecord, Contributor, Creator, DateEntry, DateKind,
    Description, DescriptionKind, FieldOrigin, FieldProvenance, Funding, Orcid,
    RelatedResource, RelationOrigin, ResourceType, Rights, Subject,
)

from datadirector.schemas.datacite import (
    DataCiteProfile, ZenodoDataCiteProfile,
)
from datadirector.schemas.rocrate import RoCrateProfile

ALICE_ORCID = "0000-0002-1825-0097"


@pytest.fixture
def record(researcher):
    return CanonicalRecord(
        title="Surface temperature series, station 14, 2019-2023",
        creators=[Creator(name="Aroa, Miriam", given_name="Miriam",
                          family_name="Aroa", orcid=ALICE_ORCID,
                          affiliations=[Affiliation(
                              name="Observatoire de Paris",
                              ror="https://ror.org/029brtt94")])],
        publication_year=2026, publisher="Zenodo",
        resource_type=ResourceType.DATASET,
        descriptions=[Description(text="Hourly readings.",
                                  kind=DescriptionKind.ABSTRACT)],
        subjects=[Subject(term="air temperature",
                          uri="http://purl.obolibrary.org/obo/ENVO_09200001",
                          scheme="ENVO"),
                  Subject(term="station 14")],
        contributors=[Contributor(name="Bani, J.", role="DataCurator")],
        rights=Rights(licence_id="CC-BY-4.0",
                      uri="https://creativecommons.org/licenses/by/4.0/"),
        dates=[DateEntry(kind=DateKind.COLLECTED, value=date(2019, 1, 1),
                         end=date(2023, 12, 31))],
        related=[RelatedResource(identifier="10.1234/paper",
                                 identifier_type="DOI",
                                 relation_type="IsSupplementTo",
                                 origin=RelationOrigin.MODEL_PROPOSED,
                                 approved_by=researcher)],
        funding=[Funding(funder_name="ANR", funder_ror="https://ror.org/00rbzpz17",
                         award_number="ANR-21-XXXX")],
        language="en", version="1.0",
        provenance={"title": FieldProvenance(field_path="title",
                                             origin=FieldOrigin.RESEARCHER_SUPPLIED)},
    )


# -- the record itself ----------------------------------------------------

def test_ungrounded_subjects_are_surfaced_not_dropped(record):
    """R2 requires the system to say where no controlled vocabulary exists.
    Silently discarding an ungrounded term would hide that finding."""
    assert [s.term for s in record.ungrounded_subjects()] == ["station 14"]


def test_fields_without_provenance_are_reportable(record):
    """The omission is invisible in the record and only surfaces when a reviewer
    asks why a value is there."""
    missing = record.fields_without_provenance()
    assert "title" not in missing
    assert "creators" in missing and "publisher" in missing


def test_the_record_is_frozen(record):
    """Freezing is what lets the gate compare two states rather than trust that
    nothing changed between display and deposit."""
    with pytest.raises(Exception):
        record.title = "something else"


# -- DataCite projection --------------------------------------------------

def test_datacite_projection_carries_identifiers(record):
    out = DataCiteProfile().project(record)
    creator = out["creators"][0]
    assert creator["nameIdentifiers"][0]["nameIdentifier"].endswith(ALICE_ORCID)
    assert creator["affiliation"][0]["affiliationIdentifierScheme"] == "ROR"
    assert out["fundingReferences"][0]["funderIdentifierType"] == "ROR"


def test_datacite_date_ranges_use_the_slash_form(record):
    assert DataCiteProfile().project(record)["dates"][0]["date"] == \
        "2019-01-01/2023-12-31"


def test_controlled_subjects_keep_their_uri_and_scheme(record):
    subjects = DataCiteProfile().project(record)["subjects"]
    grounded = [s for s in subjects if "valueUri" in s]
    assert len(grounded) == 1 and grounded[0]["subjectScheme"] == "ENVO"


def test_missing_required_fields_are_reported_never_filled():
    """A profile supplying a default publisher would change what the human
    approved, which is the same objection as a driver repairing metadata."""
    thin = CanonicalRecord(title="Only a title")
    missing = DataCiteProfile().missing_required(thin)
    assert set(missing) == {"creators", "publication_year", "publisher"}
    assert DataCiteProfile().project(thin).get("publisher") is None


# -- the Zenodo subset ----------------------------------------------------

def test_zenodo_declares_a_narrower_relation_vocabulary():
    """Zenodo accepts a subset of DataCite. The profile states what Zenodo takes,
    not what DataCite defines."""
    full = DataCiteProfile().relation_vocabulary().permitted
    zenodo = ZenodoDataCiteProfile().relation_vocabulary().permitted
    assert zenodo < full
    assert "IsVersionOf" in full and "IsVersionOf" not in zenodo


def test_a_relation_outside_the_target_vocabulary_is_refused(record):
    vocabulary = ZenodoDataCiteProfile().relation_vocabulary()
    vocabulary.validate_relation("IsSupplementTo")
    with pytest.raises(ValueError, match="not in the"):
        vocabulary.validate_relation("IsVersionOf")


# -- RO-Crate projection: structurally different, not a renaming ----------

def test_ro_crate_makes_people_graph_nodes(record):
    """The structural difference from DataCite: an ORCID is a node with its own
    identifier, not a nested attribute of a creator object."""
    out = RoCrateProfile().project(record)
    root = next(n for n in out["@graph"] if n["@id"] == "./")
    assert root["author"] == [{"@id": f"https://orcid.org/{ALICE_ORCID}"}]
    person = next(n for n in out["@graph"]
                  if n["@id"] == f"https://orcid.org/{ALICE_ORCID}")
    assert person["@type"] == "Person"
    assert person["affiliation"] == [{"@id": "https://ror.org/029brtt94"}]


def test_ro_crate_is_valid_json_ld_with_a_conformance_node(record):
    out = RoCrateProfile().project(record)
    assert out["@context"].startswith("https://w3id.org/ro/crate/")
    descriptor = out["@graph"][0]
    assert descriptor["@id"] == "ro-crate-metadata.json"
    assert descriptor["about"] == {"@id": "./"}


def test_an_unmapped_relation_becomes_a_mention_not_a_silent_drop(researcher):
    """schema.org has no closed relation vocabulary; anything without a natural
    property is emitted generically rather than lost."""
    record = CanonicalRecord(
        title="T",
        related=[RelatedResource(identifier="10.5555/obsolete",
                                 identifier_type="DOI",
                                 relation_type="Obsoletes",
                                 origin=RelationOrigin.RESEARCHER_SUPPLIED)])
    root = next(n for n in RoCrateProfile().project(record)["@graph"]
                if n["@id"] == "./")
    assert root["mentions"] == [{"@id": "10.5555/obsolete"}]


def test_both_profiles_project_the_same_record(record):
    """The claim this part exists to test. Two structurally unrelated outputs
    from one internal representation."""
    datacite = DataCiteProfile().project(record)
    crate = RoCrateProfile().project(record)
    assert datacite["titles"][0]["title"] == record.title
    root = next(n for n in crate["@graph"] if n["@id"] == "./")
    assert root["name"] == record.title
    assert "creators" in datacite and "creators" not in crate
    assert "@graph" in crate and "@graph" not in datacite


# ==========================================================================
# Parts D2 and D1: the fake repository, and the driver written against it
# ==========================================================================

from pathlib import Path  # noqa: E402

from datadirector.credentials.broker import CredentialBroker  # noqa: E402
from datadirector.errors import ExternalServiceError  # noqa: E402
from datadirector.repositories.zenodo import (  # noqa: E402
    DepositionRegistry, ZenodoDriver,
)

from .fake_zenodo import VALID_TOKEN, FakeZenodo  # noqa: E402


@pytest.fixture
def zenodo():
    with FakeZenodo() as server:
        yield server


@pytest.fixture
def driver(zenodo, tmp_path):
    broker = CredentialBroker({"zenodo:deposit": "DD_ZENODO_TOKEN"},
                              environ={"DD_ZENODO_TOKEN": VALID_TOKEN})
    return ZenodoDriver(broker, DepositionRegistry(tmp_path / "depositions.json"),
                        base_url=zenodo.base_url)


@pytest.fixture
def payload(tmp_path):
    path = tmp_path / "observations.csv"
    path.write_text("station,date,temp\nS14,2019-03-02,4.1\n")
    return [path]


# -- preflight reports and never repairs ----------------------------------

def test_preflight_reports_missing_required_fields(driver, payload):
    thin = CanonicalRecord(title="")
    problems = driver.preflight(thin, payload, profile=DataCiteProfile())
    assert any("title" in p for p in problems)
    assert any("creator" in p.lower() for p in problems)


def test_preflight_refuses_a_deposit_with_no_files(driver, record):
    assert any("at least one file" in p
               for p in driver.preflight(record, [], profile=DataCiteProfile()))


def test_preflight_catches_an_empty_file(driver, record, tmp_path):
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    assert any("empty" in p for p in driver.preflight(record, [empty]))


def test_preflight_refuses_a_relation_the_target_does_not_accept(driver, tmp_path,
                                                                 researcher, payload):
    """Zenodo accepts a subset of DataCite relations (ADR-026)."""
    record = CanonicalRecord(
        title="T", creators=[Creator(name="A")],
        related=[RelatedResource(identifier="10.1/x", identifier_type="DOI",
                                 relation_type="IsVersionOf",
                                 origin=RelationOrigin.MODEL_PROPOSED,
                                 approved_by=researcher)])
    problems = driver.preflight(record, payload,
                                profile=ZenodoDataCiteProfile())
    assert any("IsVersionOf" in p for p in problems)


def test_preflight_does_not_mutate_the_record(driver, record, payload):
    """A driver that repairs metadata has changed what the human approved."""
    before = record.model_dump_json()
    driver.preflight(record, payload, profile=DataCiteProfile())
    assert record.model_dump_json() == before


# -- the happy path -------------------------------------------------------

def test_deposit_produces_a_version_and_a_concept_identifier(driver, record,
                                                             payload, researcher):
    receipt = driver.deposit("job-a", record, payload, on_behalf_of=researcher)
    assert receipt.pid.startswith("10.5072/zenodo.")
    assert receipt.concept_pid and receipt.concept_pid != receipt.pid
    assert receipt.landing_page


def test_metadata_reaches_the_repository_in_its_own_shape(driver, record, payload,
                                                          researcher, zenodo):
    """Zenodo's schema is not DataCite's: upload_type, not resourceTypeGeneral."""
    driver.deposit("job-a", record, payload, on_behalf_of=researcher)
    stored = next(iter(zenodo.state.depositions.values()))
    assert stored["upload_type"] == "dataset"
    assert stored["creators"][0]["orcid"] == ALICE_ORCID
    assert "resourceTypeGeneral" not in stored


# -- failure and retry ----------------------------------------------------

def test_a_bad_credential_says_what_to_check(zenodo, tmp_path, record, payload,
                                             researcher):
    broker = CredentialBroker({"zenodo:deposit": "DD_ZENODO_TOKEN"},
                              environ={"DD_ZENODO_TOKEN": "not-a-real-token"})
    driver = ZenodoDriver(broker, DepositionRegistry(tmp_path / "d.json"),
                          base_url=zenodo.base_url)
    with pytest.raises(ExternalServiceError, match="deposit:write"):
        driver.deposit("job-a", record, payload, on_behalf_of=researcher)


def test_invalid_metadata_is_refused_with_the_field_named(driver, payload,
                                                          researcher, zenodo):
    incomplete = CanonicalRecord(title="T")  # no creators
    with pytest.raises(ExternalServiceError, match="creators"):
        driver.deposit("job-b", incomplete, payload, on_behalf_of=researcher)


def test_a_retry_after_a_dropped_upload_produces_one_record(driver, record,
                                                            payload, researcher,
                                                            zenodo):
    """The failure that matters: a duplicate deposit is not something the
    researcher can undo."""
    zenodo.state.drop_next_upload = True
    with pytest.raises(ExternalServiceError):
        driver.deposit("job-c", record, payload, on_behalf_of=researcher)

    receipt = driver.deposit("job-c", record, payload, on_behalf_of=researcher)
    assert len(zenodo.state.depositions) == 1, "the retry created a second record"
    assert receipt.pid


def test_idempotency_survives_a_process_restart(zenodo, tmp_path, record, payload,
                                                researcher):
    """An in-memory record would be gone exactly when it is needed."""
    registry_path = tmp_path / "depositions.json"
    broker = CredentialBroker({"zenodo:deposit": "DD_ZENODO_TOKEN"},
                              environ={"DD_ZENODO_TOKEN": VALID_TOKEN})
    first = ZenodoDriver(broker, DepositionRegistry(registry_path),
                         base_url=zenodo.base_url)
    deposition_id = first.begin("job-d")

    revived = ZenodoDriver(broker, DepositionRegistry(registry_path),
                           base_url=zenodo.base_url)
    assert revived.begin("job-d") == deposition_id
    assert len(zenodo.state.depositions) == 1


def test_publishing_twice_is_not_an_error(driver, record, payload, researcher):
    """A retry after a lost response would otherwise leave the job unable to
    complete despite the deposit having succeeded."""
    first = driver.deposit("job-e", record, payload, on_behalf_of=researcher)
    second = driver.deposit("job-e", record, payload, on_behalf_of=researcher)
    assert first.pid == second.pid


def test_a_resumed_deposit_does_not_re_upload(driver, record, payload,
                                              researcher, zenodo):
    deposition_id = driver.begin("job-f")
    driver.set_metadata(deposition_id, record)
    driver.upload(deposition_id, payload[0])
    before = sum(1 for method, path in zenodo.state.requests
                 if method == "PUT" and "bucket" in path)

    driver.deposit("job-f", record, payload, on_behalf_of=researcher)
    after = sum(1 for method, path in zenodo.state.requests
                if method == "PUT" and "bucket" in path)
    assert after == before, "an already-uploaded file was sent again"


def test_publishing_without_files_is_refused(driver, record, researcher):
    deposition_id = driver.begin("job-g")
    driver.set_metadata(deposition_id, record)
    with pytest.raises(ExternalServiceError, match="no files"):
        driver.publish(deposition_id, on_behalf_of=researcher)


def test_a_new_version_requires_a_published_deposition(driver, record, payload,
                                                       researcher):
    unpublished = driver.begin("job-h")
    with pytest.raises(ExternalServiceError, match="new version"):
        driver.new_version_of(unpublished)

    driver.deposit("job-i", record, payload, on_behalf_of=researcher)
    published = driver._registry.get("job-i")
    assert driver.new_version_of(published) != published


def test_no_credential_reaches_a_log_or_an_error(driver, record, payload,
                                                 researcher, zenodo):
    """The token must not surface in any message the workflow records."""
    zenodo.state.fail_next_publish_with = 400
    with pytest.raises(ExternalServiceError) as exc:
        driver.deposit("job-j", record, payload, on_behalf_of=researcher)
    assert VALID_TOKEN not in str(exc.value)


# ==========================================================================
# Part C: identity and delegated credentials
# ==========================================================================

import json as _json  # noqa: E402

import httpx  # noqa: E402

from datadirector.credentials.broker import Secret  # noqa: E402
from datadirector.credentials.oauth import (  # noqa: E402
    DelegatedTokenStore, ZenodoOAuthFlow,
)
from datadirector.errors import AuthorityError, CredentialError  # noqa: E402
from datadirector.identity.orcid import OrcidIdentityProvider  # noqa: E402


class _FakeOAuthTransport(httpx.BaseTransport):
    """Serves the token endpoint, recording what was posted."""

    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status = status
        self.posted: list[dict] = []

    def handle_request(self, request):
        from urllib.parse import parse_qs
        self.posted.append({k: v[0] for k, v in
                            parse_qs(request.content.decode()).items()})
        return httpx.Response(self.status, json=self.payload, request=request)


@pytest.fixture
def orcid_provider(tmp_path, monkeypatch):
    broker = CredentialBroker({"orcid:client-secret": "DD_ORCID_CLIENT_SECRET"},
                              environ={"DD_ORCID_CLIENT_SECRET": "shh"})
    return OrcidIdentityProvider("APP-123", broker,
                                 "https://example.org/callback")


def _patch_post(monkeypatch, transport):
    def fake_post(url, **kwargs):
        client = httpx.Client(transport=transport)
        try:
            return client.post(url, **kwargs)
        finally:
            client.close()
    monkeypatch.setattr(httpx, "post", fake_post)


# -- ORCID identity -------------------------------------------------------

def test_authorize_url_carries_the_state_and_redirect(orcid_provider):
    state = orcid_provider.new_state()
    url = orcid_provider.authorize_url(state)
    assert f"state={state}" in url
    assert "response_type=code" in url
    assert "scope=%2Fauthenticate" in url


def test_a_reused_state_is_refused(orcid_provider, monkeypatch):
    """A redemption we cannot tie to a request we issued is what a cross-site
    attack looks like."""
    _patch_post(monkeypatch, _FakeOAuthTransport({"orcid": ALICE_ORCID}))
    state = orcid_provider.new_state()
    assert orcid_provider.exchange("code-1", state=state).value == ALICE_ORCID
    with pytest.raises(AuthorityError, match="already been used"):
        orcid_provider.exchange("code-2", state=state)


def test_an_unknown_state_is_refused(orcid_provider, monkeypatch):
    _patch_post(monkeypatch, _FakeOAuthTransport({"orcid": ALICE_ORCID}))
    with pytest.raises(AuthorityError, match="unknown"):
        orcid_provider.exchange("code", state="never-issued")


def test_a_returned_orcid_is_checksum_validated(orcid_provider, monkeypatch):
    """An ORCID is the subject of every accountability claim in the system."""
    _patch_post(monkeypatch, _FakeOAuthTransport({"orcid": "0000-0002-1825-0098"}))
    with pytest.raises(ValueError, match="checksum"):
        orcid_provider.exchange("code", state=orcid_provider.new_state())


def test_no_identifier_means_no_workflow(orcid_provider, monkeypatch):
    """Without one, no action can be attributed to a person."""
    _patch_post(monkeypatch, _FakeOAuthTransport({"token_type": "bearer"}))
    with pytest.raises(AuthorityError, match="no identifier"):
        orcid_provider.exchange("code", state=orcid_provider.new_state())


def test_the_client_secret_is_sent_but_never_surfaces(orcid_provider, monkeypatch):
    transport = _FakeOAuthTransport({}, status=400)
    _patch_post(monkeypatch, transport)
    with pytest.raises(AuthorityError) as exc:
        orcid_provider.exchange("code", state=orcid_provider.new_state())
    assert transport.posted[0]["client_secret"] == "shh"
    assert "shh" not in str(exc.value)


# -- delegated repository tokens ------------------------------------------

def test_tokens_are_held_per_user_per_repository(tmp_path, researcher,
                                                 data_steward):
    store = DelegatedTokenStore(tmp_path / "tokens.json")
    store.put(researcher, "zenodo", "token-a")
    store.put(data_steward, "zenodo", "token-b")
    assert store.get(researcher, "zenodo").reveal() == "token-a"
    assert store.get(data_steward, "zenodo").reveal() == "token-b"


def test_a_missing_delegation_says_what_must_happen(tmp_path, researcher):
    store = DelegatedTokenStore(tmp_path / "tokens.json")
    with pytest.raises(CredentialError, match="authorise this deployment"):
        store.get(researcher, "zenodo")


def test_a_stored_token_never_prints_itself(tmp_path, researcher):
    store = DelegatedTokenStore(tmp_path / "tokens.json")
    store.put(researcher, "zenodo", "tok_secret_value")
    held = store.get(researcher, "zenodo")
    assert "tok_secret" not in repr(held)
    assert "tok_secret" not in f"{held}"


def test_the_token_file_is_not_world_readable(tmp_path, researcher):
    store = DelegatedTokenStore(tmp_path / "tokens.json")
    store.put(researcher, "zenodo", "t")
    assert oct(store.path.stat().st_mode)[-3:] == "600"


def test_forgetting_a_delegation_is_local_only(tmp_path, researcher):
    store = DelegatedTokenStore(tmp_path / "tokens.json")
    store.put(researcher, "zenodo", "t")
    store.forget(researcher, "zenodo")
    assert store.has(researcher, "zenodo") is False


def test_the_flow_binds_the_state_to_the_requesting_user(tmp_path, researcher,
                                                         data_steward, monkeypatch):
    """A redemption arriving with someone else's state would store one
    researcher's token under another's name, misattributing every deposit."""
    store = DelegatedTokenStore(tmp_path / "tokens.json")
    flow = ZenodoOAuthFlow("cid", Secret("csecret", "zenodo:client"),
                           "https://example.org/cb", store,
                           base_url="https://sandbox.zenodo.org")
    _patch_post(monkeypatch, _FakeOAuthTransport({"access_token": "deposit-token"}))

    _, state = flow.authorize_url(researcher)
    assert flow.exchange("code", state) == researcher
    assert store.has(researcher, "zenodo")
    assert store.has(data_steward, "zenodo") is False


def test_the_flow_requests_both_deposit_scopes(tmp_path, researcher):
    store = DelegatedTokenStore(tmp_path / "tokens.json")
    flow = ZenodoOAuthFlow("cid", Secret("s", "x"), "https://example.org/cb",
                           store, base_url="https://sandbox.zenodo.org")
    url, _ = flow.authorize_url(researcher)
    assert "deposit%3Awrite" in url and "deposit%3Aactions" in url


def test_a_replayed_authorisation_is_refused(tmp_path, researcher, monkeypatch):
    store = DelegatedTokenStore(tmp_path / "tokens.json")
    flow = ZenodoOAuthFlow("cid", Secret("s", "x"), "https://example.org/cb",
                           store, base_url="https://sandbox.zenodo.org")
    _patch_post(monkeypatch, _FakeOAuthTransport({"access_token": "t"}))
    _, state = flow.authorize_url(researcher)
    flow.exchange("code", state)
    with pytest.raises(AuthorityError, match="already been used"):
        flow.exchange("code", state)


# ==========================================================================
# Part B: metadata, documentation and validation
# ==========================================================================

from datadirector_contracts import (  # noqa: E402
    Digest, ModelCapability, ModelRequest, ModelResponse, PolicyConfig,
    Residency, SensitivityClass, VocabularyTerm,
)

from datadirector.agents.documentation import DocumentationAgent  # noqa: E402
from datadirector.agents.metadata import MetadataAgent  # noqa: E402
from datadirector.agents.validation import (  # noqa: E402
    ValidationAgent, blocks_deposit,
)
from datadirector.policy.pep import PolicyEnforcementPoint  # noqa: E402
from datadirector.state.projection import JobState  # noqa: E402


class Scripted:
    name = "scripted"

    def __init__(self, reply):
        self.reply = reply
        self.seen = []

    def residency(self):
        return Residency.ON_PREMISE

    def capabilities(self):
        return {ModelCapability.TEXT_GENERATION}

    def complete(self, request):
        self.seen.append(request)
        return ModelResponse(text=self.reply, model_id=self.name,
                             input_digest=Digest.of_bytes(b""))


def _pep(model):
    return PolicyEnforcementPoint(
        PolicyConfig(backend_by_sensitivity={
            SensitivityClass.PUBLIC: ["m"],
            SensitivityClass.INTERNAL: ["m"],
            SensitivityClass.SENSITIVE: ["m"],
        }), {"m": model})


class FakeVocabulary:
    """Returns a term for anything in `known`, nothing otherwise."""

    def __init__(self, known: dict[str, str]):
        self.known = known

    def search(self, query, *, scheme=None, limit=10):
        uri = self.known.get(query.lower())
        if not uri:
            return []
        return [VocabularyTerm(uri=uri, label=query, scheme="ENVO")]


DRAFT_REPLY = _json.dumps({
    "title": "Surface temperature series, station 14",
    "abstract": "Hourly readings from a coastal weather station.",
    "methods": None,
    "keywords": ["air temperature", "station 14"],
    "resource_type": "Dataset",
    "language": "en",
    "uncertain": ["funder: no funding information appears in the material"],
})

PROFILE = {"files": [{"path": "obs.csv",
                      "columns": [{"name": "station"}, {"name": "date"},
                                  {"name": "temp"}]}]}


# -- the metadata agent ---------------------------------------------------

def test_ungrounded_keywords_are_reported_not_passed_off_as_controlled():
    """Dropping would hide a finding R2 asks us to make; passing off would
    misrepresent a guess as a vocabulary term."""
    agent = MetadataAgent(_pep(Scripted(DRAFT_REPLY)),
                          FakeVocabulary({"air temperature": "http://envo/1"}))
    record, decision = agent.draft(
        JobState(job_id="job-x", step="metadata"), profile_summary=PROFILE)

    grounded = [s for s in record.subjects if s.is_controlled]
    ungrounded = record.ungrounded_subjects()
    assert [s.term for s in grounded] == ["air temperature"]
    assert [s.term for s in ungrounded] == ["station 14"]
    assert any("station 14" in u for u in decision.undetermined)


def test_the_absence_of_a_vocabulary_is_recorded_as_a_value():
    """R2 requires the system to state openly where none exists. A field is a
    statement; a sentence in generated prose is not."""
    agent = MetadataAgent(_pep(Scripted(DRAFT_REPLY)), FakeVocabulary({}))
    record, _ = agent.draft(JobState(job_id="job-x", step="metadata"),
                            profile_summary=PROFILE)
    assert record.origin_of("subjects.ungrounded").origin.value == "absent-by-design"


def test_missing_creators_are_reported_not_invented():
    """A plausible wrong affiliation is worse than a blank: a blank prompts a
    question, a plausible value is believed."""
    agent = MetadataAgent(_pep(Scripted(DRAFT_REPLY)), FakeVocabulary({}))
    record, decision = agent.draft(JobState(job_id="job-x", step="metadata"),
                                   profile_summary=PROFILE)
    assert record.creators == []
    assert any("creators" in u for u in decision.undetermined)


def test_depositor_statements_outrank_inference():
    agent = MetadataAgent(_pep(Scripted(DRAFT_REPLY)), FakeVocabulary({}))
    record, _ = agent.draft(JobState(job_id="job-x", step="metadata"),
                            profile_summary=PROFILE,
                            stated={"title": "The depositor's own title"})
    assert record.title == "The depositor's own title"
    assert record.origin_of("title").origin.value == "researcher-supplied"


def test_an_unreadable_reply_yields_no_invented_record():
    agent = MetadataAgent(_pep(Scripted("I could not do that")), FakeVocabulary({}))
    record, decision = agent.draft(JobState(job_id="job-x", step="metadata"),
                                   profile_summary=PROFILE)
    assert record.descriptions == [] and record.subjects == []
    assert any("title" in u for u in decision.undetermined)


def test_the_metadata_agent_does_not_see_the_data():
    """By this point the material has been examined; re-reading would be
    exposure without new information."""
    model = Scripted(DRAFT_REPLY)
    MetadataAgent(_pep(model), FakeVocabulary({})).draft(
        JobState(job_id="job-x", step="metadata"), profile_summary=PROFILE)
    assert "station" in model.seen[0].user_content       # column names
    assert "S14,2019" not in model.seen[0].user_content  # never values


# -- the documentation agent ----------------------------------------------

DOC_REPLY = _json.dumps({
    "readme_sections": [{"heading": "Overview", "body": "One CSV of readings."}],
    "variables": [
        {"name": "temp", "described_as": "air temperature", "unit": "degC",
         "gap": None},
        {"name": "flag", "described_as": None, "unit": None,
         "gap": "the meaning of this code is not derivable from the data"},
    ],
    "gaps": ["collection procedure: only the depositor knows how these were taken"],
})


def test_variables_the_agent_cannot_define_are_gaps_not_definitions():
    """A data dictionary whose definitions were invented is worse than none: it
    is read as authoritative by someone who was not there."""
    agent = DocumentationAgent(_pep(Scripted(DOC_REPLY)))
    documentation, decision = agent.draft(
        JobState(job_id="job-x", step="metadata"),
        profile_summary=PROFILE, record_summary={})

    assert [v.name for v in documentation.incomplete_variables] == ["flag"]
    assert any("flag" in u for u in decision.undetermined)
    assert any("collection procedure" in u for u in decision.undetermined)


def test_a_readme_is_drafted_from_structure():
    agent = DocumentationAgent(_pep(Scripted(DOC_REPLY)))
    documentation, _ = agent.draft(JobState(job_id="job-x", step="metadata"),
                                   profile_summary=PROFILE, record_summary={})
    assert documentation.readme.startswith("## Overview")


# -- validation: C15 as contradiction, not quality ------------------------

def test_a_title_claiming_years_the_data_do_not_cover_is_flagged(record):
    """Contradiction is checkable; quality is not (ADR-011)."""
    wrong = record.model_copy(update={
        "title": "Temperature series 1975-1980",
    })
    findings, _ = ValidationAgent(DataCiteProfile()).validate(wrong)
    assert any(f.rule == "c15-temporal-consistency" for f in findings)


def test_a_description_naming_a_column_that_does_not_exist_is_flagged(record):
    described = record.model_copy(update={
        "descriptions": [Description(text="The 'humidity' column is hourly.")],
    })
    findings, _ = ValidationAgent(DataCiteProfile()).validate(
        described, profile_summary=PROFILE)
    assert any(f.rule == "c15-column-consistency" and "humidity" in f.message
               for f in findings)


def test_a_column_that_does_exist_is_not_flagged(record):
    described = record.model_copy(update={
        "descriptions": [Description(text="The 'temp' column is hourly.")],
    })
    findings, _ = ValidationAgent(DataCiteProfile()).validate(
        described, profile_summary=PROFILE)
    assert not any(f.rule == "c15-column-consistency" for f in findings)


def test_a_field_with_no_recorded_origin_is_a_finding(record):
    """C14: a value nobody can account for is a finding, not an omission."""
    findings, _ = ValidationAgent(DataCiteProfile()).validate(record)
    assert any(f.rule == "c14-field-provenance" and f.field == "creators"
               for f in findings)


def test_missing_required_fields_are_errors_and_block_deposit():
    findings, _ = ValidationAgent(DataCiteProfile()).validate(
        CanonicalRecord(title="T"))
    assert blocks_deposit(findings) is True


def test_warnings_alone_do_not_block(record):
    findings, _ = ValidationAgent(DataCiteProfile()).validate(record)
    assert not blocks_deposit(findings)
    assert any(f.severity == "warning" for f in findings)


def test_a_future_publication_year_is_an_error(record):
    findings, _ = ValidationAgent(DataCiteProfile()).validate(
        record.model_copy(update={"publication_year": 2999}))
    assert blocks_deposit(findings)


def test_validation_uses_no_model():
    """Deterministic by construction: a repository will not accept 'the model
    thought it was fine'."""
    agent = ValidationAgent(DataCiteProfile())
    assert not hasattr(agent, "_pep")


# ==========================================================================
# Part E: the publication agent
# ==========================================================================

from datadirector_contracts import (  # noqa: E402
    DepositReceipt, EventKind, InspectionTier, ItemDecision, MediaFinding,
    ReasonCode, RedactionProposal, Treatment, UninspectedReason,
    ValidationFinding,
)

from datadirector.agents.publication import (  # noqa: E402
    NothingToDeposit, PublicationAgent,
)
from datadirector.gate.items import (  # noqa: E402
    Gate, from_media_findings, from_redaction_proposals,
)


@pytest.fixture
def publication(driver, tmp_path):
    return PublicationAgent(driver, tmp_path / "work")


def _state(job_id):
    return JobState(job_id=job_id, step="deposit")


def _uninspected(name="scan.jpg"):
    return MediaFinding(
        artefact=name, media_type="image", tier=InspectionTier.NONE,
        uninspected_reason=UninspectedReason.NO_CAPABLE_BACKEND,
        sensitivity=SensitivityClass.SENSITIVE, presumed=True)


def _error():
    return ValidationFinding(severity="error", field="creators",
                             message="required by DataCite")


def test_an_unresolved_gate_item_refuses_the_deposit(publication, record, payload,
                                                     researcher, job_id):
    gate = Gate()
    gate.add(from_media_findings([_uninspected()]))
    with pytest.raises(AuthorityError, match="unresolved"):
        publication.deposit(_state(job_id), record, payload, gate=gate,
                            findings=[], human=researcher)


def test_a_validation_error_refuses_the_deposit(publication, record, payload,
                                                researcher, job_id):
    """The repository would refuse it anyway; failing here is cheaper and more
    legible."""
    with pytest.raises(AuthorityError, match="validation error"):
        publication.deposit(_state(job_id), record, payload, gate=Gate(),
                            findings=[_error()], human=researcher)


def test_everything_blocking_is_reported_at_once(publication, record, payload,
                                                 researcher, job_id):
    """A researcher should see all of it, not discover it in sequence."""
    gate = Gate()
    gate.add(from_media_findings([_uninspected("a.jpg"), _uninspected("b.jpg")]))
    blocking = publication.check_ready(gate, [_error()])
    assert len(blocking) == 3


def test_an_excluded_artefact_is_not_uploaded(publication, record, tmp_path,
                                              researcher, job_id, zenodo):
    keep = tmp_path / "keep.csv"
    keep.write_text("a\n1\n")
    drop = tmp_path / "drop.csv"
    drop.write_text("b\n2\n")

    gate = Gate()
    gate.add(from_redaction_proposals([RedactionProposal(
        artefact="drop.csv", location="whole file",
        reason_code=ReasonCode.PERSONAL_DATA, treatment=Treatment.SUPPRESS,
        evidence="contains direct identifiers")]))
    gate.add(from_media_findings([_uninspected("drop.csv")]))
    gate.resolve("redaction:drop.csv:whole file", ItemDecision.APPROVE,
                 human=researcher)
    gate.resolve("uninspected:drop.csv", ItemDecision.EXCLUDE_FROM_DEPOSIT,
                 human=researcher)

    receipt, events, _ = publication.deposit(
        _state(job_id), record, [keep, drop], gate=gate, findings=[],
        human=researcher)
    assert events[0].payload["files"] == ["keep.csv"]
    assert events[0].payload["excluded"] == ["drop.csv"]
    uploaded = set(next(iter(zenodo.state.files.values())))
    assert uploaded == {"keep.csv"}


def test_excluding_everything_is_a_terminal_success(publication, record, tmp_path,
                                                    researcher, job_id):
    """The researcher decided nothing here may be published. That is an outcome,
    not an error."""
    only = tmp_path / "only.csv"
    only.write_text("a\n1\n")
    gate = Gate()
    gate.add(from_media_findings([_uninspected("only.csv")]))
    gate.resolve("uninspected:only.csv", ItemDecision.EXCLUDE_FROM_DEPOSIT,
                 human=researcher)

    with pytest.raises(NothingToDeposit) as exc:
        publication.deposit(_state(job_id), record, [only], gate=gate,
                            findings=[], human=researcher)
    assert exc.value.events[0].kind is EventKind.WORKFLOW_CLOSED_NOT_SHARED
    assert exc.value.decision.selected == "closed-not-shared"


def test_the_deposit_event_names_the_human(publication, record, payload,
                                           researcher, job_id):
    _, events, _ = publication.deposit(_state(job_id), record, payload,
                                       gate=Gate(), findings=[],
                                       human=researcher)
    assert events[0].kind is EventKind.DEPOSIT_COMPLETED
    assert events[0].human == researcher
    assert events[0].payload["pid"].startswith("10.5072/")


def test_working_material_is_not_released_without_an_identifier(publication,
                                                                job_id):
    """Deleting after a failed deposit leaves a job that cannot be resumed and a
    researcher whose data the tool consumed without publishing."""
    with pytest.raises(AuthorityError, match="not confirmed"):
        publication.release_working_material(
            _state(job_id), DepositReceipt(pid="", deposited_at="failed"))


def test_working_material_is_released_after_a_confirmed_publish(publication,
                                                                tmp_path, job_id):
    unpacked = tmp_path / "work" / job_id / "unpacked"
    unpacked.mkdir(parents=True)
    (unpacked / "obs.csv").write_text("a\n1\n")
    removed = publication.release_working_material(
        _state(job_id), DepositReceipt(pid="10.5072/zenodo.1", deposited_at="done"))
    assert removed == ["obs.csv"]
    assert not unpacked.exists()


def test_the_full_path_runs_offline(publication, driver, record, tmp_path,
                                    researcher, job_id, zenodo):
    """The cluster-4 definition of done, minus the live run: profile, gate,
    validate, deposit, release."""
    work = tmp_path / "work" / job_id / "unpacked"
    work.mkdir(parents=True)
    data = work / "observations.csv"
    data.write_text("station,date,temp\nS14,2019-03-02,4.1\n")

    findings, _ = ValidationAgent(ZenodoDataCiteProfile()).validate(
        record, profile_summary=PROFILE)
    assert not blocks_deposit(findings)

    gate = Gate()
    gate.add(from_media_findings([_uninspected("plate.png")]))
    assert gate.blocks_deposit()
    gate.resolve("uninspected:plate.png", ItemDecision.PUBLISH_AS_IS,
                 human=researcher, reason="opened it myself; instrument plot only")
    assert not gate.blocks_deposit()

    receipt, events, decision = publication.deposit(
        _state(job_id), record, [data], gate=gate, findings=findings,
        human=researcher)
    assert receipt.pid and receipt.concept_pid

    removed = publication.release_working_material(_state(job_id), receipt)
    assert removed == ["observations.csv"]
    assert not work.exists()
    assert "10.5072/" in decision.selected


# -- Divergences found by running against the real API --------------------

def test_zenodo_does_not_require_a_publisher():
    """Found in live testing: DataCite requires `publisher`, Zenodo assigns it.

    Inheriting the requirement made preflight demand a field no depositor
    supplies and the repository would ignore. A profile stricter than its target
    is not safe, merely obstructive: it refuses deposits that would succeed.
    """
    record = CanonicalRecord(title="T", creators=[Creator(name="A")],
                             publication_year=2026)
    assert "publisher" in DataCiteProfile().missing_required(record)
    assert ZenodoDataCiteProfile().missing_required(record) == []


def test_a_draft_accepts_incomplete_metadata_and_publish_refuses_it(driver, payload,
                                                                    researcher):
    """The second divergence: the real API validates on publish, not on update.

    The fake originally validated every write, which made it stricter than the
    API and left the driver's publish-time error handling untested.
    """
    incomplete = CanonicalRecord(title="No creators here")
    deposition_id = driver.begin("job-draft")
    driver.set_metadata(deposition_id, incomplete)          # accepted
    driver.upload(deposition_id, payload[0])
    with pytest.raises(ExternalServiceError, match="creators"):
        driver.publish(deposition_id, on_behalf_of=researcher)


def test_preflight_still_catches_it_before_the_repository_does(driver, payload):
    """Our own guard is what should refuse this, not a round trip to Zenodo."""
    incomplete = CanonicalRecord(title="No creators here")
    problems = driver.preflight(incomplete, payload,
                                profile=ZenodoDataCiteProfile())
    assert any("creators" in p for p in problems)


def test_the_fake_matches_the_shape_observed_from_the_real_api(driver, zenodo,
                                                               payload):
    """The fake is checked against a recorded response, not against memory.

    Every divergence found so far was invisible offline: the fake and the tests
    agreed with each other and both were wrong. Recording the real key set turns
    that agreement into something falsifiable, and stops a later edit to the fake
    drifting away from the API unnoticed.

    The fixture is updated only from an observed response.
    """
    import json as _j
    from pathlib import Path as _P

    shape = _j.loads(
        (_P(__file__).parent / "fixtures" / "zenodo" /
         "deposition-shape.json").read_text(encoding="utf-8"))

    deposition_id = driver.begin("job-shape")
    driver.upload(deposition_id, payload[0])
    draft = driver._deposition(deposition_id)

    missing = sorted(set(shape["top_level"]) - set(draft))
    assert not missing, f"the fake omits keys the real API returns: {missing}"

    missing_links = sorted(set(shape["links"]) - set(draft.get("links", {})))
    assert not missing_links, (
        f"the fake omits links the real API returns: {missing_links}")

    present = [k for k in shape["absent_when_unsubmitted"] if k in draft]
    assert not present, (
        f"the fake returns {present} on an unsubmitted draft; the real API omits "
        "them entirely, and a caller checking key presence rather than value "
        "would read that as a published record"
    )


def test_a_published_deposition_carries_both_identifiers(driver, record, payload,
                                                         researcher):
    """The complement: what a draft lacks, a published record must have."""
    driver.deposit("job-shape-2", record, payload, on_behalf_of=researcher)
    published = driver._deposition(driver._registry.get("job-shape-2"))
    assert published["doi"] and published["conceptdoi"]
    assert published["doi"] != published["conceptdoi"]


def test_a_retry_on_a_published_deposition_returns_without_replaying(
        driver, record, payload, researcher, zenodo):
    """Found live: the real API refuses metadata writes to a published record.

    The original retry replayed the whole sequence and failed at the first PUT,
    never reaching the publish step whose 409 handling it was meant to exercise.
    Idempotency here means "already done" returns the result; it does not repeat
    the work.
    """
    first = driver.deposit("job-retry", record, payload, on_behalf_of=researcher)
    writes_before = sum(1 for method, path in zenodo.state.requests
                        if method == "PUT")

    second = driver.deposit("job-retry", record, payload, on_behalf_of=researcher)
    writes_after = sum(1 for method, path in zenodo.state.requests
                       if method == "PUT")

    assert second.pid == first.pid
    assert second.concept_pid == first.concept_pid
    assert writes_after == writes_before, (
        "the retry wrote to a published deposition; the real API refuses that")


def test_writing_to_a_published_deposition_is_refused(driver, record, payload,
                                                      researcher):
    """The behaviour the fake now shares with the API."""
    driver.deposit("job-locked", record, payload, on_behalf_of=researcher)
    deposition_id = driver._registry.get("job-locked")
    with pytest.raises(ExternalServiceError, match="404"):
        driver.set_metadata(deposition_id, record)


def test_error_messages_carry_the_http_status(driver, record, payload,
                                              researcher):
    """A live failure once reported only 'Not found.', which cost a guess about
    whether the endpoint was wrong, the record missing, or the write refused."""
    driver.deposit("job-status", record, payload, on_behalf_of=researcher)
    deposition_id = driver._registry.get("job-status")
    with pytest.raises(ExternalServiceError) as exc:
        driver.set_metadata(deposition_id, record)
    assert "HTTP 404" in str(exc.value)


# ==========================================================================
# Crash recovery against the repository
# ==========================================================================

from datadirector_contracts import EffectKind, StepIntent  # noqa: E402

from datadirector.workflow.effects import (  # noqa: E402
    DID_NOT, INDETERMINATE, TOOK_EFFECT, ReconciliationRequired,
    idempotency_key, unfinished,
)


def test_the_driver_can_be_asked_whether_an_upload_took_effect(driver, record,
                                                               payload,
                                                               researcher):
    """A component performing an external effect that offers no way to check it
    leaves recovery with nothing but a guess."""
    deposition_id = driver.begin("job-probe")
    driver.upload(deposition_id, payload[0])
    probers = driver.probers()

    present = StepIntent(step="upload", effect=EffectKind.REPOSITORY_UPLOAD,
                         target="zenodo", idempotency_key="k1",
                         detail={"deposition_id": deposition_id,
                                 "artefact": payload[0].name})
    assert probers[EffectKind.REPOSITORY_UPLOAD](present)[0] == TOOK_EFFECT

    absent = present.model_copy(update={
        "detail": {"deposition_id": deposition_id, "artefact": "never-sent.csv"}})
    assert probers[EffectKind.REPOSITORY_UPLOAD](absent)[0] == DID_NOT


def test_the_driver_can_be_asked_whether_a_publish_took_effect(driver, record,
                                                               payload,
                                                               researcher):
    """The question that matters: retrying a publish nobody checked is how a
    dataset gets two DOIs."""
    probers = driver.probers()
    deposition_id = driver.begin("job-pub-probe")
    intent = StepIntent(step="deposit", effect=EffectKind.REPOSITORY_PUBLISH,
                        target="zenodo", idempotency_key="k2",
                        detail={"deposition_id": deposition_id})
    assert probers[EffectKind.REPOSITORY_PUBLISH](intent)[0] == DID_NOT

    driver.deposit("job-pub-probe", record, payload, on_behalf_of=researcher)
    finding, detail = probers[EffectKind.REPOSITORY_PUBLISH](intent)
    assert finding == TOOK_EFFECT
    assert "do not publish again" in detail


def test_an_unreachable_repository_is_indeterminate(tmp_path, researcher):
    """Not 'did not happen'. The service was not asked; nothing follows."""
    broker = CredentialBroker({"zenodo:deposit": "DD_ZENODO_TOKEN"},
                              environ={"DD_ZENODO_TOKEN": VALID_TOKEN})
    driver = ZenodoDriver(broker, DepositionRegistry(tmp_path / "d.json"),
                          base_url="http://127.0.0.1:9", timeout=0.3)
    intent = StepIntent(step="deposit", effect=EffectKind.REPOSITORY_PUBLISH,
                        target="zenodo", idempotency_key="k3",
                        detail={"deposition_id": 1})
    assert driver.probers()[EffectKind.REPOSITORY_PUBLISH](intent)[0] \
        == INDETERMINATE


def test_publishing_refuses_while_an_earlier_attempt_is_unsettled(
        driver, record, payload, researcher, tmp_path, job_id):
    """Publishing over an interrupted attempt is how a dataset is deposited
    twice."""
    from datadirector_contracts import Event, EventKind
    from datadirector.agents.publication import PublicationAgent
    from datadirector.gate.items import Gate
    from datadirector.state.projection import JobState
    from datadirector.state.store import EventStore

    store = EventStore(tmp_path / "state")
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.WORKFLOW_CREATED, agent="test/1.0"))
    intent = StepIntent(step="deposit", effect=EffectKind.REPOSITORY_PUBLISH,
                        target="zenodo",
                        idempotency_key=idempotency_key(job_id, "deposit",
                                                        "zenodo"),
                        detail={"job_id": job_id})
    store.append(Event(sequence=1, job_id=job_id, kind=EventKind.STEP_STARTED,
                       agent="test/1.0", payload=intent.model_dump(mode="json")))

    agent = PublicationAgent(driver, tmp_path / "work", store=store)
    with pytest.raises(ReconciliationRequired, match="interrupted"):
        agent.deposit(JobState(job_id=job_id, step="deposit"), record, payload,
                      gate=Gate(), findings=[], human=researcher)


def test_a_completed_deposit_records_its_intent_and_outcome(driver, record,
                                                            payload, researcher,
                                                            tmp_path, job_id):
    from datadirector_contracts import Event, EventKind
    from datadirector.agents.publication import PublicationAgent
    from datadirector.gate.items import Gate
    from datadirector.state.projection import JobState
    from datadirector.state.store import EventStore

    store = EventStore(tmp_path / "state")
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.WORKFLOW_CREATED, agent="test/1.0"))
    agent = PublicationAgent(driver, tmp_path / "work", store=store)
    agent.deposit(JobState(job_id=job_id, step="deposit"), record, payload,
                  gate=Gate(), findings=[], human=researcher)

    kinds = [e.kind for e in store.load(job_id)]
    assert EventKind.STEP_STARTED in kinds and EventKind.STEP_COMPLETED in kinds
    assert unfinished(store, job_id) == []


# ==========================================================================
# What the depositor said, and what the drafting agents are given
# ==========================================================================

from datadirector_contracts import Event, EventKind   # noqa: E402
from datadirector.agents.base import Invocation   # noqa: E402
from datadirector.agents.documentation import (   # noqa: E402
    README_NAME, Documentation, DocumentationAgent, ReadmeSection,
    descriptions_for_record,
)
from datadirector.job_handle import (   # noqa: E402
    JobHandle, depositor_context, latest_documentation,
    latest_draft_was_a_persons, recorded_profile,
)
from datadirector.profiling.structural import profile_tree   # noqa: E402
from datadirector.state.store import EventStore   # noqa: E402

DEPOSITOR_SAYS = ("Coastal water temperature was logged by hand, hourly, at "
                   "station 14 between 2019 and 2023.")


def _log_event(job_id, kind, payload=None, agent="test/1.0"):
    return Event(sequence=1, job_id=job_id, kind=kind, agent=agent,
                 payload=payload or {})


def test_the_depositor_s_own_account_is_read_from_the_log(job_id):
    """The statement, the claims and the plan are the depositor's account of
    the material. What they chose on a settings screen is not, and must not
    reach a drafting prompt as if it were."""
    events = [
         _log_event(job_id, EventKind.INSTRUCTIONS_RECEIVED,
                    {"channel": "responsibility-and-compliance-statement",
                     "statement": DEPOSITOR_SAYS}),
         _log_event(job_id, EventKind.DECLARATION_PARSED,
                    {"claims": [{"term": "hourly", "value": "by hand"}]}),
         _log_event(job_id, EventKind.INSTRUCTIONS_RECEIVED,
                    {"channel": "backend-preference",
                     "statement": "use the largest model available"}),
     ]
    context = depositor_context(events)
    assert context["statement"] == DEPOSITOR_SAYS
    assert context["claims"]
    assert "largest model" not in str(context)


def test_the_depositor_s_words_reach_both_drafting_prompts():
    """A field the depositor described arriving blank is not a model failure.
    It is the context never reaching the prompt that asks for that field."""
    model = Scripted(DRAFT_REPLY)
    MetadataAgent(_pep(model), FakeVocabulary({})).draft(
        JobState(job_id="job-x", step="metadata"), profile_summary=PROFILE,
        stated={"statement": DEPOSITOR_SAYS})
    assert DEPOSITOR_SAYS in model.seen[0].user_content
    assert "stated_by_depositor" in model.seen[0].user_content

    documented = Scripted(DOC_REPLY)
    DocumentationAgent(_pep(documented)).draft(
        JobState(job_id="job-x", step="metadata"), profile_summary=PROFILE,
        record_summary={}, stated={"statement": DEPOSITOR_SAYS})
    assert DEPOSITOR_SAYS in documented.seen[0].user_content
    assert "metadata" in documented.seen[0].user_content


def test_a_field_nothing_was_said_about_is_left_blank_and_named():
    """A blank prompts a question, and the undetermined list is what turns the
    blank on the screen into a question the depositor can answer."""
    reply = _json.dumps({
         "title": "Surface temperature series, station 14",
         "abstract": None, "methods": None, "keywords": [],
         "resource_type": "Dataset", "language": "en",
         "uncertain": ["abstract: nothing states what the study was for",
                       "methods: nothing states how the data were taken"],
     })
    record, decision = MetadataAgent(_pep(Scripted(reply)),
                                     FakeVocabulary({})).draft(
        JobState(job_id="job-x", step="metadata"), profile_summary=PROFILE)
    kinds = {description.kind for description in record.descriptions}
    assert DescriptionKind.ABSTRACT not in kinds
    assert DescriptionKind.METHODS not in kinds
    assert any("abstract" in u for u in decision.undetermined)



def _readme(*pairs):
    return Documentation(
        readme="\n\n".join("## %s\n\n%s" % pair for pair in pairs),
        sections=[ReadmeSection(heading=heading, body=body)
                  for heading, body in pairs])


def test_the_readme_fills_the_fields_it_actually_covers():
    documentation = _readme(
        ("Overview", "One CSV of hourly coastal readings from station 14."),
        ("Collection-and-methods", "Logged by hand, hourly, 2019 to 2023."))
    bare = CanonicalRecord(title="Station 14 readings",
                           creators=[Creator(name="Aroa, Miriam")])
    merged, filled = descriptions_for_record(documentation, bare,
                                             agent="documentation/0.1.0")
    assert sorted(filled) == ["Abstract", "Methods"]
    assert {d.kind for d in merged.descriptions} == {
        DescriptionKind.ABSTRACT, DescriptionKind.METHODS}
    assert any(d.text.startswith("One CSV") for d in merged.descriptions)
    assert merged.origin_of("descriptions").origin is FieldOrigin.INFERRED


def test_no_prose_is_improvised_for_a_field_the_readme_does_not_cover():
    documentation = _readme(("Data dictionary", "temp is degrees Celsius."))
    bare = CanonicalRecord(title="Station 14 readings")
    assert descriptions_for_record(documentation, bare,
                                   agent="documentation/0.1.0") == (None, [])


def test_a_value_the_researcher_typed_is_not_replaced_by_the_readme():
    documentation = _readme(("Methods", "Drafted by the model instead."),
                            ("Overview", "A drafted overview."))
    theirs = CanonicalRecord(
        title="Station 14 readings",
        descriptions=[Description(text="Collected by the station staff.",
                                  kind=DescriptionKind.METHODS)])
    merged, filled = descriptions_for_record(documentation, theirs,
                                             agent="documentation/0.1.0")
    assert filled == ["Abstract"]
    texts = [d.text for d in merged.descriptions]
    assert "Collected by the station staff." in texts
    assert "Drafted by the model instead." not in texts


def test_a_record_a_person_last_wrote_is_left_to_them():
    documentation = _readme(("Overview", "An improvement on their words."))
    assert descriptions_for_record(documentation,
                                   CanonicalRecord(title="Their own title"),
                                   agent="documentation/0.1.0",
                                   allow_merge=False) == (None, [])


def test_what_the_documentation_agent_drafts_stays_on_the_log(tmp_path,
                                                             job_id):
    """What used to happen: the README was drafted into `Outcome.result`, a
    caller outside the system could read it, nothing inside it did, and the
    deposit went out undocumented. Drafting that nothing can read back has
    not been done."""
    store = EventStore(tmp_path / "state")
    store.append(_log_event(job_id, EventKind.WORKFLOW_CREATED))
    work = tmp_path / "work"
    unpacked = work / job_id / "unpacked"
    unpacked.mkdir(parents=True)
    (unpacked / "obs.csv").write_text("station,date,temp\nS14,2019,4.1\n")
    record = CanonicalRecord(title="Station 14 readings",
                             creators=[Creator(name="Aroa, Miriam")])
    store.append(_log_event(job_id, EventKind.METADATA_DRAFTED,
                            {"record": _json.loads(record.model_dump_json())},
                            agent="metadata/0.1.0"))

    handle = JobHandle(job_id, store=store, working_root=work,
                       inspects_material=True)
    outcome = DocumentationAgent(_pep(Scripted(DOC_REPLY))).run(
        Invocation(job=handle))
    for event in outcome.events:
        store.append(event)

    assert (work / job_id / README_NAME).read_text().startswith("## Overview")
    assert outcome.artefacts == [README_NAME]
    logged = latest_documentation(store.load(job_id))
    assert [section["heading"] for section in logged["sections"]] == ["Overview"]
    drafts = [event for event in store.load(job_id)
              if event.kind is EventKind.METADATA_DRAFTED]
    assert len(drafts) == 2, "a fill is a new draft, not an edit in place"
    filled = CanonicalRecord.model_validate(drafts[-1].payload["record"])
    assert any(d.kind is DescriptionKind.ABSTRACT for d in filled.descriptions)
    assert drafts[-1].payload["filled_by"] == "documentation/0.1.0"




def test_a_machine_does_not_re_draft_over_the_person_s_own_words(job_id,
                                                                 researcher):
    """The newest draft is what the review screen reads. Re-drafting a record
    a person last wrote would therefore push their own wording out of view
    because someone pressed a button."""
    assert latest_draft_was_a_persons([]) is False
    drafted = [_log_event(job_id, EventKind.METADATA_DRAFTED,
                           {"record": None}, agent="metadata/0.1.0")]
    assert latest_draft_was_a_persons(drafted) is False
    drafted.append(Event(sequence=1, job_id=job_id,
                          kind=EventKind.METADATA_DRAFTED,
                          agent="metadata/0.1.0", human=researcher,
                          payload={"record": None}))
    assert latest_draft_was_a_persons(drafted) is True

def test_the_profile_the_agents_draft_from_is_the_one_ingestion_measured(
        job_id, tmp_path):
    root = tmp_path / "unpacked"
    root.mkdir()
    (root / "obs.csv").write_text("station,date,temp\nS14,2019,4.1\n")
    tree = profile_tree(root)
    events = [_log_event(job_id, EventKind.MATERIAL_REGISTERED,
                         {"profile": tree.model_dump(mode="json")},
                         agent="ingestion/0.1.0")]
    assert recorded_profile(events) == tree.compact()
    assert recorded_profile([_log_event(job_id,
                                        EventKind.WORKFLOW_CREATED)]) == {}


def test_a_record_with_nothing_to_read_about_it_is_a_finding(record):
    """Neither target requires a description, so an undescribed dataset used
    to pass schema validation, clear the gate, and leave with a blank page
    where a stranger would have read what the data are."""
    undescribed = record.model_copy(update={"descriptions": []})
    findings, _ = ValidationAgent(DataCiteProfile()).validate(undescribed)
    described = [f for f in findings if f.rule == "record-described"]
    assert any("no description" in f.message for f in described)
    assert any("methods" in f.message for f in described)
    assert blocks_deposit(described) is False

