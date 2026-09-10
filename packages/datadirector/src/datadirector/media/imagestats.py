"""Deterministic structure estimates for images. No model.

Cluster 3 Part D. This exists because of a failure observed in live testing: a
vision model was shown a rendered consent form carrying a name, a date of birth
and a telephone number, and reported "a uniform blank near-white field with no
visible content". The image contained roughly twelve thousand dark pixels. The
model was almost certainly downscaling it below the resolution at which 22-pixel
text survives.

The finding came back as `tier=content, presumed=False, sensitivity=public`,
which reads as *inspected and found clean*. That is the false assurance §9.5
exists to prevent, arriving through a door the design had not anticipated: not
"we did not look" but "we looked and could not resolve".

A model reporting a blank image for one with substantial dark structure is
detectably wrong, and detecting it needs no model. These estimates are
deliberately crude — ink coverage and row transitions — because their only job
is to contradict a claim of emptiness, not to read anything.

Pillow is used where installed. Where it is not, the check reports "unknown" and
the contradiction is not made: a missing optional dependency must not cause an
image to be trusted less *or* more than it would be otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Fraction of pixels darker than mid-grey above which an image is not blank.
INK_THRESHOLD = 0.002

# Mean transitions per row above which an image contains fine structure such as
# text rather than large flat regions.
TRANSITION_THRESHOLD = 2.0

DARK = 128


@dataclass(frozen=True)
class ImageStructure:
    available: bool
    width: int = 0
    height: int = 0
    ink_fraction: float = 0.0
    mean_row_transitions: float = 0.0

    @property
    def has_substantial_content(self) -> bool:
        """Whether the image plainly is not blank.

        Deliberately conservative: it answers "could a competent reader see
        something here", not "what is here".
        """
        if not self.available:
            return False
        return (self.ink_fraction >= INK_THRESHOLD
                and self.mean_row_transitions >= TRANSITION_THRESHOLD)

    @property
    def looks_like_text(self) -> bool:
        """Fine repeated horizontal structure, characteristic of lines of text."""
        if not self.available:
            return False
        return self.mean_row_transitions >= 6.0 and self.ink_fraction >= 0.005


def measure(path: Path, *, max_side: int = 1200) -> ImageStructure:
    try:
        from PIL import Image
    except ImportError:
        return ImageStructure(available=False)

    try:
        with Image.open(path) as image:
            image = image.convert("L")
            if max(image.size) > max_side:
                scale = max_side / max(image.size)
                image = image.resize((max(int(image.width * scale), 1),
                                      max(int(image.height * scale), 1)))
            width, height = image.size
            # tobytes() rather than getdata(): the latter is deprecated in
            # Pillow 14, and for a single-band image the bytes are the pixels.
            pixels = image.tobytes()
    except Exception:
        return ImageStructure(available=False)

    if not pixels:
        return ImageStructure(available=False)

    dark = sum(1 for p in pixels if p < DARK)
    transitions = 0
    for y in range(height):
        row = pixels[y * width:(y + 1) * width]
        previous = row[0] < DARK if row else False
        for value in row[1:]:
            current = value < DARK
            if current != previous:
                transitions += 1
                previous = current
    return ImageStructure(
        available=True, width=width, height=height,
        ink_fraction=dark / len(pixels),
        mean_row_transitions=transitions / max(height, 1),
    )
