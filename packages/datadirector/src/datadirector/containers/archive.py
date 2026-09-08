"""Zip and tar container formats.

Detection and extraction are deterministic (Document A §8.4). Both formats share
the guarded-extraction rules in `safety.py`; they differ only in how members are
enumerated and in which link types they can express.
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

from datadirector_contracts import CapabilityManifest, Digest
from datadirector_contracts.containers import (
    ContainerMember, ContainerProfile, ExtractionLimits, MemberRole,
)
from datadirector_contracts.primitives import ArtefactRef, MaterialClass

from ..errors import ExtractionError
from .safety import (
    ExtractionBudget, normalise_member_path, prepare_parent, refuse_link, resolve_within,
)

_READ_CHUNK = 1024 * 1024


def _digest_file(path: Path) -> tuple[Digest, int]:
    import hashlib

    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(_READ_CHUNK):
            h.update(chunk)
            size += len(chunk)
    return Digest(value=h.hexdigest()), size


def _archive_ref(path: Path) -> ArtefactRef:
    digest, size = _digest_file(path)
    return ArtefactRef(
        uri=f"wrk://{path.name}", material_class=MaterialClass.DATA,
        digest=digest, byte_size=size,
        description="submitted archive",
    )


class ZipContainer:
    format_id = "zip"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="container-zip", version="0.1.0", protocol="ContainerFormat",
            offline_capable=True, requires_network=False,
        )

    def detects(self, path: Path) -> bool:
        return zipfile.is_zipfile(path)

    def extract(self, path: Path, destination: Path,
                limits: ExtractionLimits) -> ContainerProfile:
        destination.mkdir(parents=True, exist_ok=True)
        budget = ExtractionBudget(limits)
        members: list[ContainerMember] = []
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # Unix mode is in the high 16 bits of external_attr; 0o120000 is a symlink.
                mode = info.external_attr >> 16
                if mode and (mode & 0o170000) == 0o120000:
                    refuse_link(info.filename, "symlink", limits)
                member = normalise_member_path(info.filename)
                budget.admit(info.file_size)
                target = resolve_within(destination, member)
                prepare_parent(target, destination)
                with zf.open(info) as src, open(target, "wb") as dst:
                    while chunk := src.read(_READ_CHUNK):
                        dst.write(chunk)
                digest, size = _digest_file(target)
                members.append(ContainerMember(
                    path=str(member), digest=digest, byte_size=size,
                    role=MemberRole.UNDETERMINED,
                ))
        return ContainerProfile(
            archive=_archive_ref(path), format_id=self.format_id, members=members,
        )


class TarContainer:
    format_id = "tar"

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="container-tar", version="0.1.0", protocol="ContainerFormat",
            offline_capable=True, requires_network=False,
        )

    def detects(self, path: Path) -> bool:
        try:
            return tarfile.is_tarfile(path)
        except (OSError, tarfile.TarError):
            return False

    def extract(self, path: Path, destination: Path,
                limits: ExtractionLimits) -> ContainerProfile:
        destination.mkdir(parents=True, exist_ok=True)
        budget = ExtractionBudget(limits)
        members: list[ContainerMember] = []
        with tarfile.open(path) as tf:
            for info in tf:
                if info.isdir():
                    continue
                if info.issym() or info.islnk():
                    refuse_link(info.name, "symlink" if info.issym() else "hardlink", limits)
                if not info.isfile():
                    raise ExtractionError(
                        f"archive member {info.name!r} is a device, FIFO or other "
                        "special entry. Only regular files are extracted."
                    )
                member = normalise_member_path(info.name)
                budget.admit(info.size)
                target = resolve_within(destination, member)
                prepare_parent(target, destination)
                src = tf.extractfile(info)
                if src is None:
                    raise ExtractionError(f"cannot read archive member {info.name!r}")
                with src, open(target, "wb") as dst:
                    while chunk := src.read(_READ_CHUNK):
                        dst.write(chunk)
                digest, size = _digest_file(target)
                members.append(ContainerMember(
                    path=str(member), digest=digest, byte_size=size,
                ))
        return ContainerProfile(
            archive=_archive_ref(path), format_id=self.format_id, members=members,
        )
