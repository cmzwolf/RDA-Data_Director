"""The external services the offline suite fakes: OLS and re3data.

Both are open and need no credentials, so these run whenever DD_LIVE_TESTS=1.
Their purpose is the same as the Zenodo live test: the offline fakes encode what
we believe the services return, and belief is what goes stale.
"""

from __future__ import annotations

import os

import pytest

from datadirector.registries.re3data import Re3dataRegistry
from datadirector.vocabularies.ols import OlsVocabularyProvider

requires_live = pytest.mark.skipif(
    os.environ.get("DD_LIVE_TESTS") != "1", reason="set DD_LIVE_TESTS=1")


@requires_live
@pytest.mark.parametrize("term,expect_grounded", [
    ("air temperature", True),      # an exact label in several ontologies
    ("soil moisture content", True),  # a synonym of an ENVO concept
    ("soil moisture", False),       # neither: ENVO has "soil moisture content"
    ("station 14", False),          # a spreadsheet label, denotes no concept
])
def test_terms_ground_or_do_not(term, expect_grounded, record_measurement,
                                capsys):
    """Whether a real vocabulary service grounds the kind of term a metadata
    agent proposes.

    The negative cases matter as much. 'station 14' is a label from someone's
    spreadsheet and denotes no concept at all. 'soil moisture' is a real idea
    that OLS happens to record as "concentration of liquid water in soil" with
    "soil moisture content" among its synonyms — so it does not ground, and
    should not: writing a near-miss URI into a published record is worse than an
    absence, because it is machine-readable and looks authoritative. The
    depositor is shown the near candidates instead.

    Synonyms do ground, because a synonym is part of the vocabulary rather than
    a fuzzy match.
    """
    provider = OlsVocabularyProvider()
    candidates = provider.search(term, limit=3)
    grounded = provider.ground(term)

    record_measurement(measurement="vocabulary-grounding", model="n/a",
                       fixture=term, grounded=grounded is not None,
                       grounded_uri=grounded.uri if grounded else None,
                       top=[{"label": t.label, "scheme": t.scheme, "uri": t.uri}
                            for t in candidates[:3]])
    with capsys.disabled():
        print(f"\n  {term!r} -> grounded={grounded is not None}"
              + (f" ({grounded.scheme}: {grounded.uri})" if grounded else ""))
        for t in candidates[:3]:
            print(f"      candidate {t.scheme}: {t.label}")

    if expect_grounded:
        assert grounded is not None, (
            f"{term!r} did not ground to any vocabulary term; candidates were "
            f"{[t.label for t in candidates[:3]]}")
    else:
        assert grounded is None, (
            f"{term!r} grounded to {grounded.uri if grounded else None}. A "
            "spreadsheet label must not become a controlled subject term: the "
            "service returns something for every query, so strictness is the "
            "only thing preventing it")


@requires_live
def test_repositories_can_be_shortlisted_with_the_fields_a_choice_needs(
        record_measurement, capsys):
    """A shortlist is only useful if the registry records what the choice turns
    on: persistent identifiers, access conditions, an API."""
    registry = Re3dataRegistry()
    entries = registry.find_repositories(discipline="climate", limit=3)
    assert entries, "the registry returned no repositories at all"

    described = registry.describe(entries[0]["id"])
    record_measurement(measurement="registry-lookup", model="n/a",
                       fixture=entries[0]["id"],
                       name=described.get("name"),
                       fields_present=sorted(k for k, v in described.items() if v))
    with capsys.disabled():
        print(f"\n  {described.get('name')} ({entries[0]['id']})")
        for key in ("pid_systems", "access_types", "api_types", "certificates"):
            print(f"      {key}: {described.get(key)}")

    assert described.get("name"), "the registry described a repository with no name"
    informative = [k for k in ("pid_systems", "access_types", "api_types",
                               "data_licenses") if described.get(k)]
    assert informative, (
        "the registry recorded none of the fields a repository choice turns on; "
        "a shortlist built from this would be names without reasons")
