"""The validation agent. Deterministic: no model.

Cluster 4 Part B3, requirements R4 and C15.

Two kinds of check, and the second is the interesting one.

**Schema validation** asks whether the record is well formed for its target.
Repository-provided rules are preferred over community ones where both exist,
because the repository is the party that will actually refuse the deposit.

**Consistency checking** is our operationalisation of C15 (ADR-011). C15
requires AI-generated metadata to meet the same curation standards as manually
produced records, and no community benchmark for that exists. What is
measurable today is whether a generated value contradicts its own source: a
title claiming a date range the data do not cover, a described column that is
not in the file, a stated language that the text is not in. Contradiction is
checkable; quality is not.

A validation failure blocks deposit and is not a gate item a human can wave
through. The repository would refuse it anyway, and failing here is cheaper and
more legible than failing at the deposit endpoint.
"""

from __future__ import annotations

import re
from datetime import date

from datadirector_contracts import (
    CanonicalRecord, DecisionRecord, DescriptionKind, ValidationFinding,
)

ERROR = "error"
WARNING = "warning"
INFO = "info"

YEAR = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")


from datadirector_contracts import EventKind
from ..job_handle import latest_drafted_record, recorded_profile
from ..workflow.engine import StepFailed
from ..workflow.graph import Condition
from .base import Agent, Capabilities, Invocation, Outcome
from .registry import AgentContext, register_agent

