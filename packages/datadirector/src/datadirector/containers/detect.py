"""Format detection across registered container plugins.

Order matters: self-describing formats are tried before generic ones, since a
BagIt archive is also a valid zip and treating it as a plain zip would discard
the metadata and checksums the depositor supplied.
"""

from __future__ import annotations

from pathlib import Path

from .archive import TarContainer, ZipContainer
from .selfdescribing import BagItContainer, RoCrateContainer

DEFAULT_FORMATS = [
    RoCrateContainer(), BagItContainer(), ZipContainer(), TarContainer(),
]


def detect(path: Path, formats=None):
    """Return the first format that recognises the file, or None."""
    for fmt in (formats if formats is not None else DEFAULT_FORMATS):
        try:
            if fmt.detects(path):
                return fmt
        except Exception:
            continue
    return None
