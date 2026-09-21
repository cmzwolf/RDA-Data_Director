"""The agent's only way to its job: a narrowed handle, not the job itself.

The whole job is not a parameter. An agent receives a `JobHandle` built by the
orchestrator from the deployment's pieces — the log, the working directory,
the ceiling from the config, the ledger from the deployment — narrowed to what
that agent's stated capabilities entitle it to. The handle exposes named
views, not the filesystem: there is no way to ask it for a directory to walk.

An agent can build a `Path` from a name the handle gave it — the language
allows it, and nothing in the orchestrator stops it — and the consequence of
ignoring the seam is that the agent stops being pluggable: what ran by asking
the handle for the probe executor becomes a probe executor built by hand,
which is a thing the orchestrator can no longer see, cap or swap.
"""
from __future__ import annotations


import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from datadirector_contracts import (
    CanonicalRecord, Event, EventKind, GateItem, GateState, ItemDecision,
    Resolution, SensitivityClass,
)

from .gate.items import Gate

from .state.projection import JobState, fold

# The channels whose recorded text carries directive authority. An
# instruction is a depositor's instruction only when the entry point that
# authenticated the person recorded it under one of these.
PLAN_CHANNEL = "plan"
INCOMING_CHANNEL = "incoming"
DEPOSITOR_CHANNELS = frozenset({
    "authenticated-depositor",
    "responsibility-and-compliance-statement",
    "metadata-standard",
    "repository-preference",
})


class JobBlocked(RuntimeError):
    """An agent asked its handle for material while the job is held at a
    gate. The engine asks runnability before it dispatches, so reaching this
    means an agent was entered where it should not have been."""


@dataclass(frozen=True)
class JobStatus:
    """What the gate currently holds, read from the log rather than kept
    beside it: an unresolved item is one whose ``item_id`` appears in a
    ``gate.items-added`` entry and in no ``gate.item-resolved`` entry."""

    blocked: bool = False
    reasons: tuple[str, ...] = ()
    unresolved: tuple[GateItem, ...] = ()


def log_items(events) -> list[GateItem]:
    """Every item ever raised, from the ``gate.items-added`` entries the
    orchestrator appended on agents' behalf."""
    items: list[GateItem] = []
    for event in events:
        payload = event.payload
        if payload.get("marker") == "gate.items-added":
            items.extend(GateItem.model_validate(raw)
                        for raw in payload.get("items", []))
    return items


def job_status(events) -> JobStatus:
    """The gate's current state, read from the log alone.

    A job with no confirmed statement is held from the moment it is created:
    nothing that reads material runs before a person has taken
    responsibility for it, so it is blocked at birth."""
    resolved = {
        event.payload.get("item_id")
        for event in events
        if event.kind is EventKind.REDACTION_DECIDED
         and event.payload.get("item_id")
    }
    unresolved = tuple(item for item in log_items(events)
                        if item.item_id not in resolved)
    reasons: list[str] = []
    if not any(event.kind is EventKind.DECLARATION_CONFIRMED
                for event in events):
        reasons.append("the depositor's statement is not yet confirmed")
    return JobStatus(blocked=bool(reasons), reasons=tuple(reasons),
                        unresolved=unresolved)


def latest_drafted_record(events):
    """The record currently drafted, or None. The record is only ever the
    latest draft: a superseded one stays in the log and never in a reader."""
    for event in reversed(events):
        if event.kind is EventKind.METADATA_DRAFTED:
            dumped = event.payload.get("record")
            if dumped is not None:
                return CanonicalRecord.model_validate(dumped)
    return None

def latest_draft_was_a_persons(events) -> bool:
    """Whether the person, not an agent, wrote the newest draft.

    The record is theirs. A machine may fill what is missing from it and
    may not improve on what they typed: the newest draft is what the review
    screen reads, so a fresh machine draft would push their own wording out
    of view. The rule belongs with the log rather than with the agent that
    happened to need it first.
    """
    for event in reversed(events):
        if event.kind is EventKind.METADATA_DRAFTED:
            return event.human is not None
    return False