@register_agent
class ValidationAgent(Agent):
    name = "validation"
    serves = ("R4", "C15")
    version = "0.1.0"

    @property
    def identity(self) -> str:
        return f"{self.name}/{self.version}"

    def __init__(self, profile=None, validators: list | None = None) -> None:
        self._profile = profile
        self._validators = list(validators or [])

    def validate(self, record: CanonicalRecord, *, profile_summary: dict | None = None
                 ) -> tuple[list[ValidationFinding], DecisionRecord]:
        findings: list[ValidationFinding] = []

        if self._profile is not None:
            findings += [
                ValidationFinding(severity=ERROR, field=field,
                                  message=f"required by "
                                          f"{self._profile.schema_id}",
                                  rule="required-field")
                for field in self._profile.missing_required(record)
            ]
            projected = self._profile.project(record)
            for validator in self._validators:
                findings += validator.validate(
                    projected, schema_id=self._profile.schema_id)

        findings += self._consistency(record, profile_summary or {})
        findings += self._provenance(record)
        findings += self._described(record)

        errors = [f for f in findings if f.severity == ERROR]
        decision = DecisionRecord(
            agent=self.identity, step="validate",
            selected=f"{len(errors)} errors, "
                     f"{len(findings) - len(errors)} warnings",
            selection_basis=(
                "schema conformance from the target profile, plus cross-source "
                "consistency checking as the operationalisation of C15 "
                "(ADR-011): contradiction is checkable, quality is not"
            ),
            undetermined=(
                [] if self._profile else
                ["schema conformance: no target profile was selected"]),
        )
        return findings, decision

    # -- C15: contradiction between a value and its source -----------------

    def _consistency(self, record: CanonicalRecord, profile_summary: dict
                     ) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        columns = {
            column["name"].lower()
            for entry in profile_summary.get("files", [])
            for column in entry.get("columns", [])
        }

        # A title or abstract naming years the data do not cover.
        covered = self._covered_years(record)
        for field, text in self._texts(record):
            claimed = {int(y) for y in YEAR.findall(text)}
            if claimed and covered and not (claimed & covered):
                findings.append(ValidationFinding(
                    severity=WARNING, field=field,
                    message=(f"mentions {sorted(claimed)} but the recorded "
                             f"coverage is {sorted(covered)}"),
                    rule="c15-temporal-consistency"))

        # A description naming a column that is not in the files.
        if columns:
            for field, text in self._texts(record):
                for quoted in re.findall(r"[`'\"]([A-Za-z_][A-Za-z0-9_]{2,})[`'\"]",
                                         text):
                    if quoted.lower() not in columns:
                        findings.append(ValidationFinding(
                            severity=WARNING, field=field,
                            message=(f"refers to {quoted!r}, which is not a "
                                     "column in the deposited files"),
                            rule="c15-column-consistency"))

        # A publication year in the future.
        if record.publication_year and record.publication_year > date.today().year:
            findings.append(ValidationFinding(
                severity=ERROR, field="publication_year",
                message=f"{record.publication_year} is in the future",
                rule="c15-plausibility"))

        # Ungrounded keywords, reported rather than silently accepted (R2).
        ungrounded = record.ungrounded_subjects()
        if ungrounded:
            findings.append(ValidationFinding(
                severity=INFO, field="subjects",
                message=("no controlled vocabulary term was found for: "
                         + ", ".join(s.term for s in ungrounded[:6])),
                rule="r2-vocabulary-absence"))
        return findings

    def _provenance(self, record: CanonicalRecord) -> list[ValidationFinding]:
        """C14: a value nobody can account for is a finding, not an omission."""
        missing = record.fields_without_provenance()
        return [ValidationFinding(
            severity=WARNING, field=field,
            message="populated but no origin recorded; a reviewer cannot see "
                    "where this value came from",
            rule="c14-field-provenance") for field in missing]

    @staticmethod
    def _described(record: CanonicalRecord) -> list[ValidationFinding]:
        """A record the repository will publish empty is a finding, not a silence.

        Neither DataCite nor Zenodo requires a description, which is exactly how
        a dataset leaves this system with no Description and no Methods at all:
        schema validation passes, the gate opens, and nobody says that the page a
        stranger will read is blank. The first such record happened because a
        drafting agent was never given what the depositor had said; this is the
        check that refuses to call that result a pass.
        """
        kinds = {d.kind for d in record.descriptions}
        findings: list[ValidationFinding] = []
        if DescriptionKind.ABSTRACT not in kinds:
            findings.append(ValidationFinding(
                severity=WARNING, field="descriptions",
                message=("has no description: the repository will publish this "
                              "dataset with nothing at all to read about it"),
                rule="record-described"))
        if DescriptionKind.METHODS not in kinds:
            findings.append(ValidationFinding(
                severity=INFO, field="descriptions",
                message=("has no methods statement: a reader cannot see how the "
                              "data were obtained"),
                rule="record-described"))
        return findings

    @staticmethod
    def _texts(record: CanonicalRecord) -> list[tuple[str, str]]:
        out = [("title", record.title)]
        out += [(f"descriptions[{i}]", d.text)
                for i, d in enumerate(record.descriptions)]
        return out

    @staticmethod
    def _covered_years(record: CanonicalRecord) -> set[int]:
        years: set[int] = set()
        for entry in record.dates:
            end = entry.end or entry.value
            years.update(range(entry.value.year, end.year + 1))
        return years

    _deployment_profile_for = None
    @classmethod
    def build(cls, context: AgentContext) -> "ValidationAgent":
        agent = cls(context.schema_profile,
                     [] if context.validator is None
                    else [context.validator])
        agent._deployment_profile_for = context.profile_for
        return agent
    @classmethod
    def capabilities(cls) -> Capabilities:
        return Capabilities(
            name="validation",
            summary=("checks the drafted record against the profile of the "
                      "standard that was chosen, not a fixed one"),
            establishes=(
                Condition("the record has been validated",
                          lambda s: EventKind.VALIDATION_COMPLETED
                          in s.seen_kinds),
                 ),
            inspects_material=False,
            serves=("R4", "C15"))
    def run(self, invocation: Invocation) -> Outcome:
        """Validate against the standard that was actually chosen.
        The deployment resolves the profile by standard name - validating a
        RO-Crate deposit against a DataCite profile would report truth as
        error. The findings are reported, never filtered: what is blocking is
        a question for the gate and the person, not for this agent to soften.
        """
        handle = invocation.job
        state = handle.state
        events_log = handle.events()
        record = latest_drafted_record(events_log)
        if record is None:
            raise StepFailed("no metadata record has been drafted yet")
        standard = next((event.payload.get("metadata_standard")
                          for event in reversed(events_log)
                          if event.payload.get("metadata_standard")), None)
        profile = self._profile
        if self._deployment_profile_for is not None:
            profile = self._deployment_profile_for(standard)
        findings, decision = ValidationAgent(profile, self._validators) \
                .validate(record, profile_summary=recorded_profile(events_log))
        return Outcome(
            job_id=handle.job_id,
            events=[self.event(
                state, EventKind.VALIDATION_COMPLETED,
                payload={"findings": [f.model_dump(mode="json")
                                       for f in findings],
                           "blocking": any(f.severity == "error"
                                            for f in findings)})],
            decision=decision, result=findings,
            message=f"validated: {len(findings)} findings")


def blocks_deposit(findings: list[ValidationFinding]) -> bool:
    """Errors block. Warnings and info do not.

    Deliberately not a gate item: a human cannot approve away a record the
    repository will refuse, and offering the choice would imply otherwise.
    """
    return any(f.severity == ERROR for f in findings)
