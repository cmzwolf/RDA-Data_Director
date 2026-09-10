"""The ingestion agent.

Document A §8.4. Detects material, registers it, computes digests, unpacks any
container under guard, and extracts a structural profile.

It reads no content, calls no model, and takes no further step until a
declaration is present and confirmed. That restraint is the point: ingestion runs
before any classification exists, so it must not be capable of exposing material.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from datadirector_contracts import DecisionRecord, Event, EventKind
from datadirector_contracts.containers import ExtractionLimits
from datadirector_contracts.primitives import ArtefactRef, Digest

from ..containers.detect import detect
from ..errors import ExtractionError
from ..profiling.structural import profile_tree
from ..state.projection import JobState
from .base import Agent


def digest_file(path: Path) -> tuple[Digest, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
            size += len(chunk)
    return Digest(value=h.hexdigest()), size


class IngestionAgent(Agent):
    name = "ingestion"

    def __init__(self, working_root: Path | str,
                 limits: ExtractionLimits | None = None) -> None:
        self.working_root = Path(working_root)
        self.limits = limits or ExtractionLimits()

    def runnable(self, state: JobState) -> bool:
        return state.step == "created" and not state.material

    def ingest(self, state: JobState, source: Path) -> tuple[list[Event], DecisionRecord]:
        """Register one submission and profile it.

        A container is unpacked to a working directory; loose files are copied
        as they are. Both paths converge on the same profile, so nothing
        downstream depends on how the material arrived.
        """
        work = self.working_root / state.job_id
        work.mkdir(parents=True, exist_ok=True)
        options, selected = [], None
        declared_metadata = None
        checksums_verified = None

        if source.is_file():
            fmt = detect(source)
            if fmt is not None:
                options.append(f"container:{fmt.format_id}")
                try:
                    profile = fmt.extract(source, work / "unpacked", self.limits)
                    selected = f"container:{fmt.format_id}"
                    declared_metadata = profile.declared_metadata
                    checksums_verified = profile.checksums_verified
                    tree = profile_tree(work / "unpacked")
                    archive_ref: ArtefactRef | None = profile.archive
                except ExtractionError:
                    # Refusal is fatal for this submission and propagates: the
                    # workflow engine records it as a halt with the reason, so
                    # capturing it here would duplicate the record rather than
                    # add to it. The archive is rejected, not partially ingested.
                    raise
            else:
                selected = "loose-file"
                target = work / "unpacked" / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                tree = profile_tree(work / "unpacked")
                archive_ref = None
        else:
            selected = "directory"
            dest = work / "unpacked"
            dest.mkdir(parents=True, exist_ok=True)
            for p in source.rglob("*"):
                if p.is_file():
                    t = dest / p.relative_to(source)
                    t.parent.mkdir(parents=True, exist_ok=True)
                    t.write_bytes(p.read_bytes())
            tree = profile_tree(dest)
            archive_ref = None

        payload = {
            "artefacts": [f.path for f in tree.files],
            "file_count": tree.file_count,
            "total_bytes": tree.total_bytes,
            "source_kind": selected,
            "profile": tree.model_dump(mode="json"),
        }
        if archive_ref is not None:
            # The archive digest is what we can prove was submitted; the member
            # digests are what we can prove was deposited (§8.4). Both are kept,
            # and the lineage between them lives here rather than in a
            # relatedIdentifier, since the members carry no separate identifier.
            payload["submitted_archive"] = {
                "uri": archive_ref.uri, "digest": archive_ref.digest.value,
                "byte_size": archive_ref.byte_size,
            }
        if declared_metadata is not None:
            payload["declared_metadata_present"] = True
        if checksums_verified is not None:
            payload["checksums_verified"] = checksums_verified

        decision = DecisionRecord(
            agent=self.identity, step="ingest",
            options_considered=[{"option": o} for o in options] or [{"option": selected}],
            selected=selected,
            selection_basis=(
                "container marker files determine the format; self-describing formats "
                "are preferred over generic ones so supplied metadata is read rather "
                "than re-derived"
            ),
            undetermined=(
                ["member roles: which files are data, documentation or supplementary"]
            ),
        )
        return [self.event(state, EventKind.MATERIAL_REGISTERED, payload=payload)], decision
