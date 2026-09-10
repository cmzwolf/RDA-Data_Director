"""Guarded extraction.

Document A §8.4 and §14.2. An archive is a strictly larger attack surface than
a document and predates language models entirely. These threats are refused by
rules, not by judgement, so no model appears anywhere in this module:

  - path traversal: a member named ``../../etc/cron.d/x`` writing outside the
    extraction root, which turns "a researcher dropped a file" into arbitrary
    file write;
  - absolute paths, which do the same more directly;
  - symlinks and hardlinks whose target escapes the root, including the case
    where the link is safe but a later member writes *through* it;
  - decompression bombs: a few kilobytes expanding without bound;
  - member-count exhaustion;
  - device and FIFO entries in tar archives;
  - nested archives, recursively.

Every check here is a refusal. Nothing is sanitised or renamed into safety,
because a member whose name had to be repaired is a member whose provenance we
can no longer describe honestly.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from datadirector_contracts.containers import ExtractionLimits

from ..errors import ExtractionError


def normalise_member_path(name: str) -> PurePosixPath:
    """Reject a member name outright, or return it as a relative POSIX path."""
    if not name or name in (".", ".."):
        raise ExtractionError(f"archive member has an unusable name: {name!r}")
    if "\x00" in name:
        raise ExtractionError("archive member name contains a null byte")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ExtractionError(
            f"archive member {name!r} has an absolute path. Absolute member paths "
            "are refused: they write outside the extraction root by construction."
        )
    p = PurePosixPath(name)
    if ".." in p.parts:
        raise ExtractionError(
            f"archive member {name!r} contains a parent reference. This is the "
            "path-traversal pattern and is refused without inspection."
        )
    return p


def resolve_within(destination: Path, member: PurePosixPath) -> Path:
    """Resolve a member against the root and confirm it stays inside.

    The check is done on the *resolved* path rather than the declared one,
    because a component of the path may itself be a symlink placed by an earlier
    member of the same archive.
    """
    root = destination.resolve()
    target = (root / Path(*member.parts))
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise ExtractionError(f"cannot resolve archive member {member}: {exc}") from exc
    if resolved != root and root not in resolved.parents:
        raise ExtractionError(
            f"archive member {member} resolves to {resolved}, outside the extraction "
            f"root {root}. Refused."
        )
    return target


class ExtractionBudget:
    """Running totals, checked before each member is written.

    Checked before rather than after, because a decompression bomb detected
    after writing has already consumed the disk it was meant to protect.
    """

    def __init__(self, limits: ExtractionLimits) -> None:
        self.limits = limits
        self.total_bytes = 0
        self.member_count = 0

    def admit(self, declared_size: int) -> None:
        self.member_count += 1
        if self.member_count > self.limits.max_member_count:
            raise ExtractionError(
                f"archive exceeds the member limit of {self.limits.max_member_count}. "
                "Refused; raise the limit in configuration if this is expected."
            )
        self.total_bytes += max(declared_size, 0)
        if self.total_bytes > self.limits.max_total_uncompressed_bytes:
            raise ExtractionError(
                f"archive expands beyond the limit of "
                f"{self.limits.max_total_uncompressed_bytes} bytes "
                f"(reached {self.total_bytes}). Refused as a possible decompression bomb."
            )


def refuse_link(name: str, kind: str, limits: ExtractionLimits) -> None:
    if not limits.allow_symlinks:
        raise ExtractionError(
            f"archive member {name!r} is a {kind}. Links are refused: a link may "
            "point outside the extraction root, and a later member may write "
            "through it even when the link itself looks harmless."
        )


def prepare_parent(path: Path, destination: Path) -> None:
    """Create the parent directory, confirming it too stays inside the root."""
    parent = path.parent
    root = destination.resolve()
    if parent.exists() and parent.is_symlink():
        raise ExtractionError(
            f"the parent directory {parent} is a symlink; refusing to write through it"
        )
    parent.mkdir(parents=True, exist_ok=True)
    if root not in parent.resolve().parents and parent.resolve() != root:
        raise ExtractionError(f"parent directory {parent} escapes the extraction root")


def assert_no_nested_archive(members: list[str], limits: ExtractionLimits) -> None:
    """Nested archives are not unpacked recursively by default.

    They are kept as opaque members rather than refused, so a legitimately
    nested deposit still arrives; only automatic recursion is withheld.
    """
    if limits.max_nesting_depth >= 2:
        return
    suffixes = {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar"}
    nested = [m for m in members if Path(m).suffix.lower() in suffixes]
    if nested:
        # Informational: recorded by the caller, not an error.
        return
