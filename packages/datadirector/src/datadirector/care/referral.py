"""CARE: detection and referral only.

Requirement R2 names the CARE principles — Collective Benefit, Authority to
Control, Responsibility, Ethics — alongside FAIR. The Blueprint is explicit
about how far a tool may go: §10.3 states that CARE compliance cannot be
assessed by checklist and that the minimum useful contribution is to identify
when CARE is likely to apply and direct the user to appropriate guidance.

**This module therefore does not assess anything.** It detects that CARE may be
engaged, says so, and points at the people whose judgement is required. An
automated CARE assessment would be a category error: the principles vest
authority in Indigenous peoples and communities, so a system that decided on
their behalf would violate the principle it claimed to check.

The referral is to Local Contexts, which administers the TK and BC Labels, and
to the Global Indigenous Data Alliance, which authored CARE.
"""

from __future__ import annotations

import re

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

LOCAL_CONTEXTS = "https://localcontexts.org/"
GIDA = "https://www.gida-global.org/care"

# Signals that CARE may be engaged. Deliberately broad and deliberately not a
# score: the output is "a person needs to look at this", and a threshold would
# imply a judgement the tool is not entitled to make.
SIGNALS: dict[str, tuple[str, ...]] = {
    "indigenous or community identity": (
        "indigenous", "aboriginal", "first nations", "first peoples", "métis",
        "inuit", "māori", "maori", "iwi", "hapū", "sámi", "sami", "adivasi",
        "native american", "american indian", "torres strait", "traditional owner",
    ),
    "traditional or community knowledge": (
        "traditional knowledge", "traditional ecological knowledge",
        "indigenous knowledge", "customary", "oral history", "oral tradition",
        "ancestral", "sacred", "ceremonial", "songline",
    ),
    "community governance or consent": (
        "community consent", "collective consent", "tribal council",
        "community protocol", "data sovereignty", "indigenous data sovereignty",
        "ocap", "tk label", "bc label", "benefit sharing", "nagoya",
    ),
    "culturally significant place or remains": (
        "sacred site", "burial", "ancestral remains", "cultural heritage site",
        "repatriation",
    ),
}


class CareAssessment(BaseModel):
    SERVES: ClassVar[tuple[str, ...]] = ("P1", "R2")
    """Whether CARE may apply, and who should be consulted.

    Note what this type does not have: a verdict, a score, a compliance flag.
    Those would all be the assessment the Blueprint says a tool must not make.
    """

    model_config = ConfigDict(frozen=True)

    may_apply: bool
    signals: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Category to the terms that raised it. Terms rather than "
        "passages: the surrounding text may itself be sensitive.",
    )
    declared_by_depositor: bool = Field(
        default=False,
        description="True where the responsibility statement said so. A "
        "depositor's own declaration is stronger evidence than any detection.",
    )

    @property
    def guidance(self) -> list[str]:
        if not self.may_apply:
            return []
        return [
            "This material may engage the CARE Principles for Indigenous Data "
            "Governance. Whether it does, and what follows if it does, is not a "
            "question this tool can answer: CARE vests authority in the peoples "
            "and communities concerned.",
            "Before depositing, consult the relevant community or governance "
            "body. Your institution's research ethics office can identify who "
            "that is if it is not already established.",
            f"Local Contexts provides TK and BC Labels and guidance on their "
            f"use: {LOCAL_CONTEXTS}",
            f"The Global Indigenous Data Alliance authored the CARE Principles "
            f"and publishes guidance on applying them: {GIDA}",
        ]


def detect(*, texts: list[str] | None = None,
           field_names: list[str] | None = None,
           declared: bool | None = None) -> CareAssessment:
    """Look for signals that CARE may be engaged.

    Deterministic and keyword-based, deliberately. A model asked to judge CARE
    applicability would be doing the assessment this module exists to avoid, and
    its output would carry an authority it has no standing to hold. A keyword
    match makes no claim beyond "this word appeared".
    """
    haystack = " ".join((texts or []) + (field_names or [])).lower()
    found: dict[str, list[str]] = {}
    for category, terms in SIGNALS.items():
        hits = [t for t in terms if re.search(rf"\b{re.escape(t)}\b", haystack)]
        if hits:
            found[category] = hits

    return CareAssessment(
        may_apply=bool(found) or bool(declared),
        signals=found,
        declared_by_depositor=bool(declared),
    )
