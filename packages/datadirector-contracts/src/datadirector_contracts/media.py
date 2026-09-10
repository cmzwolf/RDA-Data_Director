"""Media inspection: images, audio and formats nothing can read.

Cluster 3 Part D. Three tiers, and the third is the one that matters
architecturally.

  metadata      deterministic extraction of embedded fields. Always available,
                cheap, and frequently where the leak actually is: a photograph
                of a field site carries the coordinates of the field site.
  content       a multimodal model, available only if a backend permitted at
                this classification declares the capability.
  uninspectable proprietary or instrument formats nothing can read.

**"Not inspected" is a recorded, surfaced state, never silence.** A reviewer who
sees no flags on a file will reasonably infer it was checked and found clean.
Preventing that inference is the point: it is the failure that would actually
harm someone, and it is invisible by construction unless the system says so.

An uninspected image or audio file is presumed **sensitive**, not unknown. A
face is personal data and a voice is biometric, independent of any content.
Only an explicit human act relaxes that, which is the §9.3 asymmetry again
rather than a new rule to learn.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .sensitivity import SensitivityClass


class InspectionTier(StrEnum):
    METADATA = "metadata"
    CONTENT = "content"
    NONE = "none"


class UninspectedReason(StrEnum):
    """Why a file was not inspected. Coded, never free text, so a deployment can
    be audited for how much of its material it is unable to look at."""

    NO_CAPABLE_BACKEND = "no-capable-backend"
    """No configured backend can process this medium at all."""

    CAPABILITY_NOT_PERMITTED = "capability-not-permitted-at-classification"
    """A capable backend exists but policy forbids it at this classification.

    The characteristic situation of a deployment holding sensitive images and
    permitting only local text models: it cannot inspect its own material, and
    should be told so rather than quietly served."""

    CONTENT_NOT_RESOLVABLE = "content-not-resolvable"
    """The model reported an empty image that deterministic measurement says is
    not empty.

    Observed with a rendered consent form: the model described a blank field
    while the image carried thousands of dark pixels of text, almost certainly
    because it downscales below the resolution at which small text survives. A
    verdict of 'nothing here' from a reader that could not resolve the content
    is not an inspection, and recording it as one produces exactly the false
    assurance this tier exists to prevent."""

    FORMAT_UNREADABLE = "format-unreadable"
    ENCRYPTED = "encrypted"
    EXCEEDS_SIZE_LIMIT = "exceeds-size-limit"


class MediaFinding(BaseModel):
    """What was learned about one non-textual artefact, and what was not."""

    model_config = ConfigDict(frozen=True)

    artefact: str
    media_type: str | None = None
    tier: InspectionTier
    uninspected_reason: UninspectedReason | None = None
    metadata_findings: list[str] = Field(
        default_factory=list,
        description="Embedded fields of concern, described rather than quoted: "
        "'GPS coordinates present', not the coordinates.",
    )
    content_findings: list[str] = Field(default_factory=list)
    sensitivity: SensitivityClass = Field(
        description="Presumed sensitive where uninspected. A face is personal "
        "data and a voice is biometric, whatever the content turns out to be.",
    )
    presumed: bool = Field(
        default=False,
        description="True where the classification rests on a presumption rather "
        "than on inspection. Surfaced at the gate: a reviewer must be able to "
        "distinguish 'checked and found sensitive' from 'never looked at'.",
    )

    def model_post_init(self, _context: object) -> None:
        if self.tier is InspectionTier.NONE and self.uninspected_reason is None:
            raise ValueError(
                "an uninspected artefact must record why. Silence about the "
                "reason is the failure this type exists to prevent."
            )
        if self.tier is InspectionTier.NONE and not self.presumed:
            raise ValueError(
                "an uninspected artefact's classification is a presumption and "
                "must be marked as one"
            )
