# 09 · An archive inside the archive

**What this is.** A submission containing another archive, as happens when data
arrives one zip per site or per year.

## What should happen
- The inner archive is **reported as received and not opened**.
- It appears in the ingestion record under `nested_archives` and as an
  undetermined item.
- It is not unpacked recursively.

## What would be a failure
- Silence. Material received and not inspected must not look like material
  inspected and found unremarkable.
- Recursive unpacking, which is how a small upload becomes a full disk.

## What we are trying to learn
Whether the check that replaced `assert_no_nested_archive` — a function that
returned in both branches and was called from nowhere — actually reports.

**To build the submission:** zip `data/` *and* `inner.zip` together.
