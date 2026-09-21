"""The repository recommendation agent.

Requirement R1. Finds candidate repositories and reports what is known about
them; the depositor chooses.

The choice is not the agent's to make. It has funder, journal and institutional
consequences, it is frequently constrained by policy the tool cannot see, and
getting it wrong is expensive to undo once a DOI exists. So the agent shortlists
with reasons, and the selection is a human act recorded as one.

Where a Data Management Plan names a repository, that is a commitment and is
surfaced first (R8): a shortlist that quietly ignored what the project promised
its funder would be recommending a discrepancy.
"""

from __future__ import annotations

from datadirector_contracts import DecisionRecord, Event, EventKind, Orcid
from pydantic import BaseModel, ConfigDict, Field

from ..errors import ExternalServiceError
from ..state.projection import JobState
from .base import Agent


from ..workflow.graph import Condition
from .base import Capabilities, Invocation, Outcome
from .registry import AgentContext, register_agent

class Candidate(BaseModel):
    """One repository, with what is known and what is not."""

    model_config = ConfigDict(frozen=True)

    identifier: str
    name: str
    url: str | None = None
    reasons: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    committed_in_plan: bool = False
    assigns_pids: bool | None = Field(
        default=None,
        description="None where the registry does not say. Distinguished from "
        "False because 'unknown' and 'no' lead to different questions.",
    )


@register_agent
class RepositoryAgent(Agent):
    name = "repository"
    serves = ("R1",)

    def __init__(self, registry=None, *, configured: str | None = None) -> None:
        self._registry = registry
        self._configured = configured


    def shortlist(self, state: JobState, *, discipline: str | None = None,
                  plan_commitment: str | None = None, limit: int = 5
                  ) -> tuple[list[Candidate], DecisionRecord]:
        candidates: list[Candidate] = []
        undetermined: list[str] = []

        if plan_commitment:
            candidates.append(Candidate(
                identifier="plan", name=plan_commitment,
                reasons=["named in the data management plan as the intended "
                         "repository"],
                committed_in_plan=True))

        if self._registry is not None:
            try:
                for entry in self._registry.find_repositories(
                        discipline=discipline, limit=limit):
                    described = self._describe(entry["id"])
                    candidates.append(self._candidate(entry, described,
                                                      plan_commitment))
            except ExternalServiceError as exc:
                undetermined.append(
                    f"registry unavailable, so no candidates were listed: {exc}")
        else:
            undetermined.append(
                "no repository registry is configured, so candidates cannot be "
                "listed and only a configured default is available")

        if self._configured and not any(c.name == self._configured
                                        for c in candidates):
            candidates.append(Candidate(
                identifier="configured", name=self._configured,
                reasons=["configured for this deployment"]))

        decision = DecisionRecord(
            agent=self.identity, step="shortlist-repositories",
            options_considered=[{"option": c.name} for c in candidates],
            selected=None,
            selection_basis=(
                "candidates are listed with what the registry records about "
                "them; the choice has funder, journal and institutional "
                "consequences and is expensive to undo once an identifier "
                "exists, so it is made by a person"),
            undetermined=undetermined + [
                "selection: awaiting the depositor's choice"],
        )
        return candidates, decision

    def _describe(self, identifier: str) -> dict:
        try:
            return self._registry.describe(identifier)
        except Exception:
            return {}

    @staticmethod
    def _candidate(entry: dict, described: dict,
                   plan_commitment: str | None) -> Candidate:
        reasons, concerns = [], []
        pid_systems = [p for p in described.get("pid_systems", [])
                       if p.lower() not in ("none", "other")]
        if pid_systems:
            reasons.append(f"assigns persistent identifiers: "
                           f"{', '.join(sorted(set(pid_systems))[:3])}")
        elif described:
            concerns.append("the registry records no persistent identifier "
                            "system, which requirement R7 needs")

        if described.get("certificates"):
            reasons.append(f"certified: "
                           f"{', '.join(sorted(set(described['certificates']))[:2])}")
        access = {a.lower() for a in described.get("access_types", [])}
        if "open" in access:
            reasons.append("offers open access deposits")
        elif access:
            concerns.append(f"access types recorded: {', '.join(sorted(access))}")
        if described.get("api_types"):
            reasons.append("has a deposit API")
        else:
            concerns.append("no API recorded; deposit may require manual steps")

        name = described.get("name") or entry.get("name") or entry["id"]
        return Candidate(
            identifier=entry["id"], name=name,
            url=described.get("url") or entry.get("link"),
            reasons=reasons, concerns=concerns,
            committed_in_plan=bool(plan_commitment
                                   and plan_commitment.lower() in name.lower()),
            assigns_pids=bool(pid_systems) if described else None,
        )

    def select(self, state: JobState, candidate: Candidate, *, human: Orcid,
               reason: str | None = None) -> tuple[list[Event], DecisionRecord]:
        """Record the depositor's choice. A human act, so it names one."""
        events = [self.event(state, EventKind.REPOSITORY_SELECTED, human=human,
                             payload={"repository": candidate.name,
                                      "identifier": candidate.identifier,
                                      "committed_in_plan":
                                          candidate.committed_in_plan,
                                      "reason": reason})]
        decision = DecisionRecord(
            agent=self.identity, step="select-repository",
            selected=candidate.name,
            selection_basis=(reason or "selected by the depositor")
            + ("; named in the data management plan"
               if candidate.committed_in_plan else ""))
        return events, decision

    @classmethod
    def build(cls, context: AgentContext) -> "RepositoryAgent":
        return cls(context.re3data, configured=context.repository_name)
    @classmethod
    def capabilities(cls) -> Capabilities:
        return Capabilities(
            name="repository",
            summary=("shortlists candidate repositories from the registry "
                      "and the plan's commitment"),
            human_follows=True,
            establishes=(
                Condition("the deposit's destination has been chosen",
                          lambda s: EventKind.REPOSITORY_SELECTED
                          in s.seen_kinds),
              ),
             # It reads the log's recorded plan reference and a remote
             # registry. It never touches the job's material.
            inspects_material=False,
            serves=("R1",))
    def run(self, invocation: Invocation) -> Outcome:
        """Shortlist candidate repositories.
        Here rather than at ingestion because this reaches a remote
        registry: a researcher whose upload hangs because a registry is
        slow has been failed by an ordering decision, not by a registry. As
        a graph node this halts and retries like anything else with an
        external dependency; as part of ingestion it would not.
        """
        handle = invocation.job
        state = handle.state
        plan = next((event.payload.get("reference") for event in reversed(
            handle.events())
            if event.kind is EventKind.DMP_COMMITMENTS_READ), None)
        candidates, decision = self.shortlist(state, plan_commitment=plan)
        return Outcome(job_id=handle.job_id, decision=decision,
                       result=candidates,
                       message=f"shortlisted {len(candidates)} repositories")
