# Operational tests

Scenarios for running the system by hand. Not an automated suite: these exist
because the offline tests measure components and a person at a terminal measures
the system, and every defect found so far surfaced within seconds of someone
actually using it.

Each directory holds data, a statement to paste, sometimes a plan and an
instruction, and a README saying **what should happen**, **what would count as a
failure**, and **what we are trying to learn**. The last matters most: a result
you cannot interpret is not evidence.

## Running one

```bash
datadirector accounts && export DD_LOCAL_ACCOUNTS=./local-accounts.txt
datadirector serve
```

Then at `http://127.0.0.1:8000/`:

1. Sign in.
2. **Submit** — zip the scenario's `data/` directory and upload it. Paste
   `instruction.txt` into the instruction box if the scenario has one, and
   `dmp.json` as a path if it has one (or add it to the zip, which is the more
   realistic case).
3. **Declaration** — paste `statement.txt`.
4. **Confirm** — read what it was understood as saying, and decide.
5. **Continue** through classification, metadata and the gate.

Or from the command line:

```bash
cd operational-tests/03-silent-on-sensitivity
zip -r /tmp/submission.zip data/
datadirector ingest /tmp/submission.zip --depositor 0009-0000-0000-0017
datadirector advance <job-id>
```

## The scenarios

| # | Scenario | The question it asks |
|---|---|---|
| 01 | Open instrument data | Is the system quiet when there is nothing to say? |
| 02 | Clearly sensitive | Does the sensitive path hold in the easy case? |
| 03 | Silent on sensitivity | Does stated-versus-inferred survive real prose? |
| 04 | Understated | Does inference tighten when the human is wrong? |
| 05 | Field notes | Is a cross-referential disclosure found? |
| 06 | Indigenous knowledge | Does CARE refer rather than judge? |
| 07 | Plan divergence | Are conflicting authorities flagged, not resolved? |
| 08 | Injected content | Does the trust boundary hold through the workflow? |
| 09 | Nested archive | Is uninspected material reported as such? |

**01 is the one people will skip and should not.** A tool that flags everything
trains its users to dismiss it, and is then useless on the day something really
is sensitive. Over-classification is a failure, not caution.

## Recording what happened

| Scenario | Model | Outcome | Matched expectation? | Notes |
|---|---|---|---|---|
| 01 | | | | |
| 02 | | | | |
| 03 | | | | |
| 04 | | | | |
| 05 | | | | |
| 06 | | | | |
| 07 | | | | |
| 08 | | | | |
| 09 | | | | |

Worth noting alongside each: how many bytes reached a model (the exposure
ledger), how long the gate took to decide, and anything the system said that was
**confidently wrong** rather than merely unhelpful. The second kind is the one
that matters.

## A caution about the data

Every name, address, identifier and diagnosis in these directories is invented.
The ORCID-shaped identifiers in `local-accounts.txt` are well formed but are
fictions, and sessions created with them record `authentication:
local-accounts`, so runs made this way stay distinguishable from real work in
the provenance record.

None of this material should reach a real repository. The scenarios deposit to
the Zenodo **sandbox** if they deposit at all.
