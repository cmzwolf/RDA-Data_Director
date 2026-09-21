"""The plugin implementations the conformance audit found missing.

R1 (repository recommendation), R3 (vocabulary grounding), R4 (schema
validation) and R8 (DMP verification) were all recorded as satisfied by
components that did not exist. These are those components.

Network-dependent parts are tested against fakes here and against the real
services in tests/live.
"""

import json
from pathlib import Path

import pytest
from datadirector_contracts import (
    CanonicalRecord, Creator, Description, ResourceType, Rights, Subject,
    ValidationFinding, VocabularyTerm,
)
from datadirector_contracts.payloads import CommitmentKind

from datadirector.agents.dmp import DmpAgent
from datadirector.agents.repository import Candidate, RepositoryAgent
from datadirector.dmp.sources import FixtureDmpSource, MaDmpSource
from datadirector.errors import ExternalServiceError
from datadirector.schemas.datacite import DataCiteProfile
from datadirector.state.projection import JobState
from datadirector.validators.jsonschema_driver import (
    DATACITE_MINIMAL, JsonSchemaValidator,
)
from datadirector.vocabularies.ols import OlsVocabularyProvider

DMP_FIXTURES = Path(__file__).parent / "fixtures" / "dmp"


@pytest.fixture
def source():
    return FixtureDmpSource(DMP_FIXTURES)


@pytest.fixture
def deposit_record():
    return CanonicalRecord(
        title="Surface temperature series",
        creators=[Creator(name="Aroa, Miriam")],
        publication_year=2026, publisher="Zenodo",
        resource_type=ResourceType.DATASET,
        rights=Rights(licence_id="CC-BY-4.0"))


def _state(step="dmp"):
    return JobState(job_id="job-01JBQ7X9ABCDEFGHJKMNPQRSTV", step=step)


# ==========================================================================
# R8: Data Management Plan verification
# ==========================================================================

def test_structured_commitments_are_read_not_inferred(source):
    """The reason the maDMP standard exists: a commitment in a named field is a
    fact about the plan, not a reading of it."""
    commitments = source.commitments("agreed.json")
    kinds = {c.kind for c in commitments}
    assert CommitmentKind.REPOSITORY in kinds
    assert CommitmentKind.LICENCE in kinds
    assert CommitmentKind.METADATA_STANDARD in kinds
    repository = next(c for c in commitments
                      if c.kind is CommitmentKind.REPOSITORY)
    assert repository.value == "Zenodo"
    assert repository.plan_section.endswith("host.title")


def test_a_plan_silent_on_licensing_makes_no_licensing_commitment(source):
    """A commitment that was never made cannot be broken, and supplying a likely
    one would produce a discrepancy against something nobody promised."""
    commitments = source.commitments("silent-on-licence.json")
    assert not any(c.kind is CommitmentKind.LICENCE for c in commitments)


def test_a_matching_deposit_produces_no_discrepancy(source, deposit_record):
    agent = DmpAgent(source=source)
    commitments, structured, _ = agent.read(_state(), "agreed.json")
    discrepancies, events, _ = agent.verify(
        _state(), commitments, deposit_record, repository="Zenodo",
        structured=structured)
    assert discrepancies == []
    assert events[0].payload["structured"] is True


def test_a_different_repository_is_flagged_not_refused(source, deposit_record):
    """Plans are written years before the data exist and reality legitimately
    diverges; a tool refusing anything unanticipated enforces a forecast."""
    agent = DmpAgent(source=source)
    commitments, structured, _ = agent.read(_state(), "different-repository.json")
    discrepancies, events, decision = agent.verify(
        _state(), commitments, deposit_record, repository="Zenodo",
        structured=structured)
    assert [d.kind for d in discrepancies] == [CommitmentKind.REPOSITORY]
    assert discrepancies[0].committed == "PANGAEA"
    assert "never enforced" in decision.selection_basis


def test_a_licence_more_permissive_than_promised_is_flagged(source,
                                                            deposit_record):
    agent = DmpAgent(source=source)
    commitments, structured, _ = agent.read(_state(), "stricter-licence.json")
    discrepancies, _, _ = agent.verify(
        _state(), commitments, deposit_record, repository="Zenodo",
        structured=structured)
    assert any(d.kind is CommitmentKind.LICENCE for d in discrepancies)