def latest_documentation(events):
    """The documentation last drafted, as the log recorded it, or None.

    The README and the data dictionary are recorded rather than returned. An
    agent's `Outcome.result` reaches a caller outside the system and nothing
    else, so a draft that lived only there was a draft that had never existed
    once the process ended. The reader reads the log, which is why a draft made
    last week is still readable today.
    """
    for event in reversed(events):
        if event.kind is EventKind.DOCUMENTATION_DRAFTED:
            return event.payload
    return None


# The channels whose recorded text is the depositor's own account of the
# material, mapped to the name the drafting agents know it by. Only channels an
# authenticated entry point recorded appear here: the same words inside a
# submitted file are evidence, never direction (C-4), and are not reachable by
# name at all.
_DRAFTING_CHANNELS = {
    "responsibility-and-compliance-statement": "statement",
    "authenticated-depositor": "instruction",
}
assert set(_DRAFTING_CHANNELS) <= DEPOSITOR_CHANNELS, (
    "a drafting channel must be a depositor channel, or the drafting agents "
    "would be reading text no entry point authenticated")


def depositor_context(events) -> dict:
    """What the depositor has said about the material, read from the log.

    The structural profile says what the files look like; it cannot say what the
    study is, why it was collected, or over what period. That is what the
    statement, the proposed claims and the plan's commitments carry, and a
    drafting agent asked for an abstract without them can only answer with a
    blank — the correct answer to a question that was never supplied, and a
    useless thing to show a researcher as an empty text box.

    Nothing is invented and nothing is filled in here. An empty dict is the
    honest report that nothing was said; the agent that receives it says so in
    its own undetermined list rather than composing plausible prose.
    """
    context: dict[str, object] = {}
    for event in reversed(events):
        kind = event.kind
        payload = event.payload
        if kind is EventKind.INSTRUCTIONS_RECEIVED:
            key = _DRAFTING_CHANNELS.get(payload.get("channel"))
            if key is not None and key not in context:
                text = str(payload.get(key) or "").strip()
                if text:
                    context[key] = text
        elif kind is EventKind.DECLARATION_PARSED and "claims" not in context:
            claims = payload.get("claims") or []
            if claims:
                context["claims"] = claims
        elif kind is EventKind.DMP_COMMITMENTS_READ:
            context.setdefault("plan_commitments",
                                payload.get("commitments_read") or [])
    return context


def recorded_profile(events) -> dict:
    """The structural profile as ingestion measured it, read from the log.

    Re-walking the unpacked directory would give a second answer to what the
    files are, and one reachable by an agent that never stated an entitlement to
    material. Ingestion recorded the profile it took, so the drafting, the
    documentation and the validation all look at the one profile the depositor
    was shown.
    """
    from .profiling.structural import TreeProfile

    for event in reversed(events):
        if event.kind is EventKind.MATERIAL_REGISTERED:
            dump = event.payload.get("profile")
            if not isinstance(dump, dict):
                return {}
            try:
                return TreeProfile.model_validate(dump).compact()
            except Exception:
                  # A profile this version cannot read is a finding, not a
                  # silence: an empty summary lets the agent say plainly that
                  # it had nothing to draft from.
                return {}
    return {}


def record_summary(record) -> dict:
    """The drafted record, as much of it as a drafting prompt should carry.

    Not the whole record: provenance and the ungrounded-subject bookkeeping are
    the system's own accounting, and pasting them into a prompt asks the model
    to interpret a ledger. What it needs is what the record already asserts, so
    that it does not ask the depositor for what the record already says.
    """
    if record is None:
        return {}
    from datadirector_contracts import DescriptionKind

    def text_of(kind):
        for description in record.descriptions:
            if description.kind == kind:
                return description.text
        return None

    return {
        "title": record.title,
        "abstract": text_of(DescriptionKind.ABSTRACT),
        "methods": text_of(DescriptionKind.METHODS),
        "keywords": [s.term for s in record.subjects],
        "resource_type": record.resource_type.value,
        "language": record.language,
    }


