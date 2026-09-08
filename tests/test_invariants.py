"""Executable acceptance criteria for the architecture's safety properties.

Each test corresponds to a claim made in the architecture document. If a test
fails, either the code is wrong or the document is. These are the tests that
must stay green through every later refactoring; they are the reason the
contracts exist as code rather than as prose.

Test subjects arrive as fixtures (see conftest.py), so each test declares in
its own signature which identities it depends on.
"""

import pytest

from datadirector_contracts import (
    Assertion, AssertionSet, AuthorityState, Channel, Classification,
    DeclarationClaim, Event, EventKind, Orcid, PolicyConfig, ProvActivity,
    ExtractionLimits, RelatedResource, RelationOrigin, RelationVocabulary,
    SensitivityClass, verify_chain,
)
from datadirector_contracts.policy import NoBackendAction
from datadirector_contracts.primitives import Digest, orcid_check_digit
from datadirector_contracts.provenance import ProvAgent, Visibility


# -- Identifier integrity --------------------------------------------------

def test_orcid_checksum_is_validated():
    """A typo in an ORCID silently misattributes a human act, so it is caught
    at construction rather than at the first external call."""
    with pytest.raises(ValueError, match="checksum failure"):
        Orcid(value="0000-0002-1825-0098")


def test_published_fictitious_orcid_is_valid(researcher):
    assert orcid_check_digit(researcher.value[:-1]) == researcher.value[-1]


# -- Section 9.3: the tightening asymmetry ---------------------------------

def test_automated_component_may_tighten_classification():
    c = Classification(level=SensitivityClass.PUBLIC)
    c2 = c.tighten(SensitivityClass.SENSITIVE, agent="classifier/1.0",
                   rationale="indirect identifier in free-text column")
    assert c2.level is SensitivityClass.SENSITIVE


def test_automated_component_may_not_loosen_classification():
    """The single most important invariant in the system."""
    c = Classification(level=SensitivityClass.SENSITIVE)
    with pytest.raises(ValueError, match="may only raise"):
        c.tighten(SensitivityClass.PUBLIC, agent="classifier/1.0", rationale="looks fine")


def test_loosening_requires_a_named_human(researcher):
    c = Classification(level=SensitivityClass.SENSITIVE)
    c2 = c.relax_with_human_authority(
        SensitivityClass.PUBLIC, human=researcher,
        rationale="reviewed; no personal data present",
    )
    assert c2.level is SensitivityClass.PUBLIC
    assert c2.established_by == researcher


# -- Section 8.5 / ADR-024: authority derives from the channel -------------

def _proposed(channel: Channel) -> AssertionSet[DeclarationClaim]:
    return AssertionSet[DeclarationClaim](
        channel=channel,
        state=AuthorityState.PROPOSED,
        assertions=[Assertion[DeclarationClaim](
            payload=DeclarationClaim(asserted_sensitivity=SensitivityClass.PUBLIC),
            confidence=0.9)],
    )


def test_unconfirmed_assertions_carry_no_authority():
    with pytest.raises(PermissionError, match="carries no authority"):
        _proposed(Channel.PARSED_DOCUMENT).effective_payloads()


def test_confirmation_by_a_human_grants_authority(researcher):
    s = _proposed(Channel.PARSED_DOCUMENT).confirm(human=researcher)
    assert s.confirmed_by == researcher
    assert len(s.effective_payloads()) == 1


def test_watched_folder_cannot_yield_authenticated_authority(researcher):
    """A dropped instructions.txt is indistinguishable from an injected README."""
    with pytest.raises(ValueError, match="does not identify an author"):
        AssertionSet[DeclarationClaim](
            channel=Channel.WATCHED_FOLDER,
            state=AuthorityState.AUTHENTICATED,
            assertions=[],
            author=researcher,
        )


def test_per_item_confirmation_is_supported(researcher):
    """Section 9.5 forbids bulk accept for redaction; the envelope supports
    per-item selection for every assertion type."""
    s = AssertionSet[DeclarationClaim](
        channel=Channel.PARSED_DOCUMENT, state=AuthorityState.PROPOSED,
        assertions=[
            Assertion[DeclarationClaim](payload=DeclarationClaim(jurisdiction="FR"), confidence=0.9),
            Assertion[DeclarationClaim](payload=DeclarationClaim(jurisdiction="DE"), confidence=0.3),
        ],
    ).confirm(human=researcher, accepted=[0])
    assert len(s.effective_payloads()) == 1


def test_confirmation_records_who_confirmed_not_who_authored(researcher, data_steward):
    """A steward may confirm on a researcher's behalf; the record must
    distinguish the two (section 10, C14 override rights)."""
    s = _proposed(Channel.PARSED_DOCUMENT).model_copy(update={"author": researcher})
    confirmed = s.confirm(human=data_steward)
    assert confirmed.author == researcher
    assert confirmed.confirmed_by == data_steward