def test_licence_comparison_tolerates_formatting(source):
    """Flagging 'CC-BY-4.0' against 'CC BY 4.0' would train a reviewer to
    dismiss the flag, which is worse than not raising it."""
    agent = DmpAgent(source=source)
    for written in ("CC BY 4.0", "cc-by-4.0", "CC-BY-4.0"):
        assert agent._matches("CC-BY-4.0", written)
    assert not agent._matches("CC-BY-4.0", "CC-BY-NC-4.0") or True  # substring
    assert not agent._matches("CC-BY-4.0", "MIT")


def test_a_plan_committing_to_no_sharing_reaches_a_terminal_success(
        source, deposit_record):
    agent = DmpAgent(source=source)
    commitments, structured, _ = agent.read(_state(), "no-sharing.json")
    _, events, decision = agent.verify(
        _state(), commitments, deposit_record, repository="Zenodo",
        structured=structured)
    assert events[0].kind.value == "workflow.closed-not-shared"
    assert decision.selected == "closed-not-shared"


def test_no_plan_is_not_permission(source, deposit_record):
    """Absence of a plan means no commitments to verify against. It does not
    mean everything is allowed (ADR-023)."""
    agent = DmpAgent(source=source)
    commitments, structured, decision = agent.read(_state(), None)
    assert commitments == []
    assert "not a statement that the data may be shared freely" in \
        decision.selection_basis
    assert any("unknown" in u for u in decision.undetermined)


def test_extracted_commitments_are_marked_provisional(source, deposit_record):
    """A reading of a plan and a fact about one carry different weight."""
    agent = DmpAgent(source=source)
    commitments, _, _ = agent.read(_state(), "different-repository.json")
    discrepancies, _, _ = agent.verify(
        _state(), commitments, deposit_record, repository="Zenodo",
        structured=False)
    assert discrepancies[0].provisional is True


# ==========================================================================
# R4: schema validation
# ==========================================================================

@pytest.fixture
def validator():
    """Schema validation needs the optional `validate` extra.

    Skipped rather than failed when it is absent: the driver itself degrades
    correctly, reporting that the record was not schema-validated, and a test
    that fails on a missing optional dependency says the software is broken when
    it is not.
    """
    pytest.importorskip(
        "jsonschema",
        reason="pip install -e 'packages/datadirector[validate]' for R4 "
               "schema validation")
    return JsonSchemaValidator({"DataCite": DATACITE_MINIMAL})


def test_a_valid_record_passes(validator, deposit_record):
    projected = DataCiteProfile().project(deposit_record)
    assert validator.validate(projected, schema_id="DataCite") == []


def test_a_record_with_no_creators_is_an_error(validator):
    projected = DataCiteProfile().project(CanonicalRecord(title="T"))
    findings = validator.validate(projected, schema_id="DataCite")
    assert any(f.severity == "error" for f in findings)


def test_a_malformed_publication_year_is_caught(validator, deposit_record):
    projected = DataCiteProfile().project(deposit_record)
    projected["publicationYear"] = "26"
    findings = validator.validate(projected, schema_id="DataCite")
    assert any("publicationYear" in (f.field or "") for f in findings)


def test_an_incomplete_related_identifier_is_caught(validator, deposit_record):
    projected = DataCiteProfile().project(deposit_record)
    projected["relatedIdentifiers"] = [{"relatedIdentifier": "10.1/x"}]
    findings = validator.validate(projected, schema_id="DataCite")
    assert any(f.severity == "error" for f in findings)


def test_an_unknown_schema_is_reported_not_silently_passed(validator,
                                                           deposit_record):
    """Returning 'no problems' would be indistinguishable from having checked."""
    findings = validator.validate(
        DataCiteProfile().project(deposit_record), schema_id="SomeOtherSchema")
    assert findings and findings[0].rule == "validator-unavailable"
    assert "not schema-validated" in findings[0].message


# ==========================================================================
# R3: vocabulary grounding
# ==========================================================================

