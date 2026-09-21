"""Containers that carry their own metadata: BagIt and RO-Crate.

Where a depositor has already done descriptive work, it is read rather than
re-derived worse (Document A §8.4). Both formats are zip archives with marker
files, so extraction reuses the guarded zip path and only interpretation differs.

BagIt additionally supplies per-file checksums, which we verify against our own.
A mismatch is reported rather than repaired: it means the archive is not what
its manifest says it is, and that is a finding for the depositor.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from datadirector_contracts import CapabilityManifest
from datadirector_contracts.containers import ContainerProfile, ExtractionLimits

from .archive import ZipContainer

RO_CRATE_MARKER = "ro-crate-metadata.json"
BAGIT_MARKER = "bagit.txt"


def _names(path: Path) -> list[str]:
    if not zipfile.is_zipfile(path):
        return []
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


class RoCrateContainer:
    SERVES = ("P3", "P6")
    format_id = "ro-crate"

    def __init__(self) -> None:
        self._zip = ZipContainer()

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="container-ro-crate", version="0.1.0", protocol="ContainerFormat",
            schemas_emitted=["RO-Crate"], offline_capable=True, requires_network=False,
        )

    def detects(self, path: Path) -> bool:
        return any(Path(n).name == RO_CRATE_MARKER for n in _names(path))

    def extract(self, path: Path, destination: Path,
                limits: ExtractionLimits) -> ContainerProfile:
        profile = self._zip.extract(path, destination, limits)
        declared = None
        for member in profile.members:
            if Path(member.path).name == RO_CRATE_MARKER:
                try:
                    declared = json.loads(
                        (destination / member.path).read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    declared = None
                break
        return profile.model_copy(update={
            "format_id": self.format_id, "declared_metadata": declared,
        })


class BagItContainer:
    SERVES = ("P3", "P6")
    format_id = "bagit"

    def __init__(self) -> None:
        self._zip = ZipContainer()

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="container-bagit", version="0.1.0", protocol="ContainerFormat",
            offline_capable=True, requires_network=False,
        )

    def detects(self, path: Path) -> bool:
        return any(Path(n).name == BAGIT_MARKER for n in _names(path))

    def extract(self, path: Path, destination: Path,
                limits: ExtractionLimits) -> ContainerProfile:
        profile = self._zip.extract(path, destination, limits)
        declared: dict = {}
        supplied: dict[str, str] = {}
        for member in profile.members:
            name = Path(member.path).name
            target = destination / member.path
            if name == "bag-info.txt":
                declared = self._parse_tags(target)
            elif name.startswith("manifest-sha256"):
                supplied.update(self._parse_manifest(target))

        verified: bool | None = None
        if supplied:
            ours = {m.path: m.digest.value for m in profile.members}
            verified = all(
                ours.get(p.lstrip("./")) == d or ours.get(p) == d
                for p, d in supplied.items()
            )
        return profile.model_copy(update={
            "format_id": self.format_id,
            "declared_metadata": declared or None,
            "checksums_verified": verified,
        })

    @staticmethod
    def _parse_tags(path: Path) -> dict:
        out: dict[str, str] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    out[k.strip()] = v.strip()
        except OSError:
            pass
        return out

    @staticmethod
    def _parse_manifest(path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    out[parts[1].strip()] = parts[0].strip()
        except OSError:
            pass
        return out
