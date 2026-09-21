# 03 · Describes personal data, never says "sensitive"

**What this is.** The case that produced ADR-027. The statement describes dates
of birth and home addresses and never uses the word sensitive, public or
internal.

## What should happen
- `stated_sensitivity` is **null**. The document states no level, and
  transcribing one that is not there would be an invention.
- `inferred_sensitivity` is **sensitive**, with indicators naming what produced
  the reading: dates of birth, home addresses.
- The confirmation screen shows both, distinctly.

## What would be a failure
- Reporting a stated level. Four model families all got this right in earlier
  testing; a regression here is worth knowing about immediately.
- Reporting nothing at all, so the material proceeds as though unclassified.
- An inference with no indicators — a reading nobody can check.

## What we are trying to learn
Whether the two-field split survives contact with a statement written the way
researchers actually write them: describing the data rather than labelling it.