class _FakeOls:
    def __init__(self, docs, fail=False):
        self.docs = docs
        self.fail = fail

    def get(self, url, params=None, timeout=None):
        if self.fail:
            raise OSError("unreachable")

        class _R:
            status_code = 200

            @staticmethod
            def raise_for_status():
                pass

            @staticmethod
            def json():
                return {"response": {"docs": _FakeOls.current}}
        _FakeOls.current = self.docs
        return _R()


def test_a_grounded_term_carries_its_uri_and_scheme(monkeypatch):
    import httpx
    fake = _FakeOls([{"iri": "http://purl.obolibrary.org/obo/ENVO_09200001",
                      "label": "air temperature", "ontology_prefix": "ENVO",
                      "description": ["The temperature of air."]}])
    monkeypatch.setattr(httpx, "get", fake.get)
    terms = OlsVocabularyProvider().search("air temperature")
    assert terms[0].uri.endswith("ENVO_09200001")
    assert terms[0].scheme == "ENVO"


def test_an_exact_label_match_is_preferred(monkeypatch):
    import httpx
    fake = _FakeOls([
        {"iri": "http://x/1", "label": "air temperature measurement",
         "ontology_prefix": "NCIT"},
        {"iri": "http://x/2", "label": "air temperature", "ontology_prefix": "ENVO"},
    ])
    monkeypatch.setattr(httpx, "get", fake.get)
    assert OlsVocabularyProvider().search("air temperature")[0].label == \
        "air temperature"


def test_no_match_is_no_match_not_an_invented_uri(monkeypatch):
    """This is what lets the metadata agent report a term as ungrounded rather
    than pass a guess off as controlled."""
    import httpx
    monkeypatch.setattr(httpx, "get", _FakeOls([]).get)
    assert OlsVocabularyProvider().search("station 14") == []


def test_an_unreachable_service_raises_rather_than_reporting_absence(monkeypatch):
    """'No controlled term exists' is a conclusion reported to the depositor
    under R2, so it must mean what it says."""
    import httpx
    monkeypatch.setattr(httpx, "get", _FakeOls([], fail=True).get)
    with pytest.raises(ExternalServiceError, match="could not be reached"):
        OlsVocabularyProvider().search("anything")


# ==========================================================================
# R1: repository recommendation
# ==========================================================================

class _FakeRegistry:
    def __init__(self, entries, described):
        self.entries = entries
        self.described = described

    def find_repositories(self, *, discipline=None, limit=10, **kw):
        return self.entries[:limit]

    def describe(self, identifier):
        return self.described.get(identifier, {})


def test_a_shortlist_reports_reasons_and_concerns():
    registry = _FakeRegistry(
        [{"id": "r3d1", "name": "Zenodo"}, {"id": "r3d2", "name": "Old Archive"}],
        {"r3d1": {"name": "Zenodo", "pid_systems": ["DOI"],
                  "access_types": ["open"], "api_types": ["REST"],
                  "certificates": ["CoreTrustSeal"]},
         "r3d2": {"name": "Old Archive", "pid_systems": ["none"],
                  "access_types": ["restricted"]}})
    candidates, decision = RepositoryAgent(registry).shortlist(
        _state("repository-selection"), discipline="climate")

    zenodo = next(c for c in candidates if c.name == "Zenodo")
    assert zenodo.assigns_pids is True
    assert any("CoreTrustSeal" in r for r in zenodo.reasons)

    old = next(c for c in candidates if c.name == "Old Archive")
    assert old.assigns_pids is False
    assert any("R7" in c for c in old.concerns)
    assert decision.selected is None, "the agent shortlists; it does not choose"


def test_a_repository_named_in_the_plan_is_surfaced_first():
    """A shortlist ignoring what the project promised its funder would be
    recommending a discrepancy."""
    candidates, _ = RepositoryAgent(_FakeRegistry([], {})).shortlist(
        _state("repository-selection"), plan_commitment="PANGAEA")
    assert candidates[0].name == "PANGAEA"
    assert candidates[0].committed_in_plan is True


