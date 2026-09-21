# 02 · Clearly sensitive, and stated as such

**What this is.** Clinic attendance records with village, condition and age.
The researcher says plainly that it is sensitive.

## What should happen
- Classified **sensitive**, and the depositor's own statement is enough to get
  there — no clever inference needed.
- Probing stays within the exposure budget; watch how many bytes reach a model.
- Redaction proposals appear, each with its evidence and a field-level diff.
- Deposit is refused until every item is decided.

## What would be a failure
- Sending this to a backend whose residency is not permitted for sensitive
  material. Check the exposure ledger for where it went.
- A redaction proposal with no evidence, or one that quotes the data rather
  than describing it.
- Bulk-accepting the gate.

## What we are trying to learn
Whether the sensitive path holds under the easy case, so that failures in the
hard cases can be attributed to the hard part.
