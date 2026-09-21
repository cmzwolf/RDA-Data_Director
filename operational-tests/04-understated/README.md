# 04 · States "public" while describing identifying data

**What this is.** A deliberate conflict. The statement asserts the data is
public and then describes material that plainly is not.

## What should happen
- `stated_sensitivity` is **public** — transcribed faithfully, because that is
  what the document says.
- `inferred_sensitivity` is **sensitive**.
- The outcome is **sensitive**: inference may only ever tighten, never relax.
- The confirmation screen shows the conflict rather than resolving it silently.

## What would be a failure
- Accepting "public" because the depositor said so. The asymmetry in §9.3 exists
  for exactly this, and it is the single most important behaviour in the system
  to get right.
- Hiding the conflict, so the researcher never sees that their own statement
  disagreed with itself.

## What we are trying to learn
Whether the tightening rule holds when the human assertion and the described
content point in opposite directions. This is the case where deferring to the
human would be wrong.
