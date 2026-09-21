"""Check the conformance matrix against the software.

Appendix B.5 of the architecture records that six matrix rows once claimed
components that did not exist, and that they went unnoticed for weeks in a
project with every incentive to be accurate. §10.2 of the Blueprint makes
conformance self-declared, with no test suite and no independent verification,
so that failure is not an accident of this project: it is what self-declaration
invites.

This module is the mechanical answer. It discovers, by importing the source tree
rather than by reading a list, which components exist and which requirements each
declares it serves; it parses what the matrix claims; and it reports the
disagreements.

Two disagreements matter, in opposite directions.

  **Unsupported claim.** The matrix says a requirement is implemented and no
  component in the tree claims to serve it. This is the R8 failure: a row
  asserting three plugins that were never written.

  **Unclaimed capability.** A component declares it serves a requirement the
  matrix records as absent. Less serious but worth surfacing: it usually means
  work was done and the matrix was not updated, and a conformance claim that
  understates is still a claim nobody checked.

What this cannot do is judge whether a component that exists does its job well.
Declaring `serves = ("R8",)` is a claim like any other, and only a reader of the
code can falsify it. The check narrows the gap; it does not close it.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# Statuses that assert something exists. A row with one of these and no
# component behind it is the failure this module was written for.
CLAIMS_A_COMPONENT = {
    "Implemented", "Partial — architectural", "Partial — deployment",
    "Partial — specification", "Deviation",
}

CLAIMS_NOTHING = {"Not implemented", "Out of scope"}


class Component(BaseModel):
    """Something in the tree that declares which requirements it serves."""

    model_config = ConfigDict(frozen=True)

    name: str
    module: str
    kind: str = Field(description="agent | plugin")
    serves: tuple[str, ...]


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement: str
    kind: str = Field(description="unsupported-claim | unclaimed-capability")
    message: str


class ConformanceReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    components: list[Component]
    unimportable: list[str] = Field(
        default_factory=list,
        description="Modules that failed to import. Reported rather than "
        "treated as absent: a module that will not load provides nothing, but "
        "it is not evidence that the component was never written, and reading "
        "it that way would produce exactly the confusion this report exists to "
        "remove.",
    )
    claims: dict[str, str]
    coverage: dict[str, list[str]]
    findings: list[Finding]

    @property
    def agrees(self) -> bool:
        return not self.findings


PACKAGES = ("datadirector", "datadirector_contracts")


def discover(package: str | tuple[str, ...] = PACKAGES
             ) -> tuple[list[Component], list[str]]:
    """Find components by importing them, not by consulting a list.

    A list would be one more hand-maintained declaration, which is the thing
    that drifted. Importing cannot claim a component that will not load.

    Returns the components and the modules that failed to import. The second is
    not a detail: an early version of this function swallowed import errors, so
    a module broken by a bad edit read as a component that had never been
    written — the same conflation of *absent* with *broken* that this project
    has corrected in three other places. A tool for detecting overstated claims
    must not itself understate.
    """
    packages = (package,) if isinstance(package, str) else package
    components: list[Component] = []
    unimportable: list[str] = []
    seen: set[tuple[str, str]] = set()

    modules = []
    for name in packages:
        root = importlib.import_module(name)
        modules.append(type("_M", (), {"name": name})())
        modules += list(pkgutil.walk_packages(root.__path__, prefix=f"{name}."))

    for info in modules:
        if ".tests" in info.name:
            continue
        try:
            module = importlib.import_module(info.name)
        except Exception as exc:
            unimportable.append(f"{info.name}: {type(exc).__name__}: {exc}")
            continue
        for name, obj in vars(module).items():
            if not inspect.isclass(obj) or obj.__module__ != info.name:
                continue
            declared = _declared_requirements(getattr(obj, "serves", None))
            kind = "agent"
            if declared is None:
                declared = _declared_requirements(getattr(obj, "SERVES", None))
                kind = "plugin"
            if not declared:
                continue
            key = (info.name, name)
            if key in seen:
                continue
            seen.add(key)
            components.append(Component(name=name, module=info.name, kind=kind,
                                        serves=tuple(declared)))
    return sorted(components, key=lambda c: (c.kind, c.name)), unimportable


def _declared_requirements(value) -> tuple[str, ...] | None:
    """A declaration is a sequence of requirement identifiers, or it is nothing.

    Checked by shape rather than by name: `WorkflowGraph.serves` is a method,
    and an earlier version took any attribute called `serves` as a declaration
    and then tried to iterate a function.
    """
    if value is None or callable(value):
        return None
    if isinstance(value, str):
        return None
    try:
        items = tuple(str(item) for item in value)
    except TypeError:
        return None
    return items if all(re.fullmatch(r"[RCP]\d+", item) for item in items) \
        and items else None


def parse_matrix(document: Path | str) -> dict[str, str]:
    """Requirement identifier to the status the matrix records."""
    text = Path(document).read_text(encoding="utf-8")
    claims: dict[str, str] = {}

    def section(start: str, end: str, status_column: int) -> None:
        body = text[text.index(start):text.index(end)]
        for line in body.split("\n"):
            if not line.startswith("| **") or "---" in line:
                continue
            cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
            identifier = cells[0].strip("*").split()[0]
            if re.fullmatch(r"[RCP]\d+", identifier):
                claims[identifier] = cells[status_column]

    section("### B.1 Functional", "### B.2 Non-functional", 3)
    section("### B.2 Non-functional", "### B.3 Architecture", 3)
    section("### B.3 Architecture", "### B.4 Summary", 2)
    return claims


def reconcile(claims: dict[str, str], components: list[Component],
              unimportable: list[str] | None = None) -> ConformanceReport:
    coverage: dict[str, list[str]] = {}
    for component in components:
        for requirement in component.serves:
            coverage.setdefault(requirement, []).append(component.name)

    findings: list[Finding] = []
    for module in unimportable or []:
        findings.append(Finding(
            requirement="—", kind="unimportable-module",
            message=(f"{module}. Nothing this module contains can be counted, "
                     "and its absence from the coverage below says nothing "
                     "about whether the components were written")))

    for requirement, status in sorted(claims.items(), key=_order):
        served = coverage.get(requirement, [])
        if status in CLAIMS_A_COMPONENT and not served:
            findings.append(Finding(
                requirement=requirement, kind="unsupported-claim",
                message=(f"the matrix records {requirement} as {status!r}, and "
                         "no component in the source tree declares that it "
                         "serves it")))
        elif status in CLAIMS_NOTHING and served:
            findings.append(Finding(
                requirement=requirement, kind="unclaimed-capability",
                message=(f"{', '.join(served)} declares it serves "
                         f"{requirement}, which the matrix records as "
                         f"{status!r}")))

    return ConformanceReport(components=components, claims=claims,
                             unimportable=list(unimportable or []),
                             coverage={k: sorted(v) for k, v in coverage.items()},
                             findings=findings)


def _order(item: tuple[str, str]) -> tuple[str, int]:
    identifier = item[0]
    return identifier[0], int(identifier[1:])


def generate(document: Path | str,
             package: str | tuple[str, ...] = PACKAGES) -> ConformanceReport:
    components, unimportable = discover(package)
    return reconcile(parse_matrix(document), components, unimportable)


def render(report: ConformanceReport) -> str:
    lines = [
        "Conformance report",
        "",
        f"{len(report.components)} components declare coverage of "
        f"{len(report.coverage)} requirements.",
        "",
        f"{'requirement':<12} {'matrix status':<26} declared by",
    ]
    for requirement, status in sorted(report.claims.items(), key=_order):
        served = report.coverage.get(requirement, [])
        lines.append(f"{requirement:<12} {status:<26} "
                     f"{', '.join(served) if served else '—'}")

    if report.unimportable:
        lines += ["", "Modules that would not import:"]
        lines += [f"  {m}" for m in report.unimportable]

    if report.findings:
        lines += ["", "Disagreements between the matrix and the tree:"]
        for finding in report.findings:
            lines.append(f"  [{finding.kind}] {finding.message}")
    else:
        lines += ["", "No row claims a component the tree does not contain, and "
                      "no component serves a requirement the matrix records as "
                      "absent."]

    lines += [
        "",
        "This compares declarations, not behaviour. A component declaring it "
        "serves a",
        "requirement is making a claim like any other; only a reader of the "
        "code can",
        "falsify it. The check narrows the gap that Appendix B.5 describes, and "
        "does",
        "not close it.",
    ]
    return "\n".join(lines)
