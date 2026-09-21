"""The conformance matrix is checked against itself.

Appendix B.5 records that six matrix rows once claimed components that did not
exist. This test does not address that — only the source tree can — but it
closes the adjacent failure that produced it three times in one day: figures
maintained by hand, which drift silently because nothing recomputes them.

What is checked here is internal consistency. Every requirement appears exactly
once, every status is one the document defines, and the summary table matches
the rows it summarises. What is not checked is whether a row is *true*: that
requires reading the code, and Appendix B.5 exists because nobody does.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

DOC = Path(__file__).parent.parent / "docs" / "architecture.md"

VALID_STATUSES = {
    "Implemented", "Partial — architectural", "Partial — deployment",
    "Partial — specification", "Deviation", "Not implemented", "Out of scope",
}

EXPECTED_IDS = (
    [f"R{i}" for i in range(1, 13)]
    + [f"C{i}" for i in range(1, 18)]
    + [f"P{i}" for i in range(1, 15)]
)


@pytest.fixture(scope="module")
def matrix():
    text = DOC.read_text(encoding="utf-8")

    def section(start: str, end: str) -> list[list[str]]:
        body = text[text.index(start):text.index(end)]
        return [[c.strip() for c in line.strip().strip("|").split(" | ")]
                for line in body.split("\n")
                if line.startswith("| **") and "---" not in line]

    functional = section("### B.1 Functional", "### B.2 Non-functional")
    non_functional = section("### B.2 Non-functional", "### B.3 Architecture")
    principles = section("### B.3 Architecture", "### B.4 Summary")

    entries = []
    for cells in functional:
        entries.append({"id": _ident(cells[0]), "priority": _priority(cells[2]),
                        "status": cells[3]})
    for cells in non_functional:
        entries.append({"id": _ident(cells[0]), "priority": "mandatory",
                        "status": cells[3]})
    for cells in principles:
        entries.append({"id": _ident(cells[0]), "priority": "mandatory",
                        "status": cells[2]})
    return entries


def _ident(cell: str) -> str:
    return cell.strip("*").split()[0]


def _priority(cell: str) -> str:
    cleaned = cell.strip("*").strip().lower()
    return cleaned if cleaned in ("must", "should", "may") else "mandatory"


def test_every_blueprint_item_appears_exactly_once(matrix):
    """A requirement omitted from the matrix is a requirement nobody declined."""
    seen = Counter(e["id"] for e in matrix)
    duplicated = [k for k, n in seen.items() if n > 1]
    missing = [k for k in EXPECTED_IDS if k not in seen]
    unexpected = [k for k in seen if k not in EXPECTED_IDS]
    assert not duplicated, f"listed more than once: {duplicated}"
    assert not missing, f"absent from the matrix: {missing}"
    assert not unexpected, f"not a Blueprint identifier: {unexpected}"


def test_every_status_is_one_the_document_defines(matrix):
    """An invented status reads as a considered judgement and is not one."""
    unknown = {e["status"] for e in matrix} - VALID_STATUSES
    assert not unknown, (
        f"status values not defined in the status vocabulary: {sorted(unknown)}")


def test_the_summary_matches_the_rows_it_summarises():
    """The check that would have caught three hand-counting errors in a day."""
    text = DOC.read_text(encoding="utf-8")
    body = text[text.index("### B.4 Summary"):]
    summary = {}
    for line in body.split("\n"):
        if not line.startswith("| ") or "---" in line:
            continue
        cells = [c.strip().strip("*") for c in line.strip().strip("|").split(" | ")]
        if len(cells) != 5 or cells[0] in ("Status", ""):
            continue
        try:
            summary[cells[0]] = [int(c) for c in cells[1:]]
        except ValueError:
            continue
    assert summary, "no summary table was found in Appendix B.4"

    entries = _entries(text)
    for status in VALID_STATUSES | {"Total"}:
        if status == "Total":
            rows = entries
        else:
            rows = [e for e in entries if e["status"] == status]
        if not rows and status not in summary:
            continue
        mandatory = sum(1 for e in rows if e["priority"] in ("must", "mandatory"))
        should = sum(1 for e in rows if e["priority"] == "should")
        may = sum(1 for e in rows if e["priority"] == "may")
        expected = [mandatory, should, may, len(rows)]
        assert status in summary, f"the summary omits the {status!r} row"
        assert summary[status] == expected, (
            f"summary row {status!r} says {summary[status]}, "
            f"the tables give {expected}")


def test_the_totals_add_up():
    entries = _entries(DOC.read_text(encoding="utf-8"))
    assert len(entries) == len(EXPECTED_IDS) == 43


def _entries(text: str) -> list[dict]:
    def section(start: str, end: str) -> list[list[str]]:
        body = text[text.index(start):text.index(end)]
        return [[c.strip() for c in line.strip().strip("|").split(" | ")]
                for line in body.split("\n")
                if line.startswith("| **") and "---" not in line]

    out = []
    for cells in section("### B.1 Functional", "### B.2 Non-functional"):
        out.append({"id": _ident(cells[0]), "priority": _priority(cells[2]),
                    "status": cells[3]})
    for cells in section("### B.2 Non-functional", "### B.3 Architecture"):
        out.append({"id": _ident(cells[0]), "priority": "mandatory",
                    "status": cells[3]})
    for cells in section("### B.3 Architecture", "### B.4 Summary"):
        out.append({"id": _ident(cells[0]), "priority": "mandatory",
                    "status": cells[2]})
    return out
