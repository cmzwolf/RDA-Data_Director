"""Structural profiling: describing material without disclosing it.

Document A §9.1. For a large class of metadata fields, structure is sufficient
and content is unnecessary. A profiler produces column names, inferred types,
cardinality and null patterns; it does not produce rows.

This is what bounds how much material ever reaches a model. The residual
exposure is the column names themselves, which is a far smaller surface than the
data and one a depositor can inspect before release.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

SAMPLE_ROWS = 200
MAX_CATEGORIES = 25


class ColumnProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    inferred_type: str = Field(description="integer | number | boolean | date | string")
    null_count: int = 0
    distinct_count: int | None = None
    looks_categorical: bool = False
    example_shape: str | None = Field(
        default=None,
        description="A redacted shape such as 'NNNN-NN-NN', never a value. Enough "
        "for a model to recognise a date or an identifier format without seeing one.",
    )


class TabularProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    delimiter: str
    has_header: bool
    row_count_sampled: int
    columns: list[ColumnProfile]


class FileProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    byte_size: int
    media_type: str | None = None
    encoding: str | None = None
    tabular: TabularProfile | None = None


class TreeProfile(BaseModel):
    """The structural profile of a submission, container or loose files."""

    model_config = ConfigDict(frozen=True)

    root: str
    files: list[FileProfile]
    total_bytes: int
    file_count: int


def _shape_of(value: str) -> str:
    """Reduce a value to a character-class shape.

    'N' for digits, 'A' for letters, punctuation kept. So 2019-04-01 becomes
    NNNN-NN-NN. Shapes let a model recognise dates and identifier formats
    without a single real value crossing the boundary.
    """
    out = []
    for ch in value[:32]:
        if ch.isdigit():
            out.append("N")
        elif ch.isalpha():
            out.append("A")
        else:
            out.append(ch)
    return "".join(out)


def _infer_type(values: list[str]) -> str:
    non_null = [v for v in values if v.strip() != ""]
    if not non_null:
        return "string"

    def all_match(pred) -> bool:
        return all(pred(v) for v in non_null)

    if all_match(lambda v: v.strip().lstrip("+-").isdigit()):
        return "integer"
    try:
        for v in non_null:
            float(v)
        return "number"
    except ValueError:
        pass
    if all_match(lambda v: v.strip().lower() in {"true", "false", "yes", "no", "0", "1"}):
        return "boolean"
    if all_match(lambda v: len(v) in (8, 10) and sum(c.isdigit() for c in v) >= 6):
        return "date"
    return "string"


def profile_tabular(path: Path) -> TabularProfile | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        dialect = csv.Sniffer().sniff(raw[:8192])
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    try:
        has_header = csv.Sniffer().has_header(raw[:8192])
    except csv.Error:
        has_header = True

    reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
    rows = []
    for i, row in enumerate(reader):
        if i > SAMPLE_ROWS:
            break
        rows.append(row)
    if not rows:
        return None

    header = rows[0] if has_header else [f"column_{i+1}" for i in range(len(rows[0]))]
    body = rows[1:] if has_header else rows

    columns = []
    for idx, name in enumerate(header):
        values = [r[idx] for r in body if idx < len(r)]
        distinct = {v for v in values if v.strip()}
        first = next((v for v in values if v.strip()), None)
        columns.append(ColumnProfile(
            name=name,
            inferred_type=_infer_type(values),
            null_count=sum(1 for v in values if not v.strip()),
            distinct_count=len(distinct),
            looks_categorical=0 < len(distinct) <= MAX_CATEGORIES,
            example_shape=_shape_of(first) if first else None,
        ))
    return TabularProfile(
        path=str(path.name), delimiter=delimiter, has_header=has_header,
        row_count_sampled=len(body), columns=columns,
    )


TABULAR_SUFFIXES = {".csv", ".tsv", ".txt"}
MEDIA_TYPES = {
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".json": "application/json",
    ".txt": "text/plain", ".md": "text/markdown", ".pdf": "application/pdf",
    ".png": "image/png", ".jpg": "image/jpeg", ".tif": "image/tiff",
    ".fits": "application/fits", ".xml": "application/xml", ".yaml": "application/yaml",
}


def profile_tree(root: Path) -> TreeProfile:
    """Walk a directory and profile every regular file.

    No content leaves this function. Tabular files yield column structure;
    everything else yields size, media type and encoding only.
    """
    files: list[FileProfile] = []
    total = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.name.startswith("."):
            continue
        size = path.stat().st_size
        total += size
        suffix = path.suffix.lower()
        tabular = profile_tabular(path) if suffix in TABULAR_SUFFIXES and size < 50_000_000 else None
        files.append(FileProfile(
            path=str(path.relative_to(root)),
            byte_size=size,
            media_type=MEDIA_TYPES.get(suffix),
            encoding="utf-8" if suffix in TABULAR_SUFFIXES else None,
            tabular=tabular,
        ))
    return TreeProfile(root=str(root), files=files, total_bytes=total, file_count=len(files))