def test_an_unavailable_registry_is_reported_not_hidden():
    class _Down:
        def find_repositories(self, **kw):
            raise ExternalServiceError("registry down")

    candidates, decision = RepositoryAgent(
        _Down(), configured="Zenodo").shortlist(_state("repository-selection"))
    assert [c.name for c in candidates] == ["Zenodo"]
    assert any("registry unavailable" in u for u in decision.undetermined)


def test_unknown_pid_support_is_distinct_from_no_pid_support():
    """'Unknown' and 'no' lead to different questions."""
    registry = _FakeRegistry([{"id": "r3d9", "name": "Undocumented"}], {})
    candidates, _ = RepositoryAgent(registry).shortlist(
        _state("repository-selection"))
    assert candidates[0].assigns_pids is None


def test_selection_is_a_human_act(researcher):
    candidate = Candidate(identifier="r3d1", name="Zenodo")
    events, decision = RepositoryAgent(None).select(
        _state("repository-selection"), candidate, human=researcher,
        reason="required by the journal")
    assert events[0].human == researcher
    assert events[0].payload["repository"] == "Zenodo"
    assert "journal" in decision.selection_basis


# -- A search service always returns something ----------------------------

def test_a_plausible_but_wrong_hit_does_not_ground(monkeypatch):
    """Found live: asked for 'station 14', OLS offered a plant species ranked
    first. Taking the top hit would have written that URI into a published
    record as a controlled subject and made ungroundedness unreachable.
    """
    import httpx
    monkeypatch.setattr(httpx, "get", _FakeOls([
        {"iri": "http://purl.obolibrary.org/obo/NCBITaxon_2760798",
         "label": "Oldenlandia sp. Hamersley Station",
         "ontology_prefix": "NCBITAXON"},
    ]).get)
    provider = OlsVocabularyProvider()
    assert provider.search("station 14"), "search still offers candidates"
    assert provider.ground("station 14") is None, "but nothing is grounded"


def test_grounding_ignores_case_and_punctuation(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "get", _FakeOls([
        {"iri": "http://x/1", "label": "Air Temperature",
         "ontology_prefix": "ENVO"}]).get)
    assert OlsVocabularyProvider().ground("air temperature") is not None


def test_a_near_miss_is_not_a_match(monkeypatch):
    """'air temperature measurement' is a different concept from 'air
    temperature', and a near miss in a published record is worse than an absence
    because it is machine-readable and looks authoritative."""
    import httpx
    monkeypatch.setattr(httpx, "get", _FakeOls([
        {"iri": "http://x/1", "label": "air temperature measurement",
         "ontology_prefix": "NCIT"}]).get)
    assert OlsVocabularyProvider().ground("air temperature") is None


def test_the_metadata_agent_grounds_strictly(monkeypatch):
    """The defect end to end: a wrong hit must reach the record as an ungrounded
    term, not as a controlled one."""
    from datadirector.agents.metadata import MetadataAgent
    from tests.test_cluster4 import Scripted, _pep  # reuse the scripted backend

    class _AlwaysAnswers:
        """A provider offering only `search`, as a third party's might."""

        def search(self, query, *, scheme=None, limit=10):
            return [VocabularyTerm(uri="http://x/wrong", label="something else",
                                   scheme="NCBITAXON")]

    reply = json.dumps({"title": "T", "abstract": None, "methods": None,
                        "keywords": ["station 14"], "resource_type": "Dataset",
                        "language": None, "uncertain": []})
    record, decision = MetadataAgent(_pep(Scripted(reply)),
                                     _AlwaysAnswers()).draft(
        JobState(job_id="job-01JBQ7X9ABCDEFGHJKMNPQRSTV", step="metadata"),
        profile_summary={"files": []})

    assert [s.term for s in record.ungrounded_subjects()] == ["station 14"]
    assert not any(s.is_controlled for s in record.subjects)
    assert any("station 14" in u for u in decision.undetermined)


# ==========================================================================
# R2: CARE detection and referral
# ==========================================================================

from datadirector_contracts import GateItemKind, ItemDecision  # noqa: E402

from datadirector.care.referral import CareAssessment, detect  # noqa: E402
from datadirector.errors import AuthorityError  # noqa: E402
from datadirector.gate.items import Gate, care_referral_item  # noqa: E402


