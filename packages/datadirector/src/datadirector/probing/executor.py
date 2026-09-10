"""Probe execution: resolve against job state, run, charge the ledger.

Cluster 3 Part C. The model proposes probes; this runs them. Nothing here
interprets results, and nothing here calls a model.

The security work is in `_resolve`: every argument must name something already
present in job state. A probe naming anything else is refused with a reason,
never answered emptily, because silence is indistinguishable from a genuine
empty result and a model told nothing will infer the wrong thing.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from datadirector_contracts import (
    ProbeKind, ProbeRefused, ProbeRequest, ProbeResult, ReleaseKind,
    Residency, SensitivityClass,
)
from datadirector_contracts.probing import VALUE_RETURNING

from ..exposure.ledger import ExposureLedger
from ..profiling.structural import TreeProfile, _shape_of

CHUNK_CHARS = 6000
CHUNK_OVERLAP = 600

RELEASE_KIND = {
    ProbeKind.SAMPLE_FIELD: ReleaseKind.FIELD_SAMPLE,
    ProbeKind.READ_CHUNK: ReleaseKind.CONTENT_CHUNK,
    ProbeKind.DISTINCT_COUNT: ReleaseKind.AGGREGATE,
    ProbeKind.VALUE_SHAPES: ReleaseKind.AGGREGATE,
    ProbeKind.NULL_PATTERN: ReleaseKind.AGGREGATE,
    ProbeKind.CROSS_TAB: ReleaseKind.AGGREGATE,
}


class ProbeExecutor:
    def __init__(self, job_id: str, profile: TreeProfile, working_root: Path | str,
                 ledger: ExposureLedger, *, disabled: set[ProbeKind] | None = None,
                 chunk_chars: int = CHUNK_CHARS,
                 chunk_overlap: int = CHUNK_OVERLAP) -> None:
        self.job_id = job_id
        self.profile = profile
        self.root = Path(working_root)
        self.ledger = ledger
        self.disabled = disabled or set()
        self.chunk_chars = chunk_chars
        self.chunk_overlap = chunk_overlap

    # -- resolution against job state --------------------------------------

    def _tabular_files(self) -> dict[str, object]:
        return {f.path: f.tabular for f in self.profile.files if f.tabular}

    def known_fields(self) -> dict[str, str]:
        """field name -> artefact path. The only field names a probe may use."""
        out: dict[str, str] = {}
        for path, tab in self._tabular_files().items():
            for col in tab.columns:
                out.setdefault(col.name, path)
        return out

    def known_artefacts(self) -> list[str]:
        return [f.path for f in self.profile.files]

    def _resolve_field(self, name: str | None) -> tuple[str, str]:
        if not name:
            raise ProbeRefused("this probe requires a field name")
        fields = self.known_fields()
        if name not in fields:
            raise ProbeRefused(
                f"field {name!r} is not in the structural profile for this job. "
                f"Known fields: {sorted(fields)[:20]}"
            )
        return name, fields[name]

    def _resolve_artefact(self, name: str | None) -> Path:
        if not name:
            raise ProbeRefused("this probe requires an artefact reference")
        if name not in self.known_artefacts():
            # Rejecting by membership rather than by inspecting the string is
            # what makes traversal unreachable: there is no path to sanitise.
            raise ProbeRefused(
                f"{name!r} is not a registered artefact of this job. Probes name "
                "artefacts from job state, never paths. "
                f"Registered: {self.known_artefacts()[:20]}"
            )
        return self.root / name

    # -- execution ---------------------------------------------------------

    def run(self, request: ProbeRequest, *, classification: SensitivityClass,
            backend: str, residency: Residency) -> ProbeResult:
        if request.kind in self.disabled:
            raise ProbeRefused(
                f"probe {request.kind.value!r} is disabled by local policy"
            )
        handler = getattr(self, f"_run_{request.kind.value}")
        returned, payload, clamped = handler(request)

        self.ledger.record(
            job_id=self.job_id,
            artefact_uri=self._artefact_for(request),
            kind=RELEASE_KIND[request.kind],
            content=payload,
            classification=classification,
            backend=backend,
            residency=residency,
            detail=f"{request.kind.value}: {request.field or request.artefact or ''}"[:256],
        )
        return ProbeResult(request=request, returned=returned,
                           byte_count=len(payload) if request.kind in VALUE_RETURNING else 0,
                           clamped_from=clamped)

    def _artefact_for(self, request: ProbeRequest) -> str:
        if request.kind is ProbeKind.READ_CHUNK:
            return request.artefact or "?"
        _, path = self._resolve_field(request.field)
        return path

    def _column_values(self, field: str) -> list[str]:
        name, path = self._resolve_field(field)
        raw = (self.root / path).read_text(encoding="utf-8", errors="replace")
        tab = self._tabular_files()[path]
        reader = csv.reader(io.StringIO(raw), delimiter=tab.delimiter)
        rows = list(reader)
        if not rows:
            return []
        header = rows[0] if tab.has_header else [c.name for c in tab.columns]
        body = rows[1:] if tab.has_header else rows
        idx = header.index(name)
        return [r[idx] for r in body if idx < len(r)]

    def _run_sample_field(self, request: ProbeRequest):
        cap = self.ledger.budget.max_sample_values
        n = min(request.n, cap)
        clamped = request.n if n < request.n else None
        values = [v for v in self._column_values(request.field) if v.strip()][:n]
        return values, "\n".join(values).encode("utf-8"), clamped

    def _run_distinct_count(self, request: ProbeRequest):
        values = [v for v in self._column_values(request.field) if v.strip()]
        return {"distinct": len(set(values)), "total": len(values)}, b"", None

    def _run_value_shapes(self, request: ProbeRequest):
        values = [v for v in self._column_values(request.field) if v.strip()]
        shapes: dict[str, int] = {}
        for v in values:
            shapes[_shape_of(v)] = shapes.get(_shape_of(v), 0) + 1
        top = dict(sorted(shapes.items(), key=lambda kv: -kv[1])[:8])
        return top, b"", None

    def _run_null_pattern(self, request: ProbeRequest):
        values = self._column_values(request.field)
        nulls = sum(1 for v in values if not v.strip())
        return {"null": nulls, "total": len(values)}, b"", None

    def _run_cross_tab(self, request: ProbeRequest):
        """Co-occurrence counts. Small cells are the re-identification finding.

        Returns counts only. The combination that appears once is what matters,
        and its size is visible without its content.
        """
        a = self._column_values(request.field)
        b = self._column_values(request.field_b)
        pairs = list(zip(a, b))
        counts: dict[tuple[str, str], int] = {}
        for pair in pairs:
            counts[pair] = counts.get(pair, 0) + 1
        sizes: dict[str, int] = {}
        for size in counts.values():
            sizes[str(size)] = sizes.get(str(size), 0) + 1
        return {
            "combinations": len(counts),
            "rows": len(pairs),
            "cell_size_distribution": dict(sorted(sizes.items(), key=lambda kv: int(kv[0]))),
            "singleton_combinations": sum(1 for c in counts.values() if c == 1),
        }, b"", None

    def _run_read_chunk(self, request: ProbeRequest):
        path = self._resolve_artefact(request.artefact)
        text = path.read_text(encoding="utf-8", errors="replace")
        chunks = chunk_text(text, self.chunk_chars, self.chunk_overlap)
        if request.index >= len(chunks):
            raise ProbeRefused(
                f"artefact {request.artefact!r} has {len(chunks)} chunks; "
                f"index {request.index} does not exist"
            )
        chunk = chunks[request.index]
        return ({"index": request.index, "of": len(chunks), "text": chunk},
                chunk.encode("utf-8"), None)


def chunk_text(text: str, size: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split with overlap.

    Overlap matters because the disclosures this system looks for are
    cross-referential: "the only midwife" in one passage and "a village of four
    hundred" in another. A boundary falling between them would hide both.
    Overlap reduces, but does not eliminate, that risk; the correlation pass in
    the content agent is what actually addresses it.
    """
    if len(text) <= size:
        return [text]
    step = max(size - overlap, 1)
    return [text[i:i + size] for i in range(0, len(text), step) if text[i:i + size].strip()]
