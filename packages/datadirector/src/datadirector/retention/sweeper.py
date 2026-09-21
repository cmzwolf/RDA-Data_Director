"""Finding, marking and deleting orphaned working material.

Deletion is an external effect in the sense §7.3 gives that phrase: irreversible,
happening outside the log, and invisible to recovery if a process dies part-way.
It therefore uses the same intent-and-outcome machinery as a repository deposit
rather than a path of its own.

Three properties carry the design.

**Eligibility comes from the log.** A modification time describes a file, not a
job. Work left at the approval gate for three weeks is idle, not abandoned.

**Evidence, not belief.** A persistent identifier in our log says we think the
material was published. Asking the repository says whether it is still there. A
withdrawn record turns a safe deletion into a permanent loss, so the check is
made rather than assumed.

**Two phases.** Candidates are marked with a tombstone and deleted only after a
grace period, so an operator who notices has a window and a researcher who
returns finds an explanation rather than an absence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import rmtree

from datadirector_contracts import (
    AUTOMATIC, DeletionRecord, Digest, EffectKind, Event, EventKind, Orcid,
    RetentionCandidate, RetentionCategory, RetentionMark, StepIntent,
    SurvivalCheck,
)

from ..errors import AuthorityError
from ..state.projection import fold
from ..state.store import EventStore
from ..workflow.effects import EffectRecorder, idempotency_key

TOMBSTONE = ".retention-mark.json"

DEFAULT_GRACE = {
    RetentionCategory.RELEASED: 30,
    RetentionCategory.IDLE: 180,
    RetentionCategory.UNATTRIBUTED: 180,
}

# How long without an event before a live job counts as idle rather than active.
IDLE_AFTER_DAYS = 30


class RetentionSweeper:
    """Reports candidates, marks them, and deletes marked ones after grace.

    Nothing here deletes on the first call. The default is to report: a cleanup
    that removed material because someone was exploring the command line would
    be the failure this design exists to avoid.
    """

    def __init__(self, store: EventStore, working_root: Path | str, *,
                 grace_days: dict[RetentionCategory, int] | None = None,
                 idle_after_days: int = IDLE_AFTER_DAYS,
                 probers: dict | None = None) -> None:
        self.store = store
        self.root = Path(working_root)
        self.grace = {**DEFAULT_GRACE, **(grace_days or {})}
        self.idle_after_days = idle_after_days
        self.probers = probers or {}
        self._effects = EffectRecorder(store, agent="retention/0.1.0")

    # -- finding -----------------------------------------------------------

    def candidates(self, *, now: datetime | None = None
                   ) -> list[RetentionCandidate]:
        now = now or datetime.now(timezone.utc)
        out: list[RetentionCandidate] = []
        if not self.root.exists():
            # Nothing has been ingested here yet. An absent working root is not
            # an error: there is simply nothing to sweep.
            return out
        known = set(self.store.list_jobs())

        for directory in sorted(p for p in self.root.iterdir() if p.is_dir()):
            job_id = directory.name
            size, count = _measure(directory)
            if job_id not in known:
                out.append(RetentionCandidate(
                    job_id=None, path=str(directory),
                    category=RetentionCategory.UNATTRIBUTED,
                    byte_size=size, file_count=count,
                    reason=("no job in the event log corresponds to this "
                            "directory, so what it holds is unknown")))
                continue

            events = self.store.load(job_id)
            state = fold(events)
            last = events[-1].occurred_at if events else None
            idle_days = ((now - last).days if last else 0)

            if state.deposit_pid:
                category = RetentionCategory.RELEASED
                reason = (f"published as {state.deposit_pid}; the repository "
                          "holds the material")
            elif state.terminal == "closed-not-shared":
                category = RetentionCategory.CLOSED_NOT_SHARED
                reason = ("a deliberate decision not to publish; nothing else "
                          "holds this material")
            elif idle_days >= self.idle_after_days:
                category = RetentionCategory.IDLE
                reason = (f"no activity for {idle_days} days, but the job is "
                          f"live at step {state.step!r}")
            else:
                category = RetentionCategory.ACTIVE
                reason = f"active at step {state.step!r}"

            out.append(RetentionCandidate(
                job_id=job_id, path=str(directory), category=category,
                byte_size=size, file_count=count, last_event_at=last,
                pid=state.deposit_pid, reason=reason))
        return out

    # -- verifying ---------------------------------------------------------

    def check_survival(self, candidate: RetentionCandidate) -> SurvivalCheck:
        """Ask whether the material still exists elsewhere.

        Only meaningful for a released candidate. An unreachable repository
        yields `survives=None`, which blocks automatic deletion: not knowing is
        not the same as knowing it is gone, and neither is it permission.
        """
        if candidate.category is not RetentionCategory.RELEASED:
            return SurvivalCheck(
                checked=False,
                detail="survival is only checked for published material")
        prober = self.probers.get(EffectKind.REPOSITORY_PUBLISH)
        if prober is None:
            return SurvivalCheck(
                checked=False,
                detail=("no way to ask the repository whether the record still "
                        "exists; deletion stays a human decision"))
        intent = StepIntent(
            step="survival-check", effect=EffectKind.REPOSITORY_PUBLISH,
            target=candidate.pid or "repository",
            idempotency_key=idempotency_key(candidate.job_id or "", "survival",
                                            candidate.pid or ""),
            detail={"job_id": candidate.job_id, "pid": candidate.pid})
        try:
            finding, detail = prober(intent)
        except Exception as exc:
            return SurvivalCheck(checked=True, survives=None,
                                 detail=f"the repository could not be asked: "
                                        f"{type(exc).__name__}: {exc}"[:200])
        if finding == "took-effect":
            return SurvivalCheck(checked=True, survives=True, detail=detail)
        if finding == "did-not-take-effect":
            return SurvivalCheck(
                checked=True, survives=False,
                detail=(f"{detail}. Our log records a deposit; the repository "
                        "does not. Do not delete: this may now be the only copy"))
        return SurvivalCheck(checked=True, survives=None, detail=detail)

    def assess(self, *, now: datetime | None = None) -> list[RetentionCandidate]:
        """Candidates with their survival checks filled in."""
        return [c.model_copy(update={"survival": self.check_survival(c)})
                for c in self.candidates(now=now)]

    # -- marking -----------------------------------------------------------

    def mark(self, candidate: RetentionCandidate, *, now: datetime | None = None,
             human: Orcid | None = None) -> RetentionMark:
        """Schedule a deletion, leaving an explanation behind."""
        if candidate.category is RetentionCategory.ACTIVE:
            raise AuthorityError(
                f"{candidate.path} belongs to an active job and is not a "
                "retention candidate")
        if candidate.category is RetentionCategory.CLOSED_NOT_SHARED and \
                human is None:
            raise AuthorityError(
                "material from a job that deliberately did not publish is never "
                "marked by a schedule; nothing else holds it, so a person must "
                "order its removal")
        now = now or datetime.now(timezone.utc)
        mark = RetentionMark(
            job_id=candidate.job_id, category=candidate.category,
            reason=candidate.reason, marked_at=now,
            delete_after=now + timedelta(
                days=self.grace.get(candidate.category, 180)),
            pid=candidate.pid, marked_by=human)
        # The tombstone is JSON with the explanation as a field, not JSON
        # followed by prose: a file that has to be parsed by cutting at the last
        # brace is a file that will eventually be parsed wrongly.
        payload = json.loads(mark.model_dump_json())
        payload["note"] = _explanation(mark)
        Path(candidate.path, TOMBSTONE).write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
        return mark

    @staticmethod
    def read_mark(path: Path | str) -> RetentionMark | None:
        tombstone = Path(path, TOMBSTONE)
        if not tombstone.exists():
            return None
        try:
            payload = json.loads(tombstone.read_text(encoding="utf-8"))
            payload.pop("note", None)
            return RetentionMark.model_validate(payload)
        except (ValueError, json.JSONDecodeError):
            # An unreadable tombstone is not a licence to delete. Returning None
            # means "not marked", so the directory stays.
            return None

    def unmark(self, candidate_path: Path | str) -> None:
        """Cancel a scheduled deletion. Returning to work should be enough."""
        tombstone = Path(candidate_path, TOMBSTONE)
        if tombstone.exists():
            tombstone.unlink()

    # -- deleting ----------------------------------------------------------

    def due(self, *, now: datetime | None = None) -> list[tuple[Path, RetentionMark]]:
        now = now or datetime.now(timezone.utc)
        out: list[tuple[Path, RetentionMark]] = []
        if not self.root.exists():
            return out
        for directory in sorted(p for p in self.root.iterdir() if p.is_dir()):
            mark = self.read_mark(directory)
            if mark and mark.delete_after <= now:
                out.append((directory, mark))
        return out

    def delete(self, directory: Path, mark: RetentionMark, *,
               ordered_by: Orcid | None = None) -> DeletionRecord:
        """Remove the material, recording what was removed.

        Bracketed by an intent, like any other irreversible external effect: a
        crash part-way through leaves a partially deleted directory that nothing
        would otherwise record.
        """
        if mark.category in AUTOMATIC or ordered_by is not None:
            pass
        else:
            raise AuthorityError(
                f"{mark.category.value} material is not deleted by a schedule; "
                "a person must order it")

        digests, size, count = _digest_tree(directory)
        intent = StepIntent(
            step="retention-delete", effect=EffectKind.EXTERNAL_FETCH,
            target=str(directory),
            idempotency_key=idempotency_key(mark.job_id or directory.name,
                                            "retention-delete", str(directory)),
            detail={"job_id": mark.job_id, "files": count, "bytes": size})
        job_for_log = mark.job_id or self._any_job()
        if job_for_log is None:
            rmtree(directory)
        else:
            with self._effects.attempt(job_for_log, intent) as result:
                rmtree(directory)
                result.update({"files": count, "bytes": size})

        record = DeletionRecord(
            job_id=mark.job_id, path=str(directory), category=mark.category,
            file_count=count, byte_size=size, digests=digests, pid=mark.pid,
            ordered_by=ordered_by or mark.marked_by)
        if job_for_log is not None:
            self.store.append(Event(
                sequence=1, job_id=job_for_log,
                kind=EventKind.STEP_COMPLETED, agent="retention/0.1.0",
                human=record.ordered_by,
                payload={"step": "retention-delete",
                         "idempotency_key": intent.idempotency_key,
                         "result": json.loads(record.model_dump_json())}))
        return record

    def _any_job(self) -> str | None:
        jobs = self.store.list_jobs()
        return jobs[0] if jobs else None


def _measure(directory: Path) -> tuple[int, int]:
    size = count = 0
    for path in directory.rglob("*"):
        if path.is_file() and path.name != TOMBSTONE:
            size += path.stat().st_size
            count += 1
    return size, count


def _digest_tree(directory: Path) -> tuple[list[Digest], int, int]:
    digests: list[Digest] = []
    size = count = 0
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name == TOMBSTONE:
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digests.append(Digest(value=digest.hexdigest()))
        size += path.stat().st_size
        count += 1
    return digests[:200], size, count


def _explanation(mark: RetentionMark) -> str:
    return (
        "This directory is scheduled for deletion.\n"
        f"  reason: {mark.reason}\n"
        f"  scheduled: {mark.marked_at.isoformat()}\n"
        f"  deletes after: {mark.delete_after.isoformat()}\n"
        + (f"  published as: {mark.pid}\n" if mark.pid else "")
        + "\n"
        "If this material is still needed, remove this file and the deletion is\n"
        "cancelled. Returning to the work should be enough to keep it.\n"
    )
