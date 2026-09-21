"""The watched folder.

Document A §8.4. The primary entry point, because it requires nothing of the
researcher beyond putting files somewhere, which matters for P12.

The non-obvious part: a file appearing is not a file that has finished being
written. A large upload appears immediately and completes minutes later. So the
watcher waits for size and modification time to stabilise across an interval,
and treats a directory as one candidate submission rather than reacting per file.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Candidate:
    """One submission: a single file, or a directory treated as a whole."""

    path: Path
    is_directory: bool
    total_bytes: int


def _signature(path: Path) -> tuple[int, float, int]:
    """Size, newest mtime and file count. Changing between polls means busy."""
    if path.is_file():
        st = path.stat()
        return st.st_size, st.st_mtime, 1
    total, newest, count = 0, 0.0, 0
    for p in path.rglob("*"):
        if p.is_file():
            st = p.stat()
            total += st.st_size
            newest = max(newest, st.st_mtime)
            count += 1
    return total, newest, count


class WatchedFolder:
    SERVES = ("P12",)
    def __init__(self, root: Path | str, *, quiet_seconds: float = 5.0) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.quiet_seconds = quiet_seconds
        self._seen: set[str] = set()

    def candidates(self) -> list[Candidate]:
        """Top-level entries not yet claimed. A directory is one candidate."""
        out = []
        for p in sorted(self.root.iterdir()):
            if p.name.startswith(".") or p.name in self._seen:
                continue
            total, _, _ = _signature(p)
            out.append(Candidate(path=p, is_directory=p.is_dir(), total_bytes=total))
        return out

    def is_stable(self, candidate: Candidate) -> bool:
        """Whether the candidate has stopped changing.

        A single observation cannot tell: this compares two, separated by the
        quiet interval. Callers poll rather than block on a long copy.
        """
        first = _signature(candidate.path)
        time.sleep(self.quiet_seconds)
        return first == _signature(candidate.path)

    def claim(self, candidate: Candidate) -> None:
        """Mark a candidate as taken, so polling does not re-ingest it."""
        self._seen.add(candidate.path.name)