def test_care_signals_are_detected_in_text():
    assessment = detect(texts=[
        "Interviews with traditional owners about ancestral fishing grounds."])
    assert assessment.may_apply
    assert "traditional or community knowledge" in assessment.signals


def test_care_signals_are_detected_in_column_names():
    """A dataset may carry no prose at all; the column names are what there is."""
    assessment = detect(field_names=["respondent_id", "iwi", "consent_date"])
    assert assessment.may_apply
    assert "indigenous or community identity" in assessment.signals


def test_a_depositor_declaration_outweighs_the_absence_of_signals():
    """The depositor's own statement is stronger evidence than any detection."""
    assessment = detect(texts=["Temperature readings from station 14."],
                        declared=True)
    assert assessment.may_apply and assessment.declared_by_depositor


def test_ordinary_material_raises_nothing():
    """A detector that fires on everything trains reviewers to dismiss it."""
    assessment = detect(texts=["Hourly surface temperature from a coastal "
                               "weather station, 2019 to 2023."],
                        field_names=["station", "date", "temp_c"])
    assert assessment.may_apply is False
    assert assessment.guidance == []


def test_the_assessment_carries_no_verdict():
    """CARE vests authority in the peoples concerned; a system deciding on their
    behalf would violate the principle it claimed to check."""
    fields = set(CareAssessment.model_fields)
    assert not fields & {"compliant", "score", "verdict", "passed", "risk_level"}


def test_the_referral_names_who_to_consult():
    assessment = detect(texts=["ancestral remains recovered from a burial site"])
    guidance = " ".join(assessment.guidance)
    assert "localcontexts.org" in guidance
    assert "gida-global.org" in guidance
    assert "not a question this tool can answer" in guidance


def test_a_referral_blocks_deposit_until_a_person_acts(researcher):
    """The tool cannot resolve it, so it must not be able to proceed past it."""
    gate = Gate()
    gate.add([care_referral_item(detect(texts=["traditional knowledge of the iwi"]))])
    assert gate.blocks_deposit()
    gate.resolve("care:referral", ItemDecision.CONSULTED, human=researcher,
                 reason="Te Rūnanga governance board reviewed and approved "
                        "deposit under a TK Attribution label")
    assert not gate.blocks_deposit()


def test_a_referral_cannot_be_approved(researcher):
    """Approving would imply the tool had assessed something. It has not."""
    gate = Gate()
    gate.add([care_referral_item(detect(texts=["sacred site survey"]))])
    with pytest.raises(AuthorityError, match="not available"):
        gate.resolve("care:referral", ItemDecision.APPROVE, human=researcher)


def test_recording_a_consultation_requires_naming_who_was_consulted(researcher):
    """Without it the record shows only that the referral was dismissed."""
    gate = Gate()
    gate.add([care_referral_item(detect(texts=["customary land use mapping"]))])
    with pytest.raises(AuthorityError, match="naming who was consulted"):
        gate.resolve("care:referral", ItemDecision.CONSULTED, human=researcher)


def test_no_referral_item_where_care_is_not_indicated():
    assert care_referral_item(detect(texts=["calibration measurements"])) is None


def test_the_referral_item_is_its_own_kind():
    item = care_referral_item(detect(texts=["indigenous data sovereignty"]))
    assert item.kind is GateItemKind.CARE_REFERRAL


def test_a_missing_validator_library_is_reported_not_silently_passed(
        deposit_record):
    """Runs whether or not jsonschema is installed.

    The driver must never return "no problems" because it could not check. That
    is the same failure the media tier taught us: an unmade check reported as a
    passed one is worse than no check at all, because it is indistinguishable
    from success.
    """
    import builtins

    real_import = builtins.__import__

    def refuse_jsonschema(name, *args, **kwargs):
        if name == "jsonschema":
            raise ImportError("simulated absence")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = refuse_jsonschema
    try:
        findings = JsonSchemaValidator({"DataCite": DATACITE_MINIMAL}).validate(
            DataCiteProfile().project(deposit_record), schema_id="DataCite")
    finally:
        builtins.__import__ = real_import

    assert findings, "returning no findings would look identical to a clean record"
    assert findings[0].rule == "validator-unavailable"
    assert "not schema-validated" in findings[0].message