def compact_profile(profile) -> dict:
    """A profile small enough to put in a prompt.

    Falls back to an empty summary rather than sending the whole tree: a
    profile that cannot be compacted is one to paste into a model call.
    """
    compact = getattr(profile, "compact", None)
    if compact is None:
        return {}
    try:
        return compact()
    except Exception:
        return {}


def build_gate(events):
    """The gate as the log describes it.

    Items and resolutions are both events, so the answer to "what is still
    outstanding" is the same after a restart, on another machine, or a year
    later.
    """
    items, resolutions = [], []
    for event in events:
        if event.kind is EventKind.REDACTION_PROPOSED and \
                event.payload.get("marker") == "gate.items-added":
            items += [GateItem.model_validate(raw)
                       for raw in event.payload.get("items", [])]
        elif event.kind is EventKind.REDACTION_DECIDED and event.human:
            resolutions.append(Resolution(
                item_id=event.payload["item_id"],
                decision=ItemDecision(event.payload["decision"]),
                decided_by=event.human,
                reason=event.payload.get("reason"),
                decided_at=event.occurred_at))
    return Gate(GateState(items=items, resolutions=resolutions))

class JobHandle:
    """A narrowed way onto one job, built by the orchestrator before each
    dispatch from the deployment's own pieces. The agent never constructs a
    path onto the job's material: it asks for the material, and the handle
    applies the ceiling, the gate state and the provenance rule on the way
    out."""

    def __init__(self, job_id: str, *, store, working_root: Path,
                    ceiling: SensitivityClass = SensitivityClass.SENSITIVE,
                    inspects_material: bool = False,
                    probe_executor_factory: Callable | None = None,
                    dmp_source_factory: Callable | None = None,
                    ledger=None) -> None:
        self.job_id = job_id
        self._store = store
        self._working_root = Path(working_root)
        self._ceiling = ceiling
        self._inspects = inspects_material
        self._probe_executor_factory = probe_executor_factory
        self._dmp_source_factory = dmp_source_factory
        self._ledger = ledger

     # -- reading ----------------------------------------------------------

    def events(self) -> list[Event]:
        """The whole log of the job, read-only, in append order."""
        return self._store.load(self.job_id)

    @property
    def state(self) -> JobState:
        """The current projection. The agent reads it; it does not carry it."""
        return fold(self.events())

    @property
    def status(self) -> JobStatus:
        """What the gate currently holds. Read from the log, not kept beside
        it."""
        return job_status(self.events())

     # -- material, behind the ceiling and the gate ------------------------

    def material(self) -> list[str]:
        """The artefacts this agent may see: submitted paths, relative to
        the unpacked directory, of files at or below the agent's ceiling, and
        none at all while the job is held at a gate.

        An agent that never looks at material is entitled to none of it:
        material() refuses an agent whose stated capabilities did not say it
        inspects, because a capability that was not stated is not one the
        orchestrator granted."""
        if not self._inspects:
            raise JobBlocked(
                f"agent entered on job {self.job_id} asked for material "
                "without stating inspects_material")
        if self.status.blocked:
            raise JobBlocked(
                f"job {self.job_id} is held at the gate: "
                "; ".join(self.status.reasons))
        state = self.state
        level = (state.classification.level
                if state.classification is not None
                else SensitivityClass.SENSITIVE)
        if level > self._ceiling:
            return []
        return list(state.material)

    def open(self, artefact: str, mode: str = "rb"):
        """Open one artefact the material view named. Reading goes through
        here rather than through a path so the ceiling and the gate are
        applied to bytes as well as to names."""
        self.material()    # gate and ceiling enforced on the way through
        return (self.unpacked_root() / artefact).open(mode)

      # -- working space, the agent's own -----------------------------------

    def working(self, name: str) -> Path:
        """A path inside the job's working directory, for what the agent
        produces. Deliberately a path: an agent that produces a file needs
        one, and this is where the language's freedom is exercised."""
        path = self.job_root() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

      # -- the job's fixed places, named rather than assumed ----------------

    def job_root(self) -> Path:
        return self._working_root / self.job_id

    def unpacked_root(self) -> Path:
        return self.job_root() / "unpacked"

    def incoming_dir(self) -> Path:
        return self.job_root() / INCOMING_CHANNEL

    def submission(self) -> Path | None:
        """What the depositor handed over, as the orchestrator staged it.
        The ingestion agent receives the source this way rather than as a
        path it was given: it never holds the depositor's path."""
        directory = self.incoming_dir()
        if not directory.exists():
            return None
        entries = sorted(directory.iterdir())
        return entries[0] if entries else None

    def supplied_plan(self) -> Path | None:
        """The depositor's own data management plan, as the orchestrator
        copied it aside before any agent could act on it."""
        directory = self.job_root() / PLAN_CHANNEL
        if not directory.exists():
            return None
        entries = sorted(directory.iterdir())
        return entries[0] if entries else None
    def depositor_context(self) -> dict:
        """What the depositor has said about this material, from the log.

        The statement made at the authenticated entry point, the claims their
        declaration was read into, and what their plan committed to. The handle
        offers it as a named view rather than each agent walking the log for it:
        what reaches a drafting prompt is then one auditable thing decided in
        one place, and an agent that was never given the view cannot go and
        fetch something else in its place.

        This is not an entitlement to material. It is the depositor's own words,
        recorded by an entry point that authenticated them; the submitted
        material stays behind `material()` and its ceiling.
        """
        return depositor_context(self.events())


      # -- deployment facilities, gathered by the agent ---------------------

    def probe_executor(self, profile):
        """The probe executor for this job and material. The agent asks the
        handle for it; the handle builds it from the deployment, which is why
        the agent stays pluggable."""
        if self._probe_executor_factory is None:
            raise JobBlocked("the deployment provides no probe executor")
        return self._probe_executor_factory(self.job_id, profile)

    def plan_source(self, reference=None):
        """A source for this job's copy of the plan, built by the
        deployment from the reference the agent named.

        The agent answers "where is the plan"; the handle resolves it. An
        agent that reached for a plan source class itself would still work
        today, and would be a thing the orchestrator can neither see, cap
        nor swap."""
        if self._dmp_source_factory is None:
            raise JobBlocked("the deployment provides no plan source")
        return self._dmp_source_factory(
            self.job_id,
            reference if reference is not None else self.supplied_plan())

    def gate(self):
        """The gate as the log describes it: items as they were raised,
        resolutions as authenticated people decided them."""
        return build_gate(self.events())


# -- staging, the orchestrator's side of the handle -------------------------

def stage_submission(source: Path | str, working_root: Path,
                    job_id: str) -> Path:
    """Copy what the depositor handed over into the job's incoming
    directory and return the staged copy. The copy is the evidence, and the
    copy is what is acted on: the depositor's own path is never held by an
    agent."""
    source = Path(source)
    directory = Path(working_root) / job_id / INCOMING_CHANNEL
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / source.name
    if source.is_dir():
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)
    return destination


def stage_plan(source: Path | str, working_root: Path, job_id: str) -> Path:
    """Copy the depositor's own plan aside before any agent can act on it —
    the orchestrator touches the source path because copying is the
    orchestrator's act, and the agent receives only the handle afterwards."""
    source = Path(source)
    directory = Path(working_root) / job_id / PLAN_CHANNEL
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / source.name
    shutil.copy2(source, destination)
    return destination


