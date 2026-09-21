"""The matrix is checked against the source tree.

`test_conformance_matrix.py` checks the matrix against itself: that the
arithmetic is right and no identifier is missing. This checks it against the
software: that a row claiming a component points at something that exists.

It is the check that would have caught R8, which was recorded as Implemented,
named three plugins, and had none of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from datadirector.conformance.report import (
    CLAIMS_A_COMPONENT, discover, generate, parse_matrix, reconcile, render,
)

DOC = Path(__file__).parent.parent / "docs" / "architecture.md"


@pytest.fixture(scope="module")
def report():
    return generate(DOC)


def test_no_module_fails_to_import(report):
    """A broken module must not be read as an absent component.

    An early version of `discover` swallowed import errors, so a module broken
    by a bad edit looked identical to one that had never been written — the same
    conflation of absent and broken corrected elsewhere in this system.
    """
    assert not report.unimportable, (
        "modules that would not import:\n  " + "\n  ".join(report.unimportable))


def test_no_row_claims_a_component_the_tree_does_not_contain(report):
    """The R8 check.

    R8 was recorded as Implemented naming three Data Management Plan plugins
    that were never written, and it stayed that way for weeks in a project with
    every incentive to be accurate.
    """
    unsupported = [f for f in report.findings if f.kind == "unsupported-claim"]
    assert not unsupported, (
        "the matrix claims components that do not exist:\n  "
        + "\n  ".join(f.message for f in unsupported))


def test_no_component_serves_a_requirement_the_matrix_calls_absent(report):
    """The opposite drift: work done and the matrix not updated.

    Less serious than an overstatement, but still a conformance claim nobody
    checked.
    """
    unclaimed = [f for f in report.findings if f.kind == "unclaimed-capability"]
    assert not unclaimed, (
        "components serve requirements the matrix records as absent:\n  "
        + "\n  ".join(f.message for f in unclaimed))


def test_the_check_fails_when_a_claim_loses_its_component():
    """A check that has never failed is a check nobody has tested.

    Simulated by removing the components that serve a requirement and confirming
    the row is then reported as unsupported.
    """
    claims = parse_matrix(DOC)
    components, _ = discover()
    without_dmp = [c for c in components if "R8" not in c.serves]

    intact = reconcile(claims, components)
    broken = reconcile(claims, without_dmp)

    assert not [f for f in intact.findings if f.requirement == "R8"]
    assert [f for f in broken.findings
            if f.requirement == "R8" and f.kind == "unsupported-claim"], (
        "removing every component serving R8 did not produce a finding; the "
        "check would not have caught the failure it was written for")


def test_a_component_for_an_absent_row_is_reported():
    claims = dict(parse_matrix(DOC))
    claims["R12"] = "Not implemented"
    components, _ = discover()
    invented = list(components) + [
        type(components[0])(name="Imaginary", module="nowhere", kind="agent",
                            serves=("R12",))]
    findings = reconcile(claims, invented).findings
    assert any(f.requirement == "R12" and f.kind == "unclaimed-capability"
               for f in findings)


def test_every_status_that_claims_something_is_checked():
    """The status vocabulary and the check must not drift apart.

    A status added to the matrix but not to `CLAIMS_A_COMPONENT` would be
    exempt from checking without anyone deciding that it should be.
    """
    from tests.test_conformance_matrix import VALID_STATUSES

    unchecked = VALID_STATUSES - CLAIMS_A_COMPONENT - {"Not implemented",
                                                       "Out of scope"}
    assert not unchecked, (
        f"statuses neither checked nor explicitly exempt: {sorted(unchecked)}")


def test_the_report_states_its_own_limits(report):
    """It compares declarations, not behaviour, and should say so.

    A component declaring `serves = ("R8",)` is making a claim like any other.
    A report that read as a verification would be the overstatement it exists to
    prevent.
    """
    text = render(report)
    assert "declarations, not behaviour" in text
    assert "does" in text and "not close it" in text
