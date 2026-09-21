"""The operational scenarios are checked for shape, not for outcome.

They are run by a person: their outcomes are judgements, and automating those
would replace the thing they exist to provide. What can be checked is that each
scenario is complete enough to run and honest enough to interpret — a scenario
without a stated failure condition produces a result nobody can read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SCENARIOS = Path(__file__).parent.parent / "operational-tests"


def _directories():
    return sorted(p for p in SCENARIOS.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


@pytest.mark.parametrize("scenario", _directories(), ids=lambda p: p.name)
def test_each_scenario_can_be_run(scenario):
    """Data and a statement: the minimum for someone to try it."""
    assert (scenario / "README.md").exists()
    assert (scenario / "statement.txt").exists(), (
        "no statement to paste at the declaration screen")
    data = scenario / "data"
    assert data.exists() and any(data.iterdir()), "no data to submit"


@pytest.mark.parametrize("scenario", _directories(), ids=lambda p: p.name)
def test_each_scenario_says_what_would_be_a_failure(scenario):
    """A result you cannot interpret is not evidence.

    Stating only what should happen invites the reading that anything else is
    merely interesting; naming the failure makes a bad outcome legible as one.
    """
    readme = (scenario / "README.md").read_text(encoding="utf-8")
    assert "What should happen" in readme
    assert "What would be a failure" in readme
    assert "What we are trying to learn" in readme


@pytest.mark.parametrize("scenario", _directories(), ids=lambda p: p.name)
def test_no_scenario_is_silently_empty(scenario):
    statement = (scenario / "statement.txt").read_text(encoding="utf-8")
    assert len(statement.split()) > 20, (
        "the statement is too short to exercise a declaration agent")


def test_the_index_lists_every_scenario():
    """An index that drifts from the directory is worse than none: a scenario
    absent from it is one nobody runs."""
    index = (SCENARIOS / "README.md").read_text(encoding="utf-8")
    for scenario in _directories():
        number = scenario.name.split("-")[0]
        assert f"| {number} |" in index, (
            f"{scenario.name} is not in the index table")


def test_the_scenarios_span_the_sensitivity_range():
    """A set that is all hard cases does not test whether the system is quiet
    when there is nothing to say, which is most real data."""
    names = " ".join(p.name for p in _directories())
    assert "open" in names, "no plainly-public scenario"
    assert any(word in names for word in ("clinic", "sensitive")), (
        "no plainly-sensitive scenario")


def test_the_invented_data_is_flagged_as_invented():
    """These files contain names, addresses and diagnoses. Somebody will
    eventually wonder whether they are real."""
    index = (SCENARIOS / "README.md").read_text(encoding="utf-8")
    assert "invented" in index
    assert "sandbox" in index