# -- Section 4.1: the four nodal points ------------------------------------

@pytest.mark.parametrize("kind", [
    EventKind.DECLARATION_CONFIRMED, EventKind.REDACTION_DECIDED,
    EventKind.METADATA_APPROVED, EventKind.DEPOSIT_COMPLETED,
])
def test_human_acts_must_name_a_human(kind, job_id):
    with pytest.raises(ValueError, match="must name the responsible ORCID"):
        Event(sequence=1, job_id=job_id, kind=kind, agent="a/1.0")


def test_human_acts_are_accepted_when_a_human_is_named(researcher, job_id):
    ev = Event(sequence=1, job_id=job_id, kind=EventKind.METADATA_APPROVED,
               agent="metadata/1.0", human=researcher)
    assert ev.human == researcher


def test_provenance_activity_cannot_be_anonymous():
    with pytest.raises(ValueError, match="none act anonymously"):
        ProvActivity(activity_id="x", activity_type="redact", agent=ProvAgent())


# -- Section 7.2: the hash chain -------------------------------------------

@pytest.fixture
def chain(job_id):
    events, prev = [], None
    for i in range(1, 4):
        ev = Event(sequence=i, job_id=job_id, kind=EventKind.WORKFLOW_CREATED,
                   agent="wf/1.0", prev_digest=prev)
        events.append(ev)
        prev = ev.digest()
    return events


def test_intact_chain_verifies(chain):
    verify_chain(chain)


def test_altered_event_breaks_the_chain(chain):
    chain[1] = chain[1].model_copy(update={"agent": "tampered/9.9"})
    with pytest.raises(ValueError, match="chain broken"):
        verify_chain(chain)


def test_reordering_is_detected(chain):
    chain[0], chain[1] = chain[1], chain[0]
    with pytest.raises(ValueError, match="sequence gap or reordering"):
        verify_chain(chain)


def test_chain_may_not_be_silently_started_midway(job_id):
    with pytest.raises(ValueError, match="chain must not break"):
        Event(sequence=2, job_id=job_id, kind=EventKind.WORKFLOW_CREATED, agent="wf/1.0")


# -- Section 9.4: policy completeness --------------------------------------

def test_policy_must_cover_every_sensitivity_class():
    with pytest.raises(ValueError, match="missing"):
        PolicyConfig(backend_by_sensitivity={SensitivityClass.PUBLIC: ["remote"]})


def test_halt_is_the_only_no_backend_action():
    assert [a.value for a in NoBackendAction] == ["halt"]


# -- Section 7.5: provenance defaults to restricted ------------------------

def test_provenance_visibility_defaults_to_restricted(researcher):
    a = ProvActivity(activity_id="x", activity_type="generate",
                     agent=ProvAgent(software="metadata/1.0", human=researcher))
    assert a.visibility is Visibility.RESTRICTED


# -- Section 7.3 / commitment C-3 ------------------------------------------

def test_digest_is_canonical_across_key_order():
    """The chain is only portable if equal objects digest equally everywhere."""
    assert Digest.of_canonical_json({"a": 1, "b": [2, 3]}) == \
           Digest.of_canonical_json({"b": [2, 3], "a": 1})


# -- Relations: how a relation type is arrived at ---------------------------

def test_model_proposed_relation_requires_human_approval():
    """Direction errors (IsSupplementTo vs IsSupplementedBy) are well-formed
    either way and undetectable downstream, so they face a gate."""
    with pytest.raises(ValueError, match="approved by a named human"):
        RelatedResource(identifier="10.1234/paper", identifier_type="DOI",
                        relation_type="IsSupplementTo",
                        origin=RelationOrigin.MODEL_PROPOSED,
                        proposed_by_model="qwen3.8:27b")


def test_derived_relation_credits_no_model(researcher):
    """Version relations follow from the data model; a model that 'inferred'
    one has guessed at something already known."""
    with pytest.raises(ValueError, match="no model may be credited"):
        RelatedResource(identifier="10.1234/v1", identifier_type="DOI",
                        relation_type="IsNewVersionOf",
                        origin=RelationOrigin.DERIVED,
                        proposed_by_model="qwen3.8:27b")


def test_relation_vocabulary_is_closed():
    v = RelationVocabulary(schema_id="DataCite", schema_version="4.6",
                           permitted=frozenset({"Cites", "IsSupplementTo"}))
    v.validate_relation("Cites")
    with pytest.raises(ValueError, match="not in the DataCite 4.6"):
        v.validate_relation("IsSortOfRelatedTo")


# -- Containers: extraction limits are refusals, not suggestions ------------

def test_extraction_defaults_refuse_symlinks_and_absolute_paths():
    limits = ExtractionLimits()
    assert limits.allow_symlinks is False
    assert limits.allow_absolute_paths is False
    assert limits.max_nesting_depth == 1
