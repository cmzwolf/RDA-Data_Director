"""The exposure ledger: append-only record of what a model was shown.

Cluster 3 Part B. Built before the first probe, deliberately: an exposure that
happens before the ledger exists cannot be reconstructed afterwards, and the
whole point of the ledger is to answer questions after the fact.

Design mirrors the event store. Append-only, no delete, JSON lines, and the
content itself never stored — only its digest and a label.
"""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

from datadirector_contracts import (
    BudgetExceeded, Digest, Exposure, ExposureBudget, Orcid, ReleaseKind,
    Residency, SensitivityClass,
)

# Releases that expose an entire artefact rather than a bounded part of one.
WHOLE_ARTEFACT: frozenset[ReleaseKind] = frozenset({
    ReleaseKind.FULL_DOCUMENT,
    ReleaseKind.MEDIA_CONTENT,
})


def requires_human_authority(kind: ReleaseKind, residency: Residency) -> bool:
    """Whether this release needs a named human to authorise it.

    A whole artefact leaving on-premise infrastructure does: the decision is
    categorical rather than incremental, since there is no smaller version of
    "the model read the whole document", and the material has left the
    institution's control.

    A whole artefact read by an on-premise model does **not**. An earlier version
    of this rule required authorisation for every whole-artefact release,
    including local ones, which made it impossible to look at an image in order
    to find out whether it was sensitive without someone first authorising the
    exposure. That is the same circularity the declaration gate exists to break,
    and it would have made media inspection unreachable in exactly the
    deployments that most need it: those handling sensitive material on local
    infrastructure.

    Where the material never leaves the premises, policy has already decided the
    question by permitting the backend at this classification.
    """
    return kind in WHOLE_ARTEFACT and residency is not Residency.ON_PREMISE

# Releases that carry no payload at all. Recorded for completeness of the audit
# trail, but exempt from the byte budget: charging for a derived description
# would make the budget a limit on analysis rather than on disclosure.
NO_PAYLOAD: frozenset[ReleaseKind] = frozenset({
    ReleaseKind.STRUCTURAL_PROFILE,
    ReleaseKind.AGGREGATE,
})


class ExposureLedger:
    SERVES = ("P10", "R10", "C16")
    """One file per job. Append-only."""

    def __init__(self, root: Path | str, budget: ExposureBudget | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget = budget or ExposureBudget()

    def _path(self, job_id: str) -> Path:
        d = self.root / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d / "exposures.jsonl"

    def load(self, job_id: str) -> list[Exposure]:
        path = self._path(job_id)
        if not path.exists():
            return []
        return [Exposure.model_validate_json(line)
                for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def spent(self, job_id: str) -> tuple[int, int]:
        """(bytes charged, releases recorded) for this job."""
        exposures = self.load(job_id)
        charged = sum(e.byte_count for e in exposures if e.kind not in NO_PAYLOAD)
        return charged, len(exposures)

    def spent_per_artefact(self, job_id: str) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for e in self.load(job_id):
            if e.kind not in NO_PAYLOAD:
                out[e.artefact_uri] += e.byte_count
        return dict(out)

    def would_exceed(self, job_id: str, artefact_uri: str, kind: ReleaseKind,
                     byte_count: int) -> str | None:
        """Why this release would breach the budget, or None.

        Checked *before* the release, so a caller can choose a smaller request
        rather than discovering the limit by tripping it.
        """
        if kind in NO_PAYLOAD:
            byte_count = 0
        charged, releases = self.spent(job_id)
        if releases + 1 > self.budget.max_releases_per_job:
            return (f"job {job_id} has made {releases} releases, at the limit of "
                    f"{self.budget.max_releases_per_job}")
        if charged + byte_count > self.budget.max_bytes_per_job:
            return (f"job {job_id} has released {charged} bytes; this release of "
                    f"{byte_count} would exceed the job limit of "
                    f"{self.budget.max_bytes_per_job}")
        per_artefact = self.spent_per_artefact(job_id).get(artefact_uri, 0)
        if per_artefact + byte_count > self.budget.max_bytes_per_artefact:
            return (f"artefact {artefact_uri} has released {per_artefact} bytes; "
                    f"this release of {byte_count} would exceed the per-artefact "
                    f"limit of {self.budget.max_bytes_per_artefact}")
        return None

    def record(self, *, job_id: str, artefact_uri: str, kind: ReleaseKind,
               content: bytes, classification: SensitivityClass, backend: str,
               residency: Residency, detail: str | None = None,
               authorised_by: Orcid | None = None) -> Exposure:
        """Record one release. Raises rather than truncating.

        `content` is hashed and measured, then discarded. It is a parameter
        rather than a digest so that callers cannot accidentally record a
        release without having the thing they released.
        """
        if requires_human_authority(kind, residency) and authorised_by is None:
            raise PermissionError(
                f"a {kind.value} release to {residency.value} infrastructure "
                "exposes an entire artefact beyond the institution and requires "
                "an identified human authorisation; there is no smaller version "
                "of this decision to fall back to"
            )
        byte_count = 0 if kind in NO_PAYLOAD else len(content)
        reason = self.would_exceed(job_id, artefact_uri, kind, byte_count)
        if reason is not None:
            raise BudgetExceeded(
                reason + ". The workflow halts rather than truncating: a model "
                "that stops receiving data without being told will conclude the "
                "data is absent, which is worse than stopping."
            )
        exposure = Exposure(
            job_id=job_id, artefact_uri=artefact_uri, kind=kind,
            byte_count=byte_count, content_digest=Digest.of_bytes(content),
            classification=classification, backend=backend, residency=residency,
            detail=detail, authorised_by=authorised_by,
        )
        self._append(job_id, exposure)
        return exposure

    def _append(self, job_id: str, exposure: Exposure) -> None:
        path = self._path(job_id)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(exposure.model_dump_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def summary(self, job_id: str) -> dict:
        """What was shown to what. The answer to 'what saw this data?'."""
        exposures = self.load(job_id)
        charged, releases = self.spent(job_id)
        by_kind: dict[str, int] = defaultdict(int)
        backends: set[str] = set()
        for e in exposures:
            by_kind[e.kind.value] += 1
            backends.add(e.backend)
        return {
            "job_id": job_id,
            "releases": releases,
            "bytes_charged": charged,
            "budget_bytes_per_job": self.budget.max_bytes_per_job,
            "by_kind": dict(sorted(by_kind.items())),
            "backends": sorted(backends),
            "artefacts": self.spent_per_artefact(job_id),
        }
