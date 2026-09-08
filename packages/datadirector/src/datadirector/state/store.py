"""The append-only event log.

Document A §7.2. The most carefully specified module in the system, because
every other guarantee rests on it: resumability, rollback (C8), tamper evidence
(P5, C1) and traceability (R10) are all properties of this file layout.

There is no delete, truncate or update operation. The absence is the contract.
Rollback is a compensating event appended through the normal path (ADR-008).
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from datadirector_contracts import Event, verify_chain

from ..errors import ChainIntegrityError, DataDirectorError


class ConcurrentAppendError(DataDirectorError):
    """Another process appended to this job first. Reload and retry.

    Raised rather than overwriting: two writers each computing prev_digest from
    the same tail would produce a fork, and a forked chain is not a chain.
    """


class EventStore:
    """One directory per job; one file per event.

    Files are named `{sequence:05d}-{unix_timestamp}.json`. The sequence comes
    first because timestamps collide and do not sort reliably across clock
    adjustments; the timestamp is informational only.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    @contextmanager
    def _job_lock(self, job_id: str):
        """Exclusive per-job lock, held for the read-tail/compute/write cycle."""
        d = self._job_dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        lock_path = d / ".lock"
        fh = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ConcurrentAppendError(
                    f"another writer holds the lock for {job_id}; "
                    "reload the job and retry the append"
                ) from exc
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            os.close(fh)

    def list_jobs(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def _event_files(self, job_id: str) -> list[Path]:
        d = self._job_dir(job_id)
        if not d.exists():
            return []
        return sorted(d.glob("[0-9]*.json"), key=lambda p: int(p.name.split("-", 1)[0]))

    def latest_sequence(self, job_id: str) -> int:
        files = self._event_files(job_id)
        return int(files[-1].name.split("-", 1)[0]) if files else 0

    def load(self, job_id: str) -> list[Event]:
        """All events for a job, ordered and verified.

        There is no unchecked read. A caller cannot opt out of verification,
        because a caller that could would eventually be the one that does.
        """
        events = [Event.model_validate_json(p.read_text(encoding="utf-8"))
                  for p in self._event_files(job_id)]
        try:
            verify_chain(events)
        except ValueError as exc:
            raise ChainIntegrityError(
                f"the event log for {job_id} does not verify: {exc}. "
                "The audit record for this job cannot be trusted; do not continue "
                "the workflow and report this to the operator."
            ) from exc
        return events

    def append(self, event_without_chain: Event) -> Event:
        """Append one event, computing its position and predecessor digest.

        The caller supplies an event whose `sequence` and `prev_digest` are
        placeholders; both are set here under the lock, since only the store
        knows the current tail.
        """
        job_id = event_without_chain.job_id
        with self._job_lock(job_id):
            existing = self.load(job_id)
            seq = len(existing) + 1
            prev = existing[-1].digest() if existing else None
            event = event_without_chain.model_copy(
                update={"sequence": seq, "prev_digest": prev}
            )
            self._write_atomic(job_id, event)
            return event

    def _write_atomic(self, job_id: str, event: Event) -> None:
        """Serialise, fsync, then rename.

        A crash between the write and the rename leaves a temporary file that
        `_event_files` does not match, so the log stays valid. A crash after the
        rename leaves a complete event. There is no window in which a partial
        record is visible.
        """
        d = self._job_dir(job_id)
        final = d / f"{event.sequence:05d}-{int(time.time())}.json"
        tmp = d / f".tmp-{event.sequence:05d}-{os.getpid()}"
        payload = json.dumps(
            json.loads(event.model_dump_json()),
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
        dir_fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def verify(self, job_id: str) -> None:
        """Raise ChainIntegrityError if the chain has been broken."""
        self.load(job_id)
