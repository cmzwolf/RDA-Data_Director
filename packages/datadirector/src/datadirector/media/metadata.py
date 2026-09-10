"""Deterministic extraction of embedded metadata. No model.

Cluster 3 Part D, first tier. Cheap, always available, and frequently where the
disclosure actually is: a photograph of a field site carries the coordinates of
the field site, and a PDF carries the name of whoever's machine produced it.

Findings are *descriptions*, never values. "GPS coordinates present" and not the
coordinates: the finding is that the file locates something, and repeating the
location into the provenance record would defeat the purpose of noticing.

Parsed with the standard library only. These are attacker-supplied files, and a
metadata parser is a parser like any other; the fewer of them in the trusted
path, the better. Depth is traded for not adding dependencies that must then be
patched.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

# JPEG APP1/Exif markers, read positionally rather than by decoding the image.
_EXIF_GPS_IFD = 0x8825
_EXIF_DATETIME = 0x0132
_EXIF_MAKE = 0x010F
_EXIF_MODEL = 0x0110
_EXIF_ARTIST = 0x013B
_EXIF_SOFTWARE = 0x0131

_INTERESTING_EXIF = {
    _EXIF_GPS_IFD: "GPS coordinates present: the file records where it was taken",
    _EXIF_DATETIME: "capture timestamp present",
    _EXIF_MAKE: "camera manufacturer recorded",
    _EXIF_MODEL: "camera model recorded: identifies the device",
    _EXIF_ARTIST: "artist or author field populated: may name a person",
    _EXIF_SOFTWARE: "processing software recorded",
}

_PDF_FIELDS = {
    b"/Author": "PDF author field populated: may name a person",
    b"/Creator": "PDF creator application recorded",
    b"/Producer": "PDF producer recorded",
    b"/Title": "PDF title field populated",
}

_OFFICE_PARTS = {
    "docProps/core.xml": "document properties present: creator and revision history",
    "docProps/app.xml": "application properties present",
}


def _jpeg_exif(path: Path) -> list[str]:
    found: list[str] = []
    with open(path, "rb") as fh:
        if fh.read(2) != b"\xff\xd8":
            return found
        blob = fh.read(256 * 1024)
    idx = blob.find(b"Exif\x00\x00")
    if idx < 0:
        return found
    tiff = blob[idx + 6:]
    if len(tiff) < 8:
        return found
    endian = "<" if tiff[:2] == b"II" else ">"
    try:
        offset = struct.unpack(endian + "I", tiff[4:8])[0]
        count = struct.unpack(endian + "H", tiff[offset:offset + 2])[0]
    except (struct.error, IndexError):
        return found
    for i in range(min(count, 128)):
        entry = offset + 2 + i * 12
        try:
            tag = struct.unpack(endian + "H", tiff[entry:entry + 2])[0]
        except (struct.error, IndexError):
            break
        if tag in _INTERESTING_EXIF:
            found.append(_INTERESTING_EXIF[tag])
    return found


def _png_text(path: Path) -> list[str]:
    found: list[str] = []
    with open(path, "rb") as fh:
        if fh.read(8) != b"\x89PNG\r\n\x1a\n":
            return found
        blob = fh.read(256 * 1024)
    for marker, description in ((b"tEXt", "PNG text chunk present"),
                                (b"iTXt", "PNG international text chunk present"),
                                (b"eXIf", "PNG Exif chunk present")):
        if marker in blob:
            found.append(description)
    return found


def _pdf_info(path: Path) -> list[str]:
    found: list[str] = []
    blob = path.read_bytes()[:512 * 1024]
    for field, description in _PDF_FIELDS.items():
        if field in blob:
            found.append(description)
    if b"/Encrypt" in blob:
        found.append("PDF is encrypted")
    return found


def _office_props(path: Path) -> list[str]:
    found: list[str] = []
    if not zipfile.is_zipfile(path):
        return found
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        return found
    for part, description in _OFFICE_PARTS.items():
        if part in names:
            found.append(description)
    return found


def _id3(path: Path) -> list[str]:
    with open(path, "rb") as fh:
        head = fh.read(10)
    if head[:3] == b"ID3":
        return ["ID3 tags present: may carry recording details or contributor names"]
    return []


def _dicom(path: Path) -> list[str]:
    """DICOM carries patient identity by design, so its presence is the finding."""
    with open(path, "rb") as fh:
        fh.seek(128)
        if fh.read(4) != b"DICM":
            return []
    return [
        "DICOM file: the format carries patient name, identifier and birth date "
        "in its header by design, and these are present unless deliberately removed"
    ]


EXTRACTORS = {
    ".jpg": _jpeg_exif, ".jpeg": _jpeg_exif, ".tif": _jpeg_exif, ".tiff": _jpeg_exif,
    ".png": _png_text,
    ".pdf": _pdf_info,
    ".docx": _office_props, ".xlsx": _office_props, ".pptx": _office_props,
    ".mp3": _id3, ".wav": _id3,
    ".dcm": _dicom,
}


def extract(path: Path) -> list[str]:
    """Describe embedded metadata of concern. Never returns a value."""
    extractor = EXTRACTORS.get(path.suffix.lower())
    if extractor is None:
        # DICOM is often extensionless, and its magic is cheap to check.
        try:
            return _dicom(path)
        except OSError:
            return []
    try:
        return extractor(path)
    except (OSError, struct.error, ValueError):
        return []


def looks_encrypted(path: Path) -> bool:
    try:
        blob = path.read_bytes()[:8192]
    except OSError:
        return False
    if path.suffix.lower() == ".pdf" and b"/Encrypt" in blob:
        return True
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as zf:
                return any(i.flag_bits & 0x1 for i in zf.infolist())
        except zipfile.BadZipFile:
            return False
    return False


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".bmp", ".webp"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}
UNREADABLE_SUFFIXES = {".fits", ".hdf5", ".h5", ".nc", ".mat", ".sav", ".dta", ".rdata"}


def medium_of(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in UNREADABLE_SUFFIXES:
        return "instrument"
    return None
