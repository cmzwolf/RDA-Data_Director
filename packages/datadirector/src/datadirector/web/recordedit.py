# The metadata editor's record helpers: a draft into a form, and a form back
# into a new draft.
#
# The web must show a researcher what will actually be published and let them
# correct the machine's draft. Two rules hold here. Revision produces a new
# record, and only what changed is attributed to the person: the CanonicalRecord
# is frozen so an approval can be compared against what was displayed, so the
# editor builds a new one and stamps a field researcher-supplied only where the
# value actually differs. And the findings shown are the findings the deposit is
# checked against, read back rather than recomputed, so nobody is refused on a
# basis they were not first shown.

from __future__ import annotations

import re


def resource_types():
    # The resource types a depositor may choose, read off the contract rather
    # than restated here: a hand-written list drifts from the enum the moment a
    # type is added, and the screen would quietly offer an outdated set.
    from datadirector_contracts import ResourceType
    return [member.value for member in ResourceType]


def drafted_record(service, job_id):
    # The record currently drafted, or nothing.
    from ..job_handle import latest_drafted_record
    return latest_drafted_record(service.events(job_id))


def split_findings(findings):
    # Blocking errors separated from advisory warnings and information. An error
    # is what the repository refuses and the publication agent refuses on;
    # everything else is reported but does not stop a publish. Merging them would
    # send a researcher to fix things that need no fixing while obscuring the one
    # that does.
    blocking = [f for f in findings if f.severity == "error"]
    advisory = [f for f in findings if f.severity != "error"]
    return blocking, advisory


def record_form(record):
    # The drafted record flattened into the fields the editor prefills, from what
    # the agents produced, so the researcher corrects and completes the machine's
    # draft rather than starting from a blank page.
    from datadirector_contracts import DescriptionKind

    def text_of(kind):
        for description in (record.descriptions if record else []):
            if description.kind == kind:
                return description.text
        return ""

    creators = [{"name": c.name, "orcid": c.orcid or "",
                  "given_name": c.given_name or "",
                  "family_name": c.family_name or ""}
                 for c in (record.creators if record else [])]
    rights = record.rights if record else None
    return {
         "title": record.title if record else "",
         "abstract": text_of(DescriptionKind.ABSTRACT),
         "methods": text_of(DescriptionKind.METHODS),
         "publication_year": (record.publication_year if record
                              and record.publication_year else ""),
         "publisher": (record.publisher if record and record.publisher else ""),
         "resource_type": (record.resource_type.value if record else "Dataset"),
         "language": (record.language if record and record.language else ""),
         "licence_id": (rights.licence_id if rights else ""),
         "licence_uri": (rights.uri if rights else ""),
         "licence_statement": (rights.statement if rights else ""),
         "subjects": ", ".join(s.term
                               for s in (record.subjects if record else [])),
         "creators": creators,
     }


def _parse_creator_rows(form):
    # Creators from the editor's indexed rows, dropping those left blank. Indexed
    # rows because a creator is structured (a name, optionally an ORCID) and P5
    # keeps the identifier separate from the display form. A blank row is an
    # unused slot, not a person named "".
    from datadirector_contracts import Creator
    try:
        count = int(form.get("creator_count") or "0")
    except (TypeError, ValueError):
        count = 0
    out = []
    for index in range(count):
        name = (form.get(f"creator_{index}_name") or "").strip()
        if not name:
            continue
        out.append(Creator(
            name=name,
            orcid=(form.get(f"creator_{index}_orcid") or "").strip() or None,
            given_name=(form.get(f"creator_{index}_given") or "").strip() or None,
            family_name=(form.get(f"creator_{index}_family") or "").strip()
                         or None))
    return out


def _parse_subjects(text, existing):
    # Keywords as subject terms, preserving the grounding of any kept as-is. A
    # term the agents grounded (uri and scheme) stays grounded if the researcher
    # kept the same word: re-typing a keyword must not silently downgrade a
    # controlled term to free text, which would lose the grounding R3 asked for.
    from datadirector_contracts import Subject
    grounded = {s.term.lower(): s for s in existing}
    out = []
    for raw in re.split(r"[,\n]", text or ""):
        term = raw.strip()
        if not term:
            continue
        match = grounded.get(term.lower())
        out.append(match if match is not None else Subject(term=term))
    return out


def revised_record(existing, form, who):
     # A new record from the editor's form, keeping what the form never showed.
     # A field is stamped researcher-supplied only where its value actually
     # differs from the draft; one left alone keeps the provenance the agents
     # recorded. Claiming every field as the researcher's because they opened the
     # page would misattribute a machine's inferred value to a person (C14).
    from datadirector_contracts import (
        CanonicalRecord, Description, DescriptionKind, FieldOrigin,
        FieldProvenance, ResourceType, Rights,
    )

    base = existing or CanonicalRecord(title="")
    provenance = dict(base.provenance)

    def stamp(path, new, old):
        if new != old:
            provenance[path] = FieldProvenance(
                field_path=path, origin=FieldOrigin.RESEARCHER_SUPPLIED,
                contributed_by=who.orcid)

    title = (form.get("title") or "").strip() or base.title
    stamp("title", title, base.title)

    creators = _parse_creator_rows(form)
    stamp("creators", creators, list(base.creators))

    abstract = (form.get("abstract") or "").strip()
    methods = (form.get("methods") or "").strip()
    descriptions = [d for d in base.descriptions
                     if d.kind not in (DescriptionKind.ABSTRACT,
                                        DescriptionKind.METHODS)]
    if abstract:
        descriptions.append(Description(text=abstract,
                                         kind=DescriptionKind.ABSTRACT))
    if methods:
        descriptions.append(Description(text=methods, kind=DescriptionKind.METHODS))
    stamp("descriptions", descriptions, list(base.descriptions))

    subjects = _parse_subjects(form.get("subjects"), base.subjects)
    stamp("subjects", subjects, list(base.subjects))

    licence_id = (form.get("licence_id") or "").strip()
    if licence_id:
        rights = Rights(
            licence_id=licence_id,
            uri=(form.get("licence_uri") or "").strip() or None,
            statement=(form.get("licence_statement") or "").strip() or None)
    else:
        rights = base.rights
    stamp("rights", rights, base.rights)

    language = (form.get("language") or "").strip() or base.language
    stamp("language", language, base.language)

    raw_year = (form.get("publication_year") or "").strip()
    publication_year = base.publication_year
    if re.fullmatch(r"[0-9]{4}", raw_year or ""):
        publication_year = int(raw_year)
    stamp("publication_year", publication_year, base.publication_year)

    publisher = (form.get("publisher") or "").strip() or base.publisher
    stamp("publisher", publisher, base.publisher)

    try:
        resource_type = ResourceType((form.get("resource_type") or "").strip())
    except ValueError:
        resource_type = base.resource_type
    stamp("resource_type", resource_type, base.resource_type)

    return CanonicalRecord(
        title=title, creators=creators, publication_year=publication_year,
        publisher=publisher, resource_type=resource_type,
        descriptions=descriptions, subjects=subjects,
        contributors=list(base.contributors), rights=rights,
        dates=list(base.dates), related=list(base.related),
        funding=list(base.funding), language=language, version=base.version,
        formats=list(base.formats), sizes=list(base.sizes),
        provenance=provenance)




def editor_notes(service, job_id):
       # Why a field on the review screen is empty, in the words the log holds.
       # An empty text box tells a researcher nothing: they cannot tell whether
       # the depositor never said it, whether a drafted README section failed to
       # reach the record, or whether the draft simply stopped. The agents record
       # which of those happened, so the screen says it plainly instead of leaving
       # the researcher to work out which box is theirs to fill.
    from datadirector_contracts import DescriptionKind, EventKind
    from ..job_handle import latest_documentation, latest_drafted_record

    events = service.events(job_id)
    record = latest_drafted_record(events)
    if record is None:
        return {}
    documentation = latest_documentation(events) or {}

    def normalise(heading):
         # The same reduction the documentation agent applies to its own
         # headings: whether a drafted section counts as the record's
         # description cannot depend on whether the model wrote
         # "Collection and methods" or "Collection-and-methods".
        return "".join(character for character in str(heading).lower()
                            if character.isalnum())

    headings = {normalise(section.get("heading", ""))
                   for section in documentation.get("sections") or []}
    drafted_from = set()
    for event in reversed(events):
        if event.kind is EventKind.METADATA_DRAFTED:
            drafted_from = {str(value)
                               for value in
                                (event.payload.get("drafted_from") or [])}
            break

    def missing(kind, label, sections, consequence):
        if any(description.kind == kind
                  for description in record.descriptions):
            return None
        if not drafted_from:
            return (f"Nothing has been stated about this material, so no "
                       f"{label} could be drafted. A statement, the claims "
                       f"confirmed from it and the plan are what the draft "
                       f"is written from.")
        wanted = {normalise(entry) for entry in sections}
        if documentation and not (headings & wanted):
            return (f"The README has no {label} section, so this stays the "
                       f"depositor's to write. {consequence}")
        return f"No {label} was drafted. {consequence}"

    notes = {}
    for key, kind, label, sections, consequence in (
             ("abstract", DescriptionKind.ABSTRACT, "overview",
                 ("overview", "description", "summary", "abstract",
                     "context", "about", "what the data are"),
                 "A dataset published without a description is published "
                 "unreadable to anyone who did not collect it."),
             ("methods", DescriptionKind.METHODS, "methods",
                 ("methods", "collection", "collection and methods",
                     "data collection", "collection methods", "procedures",
                     "sampling", "how the data were collected"),
                 "Without it a reader cannot judge how the data were "
                 "obtained or trust them.")):
        note = missing(kind, label, sections, consequence)
        if note:
            notes[key] = note
    if not record.subjects:
        notes["keywords"] = (
             "No keyword reached the record. A term invented for the occasion "
             "would look controlled and not be one.")
    rights = record.rights
    if rights is None or not (rights.licence_id or rights.statement):
        notes["licence"] = (
             "No licence has been chosen, and the repository will refuse the "
             "deposit without one: only the depositor may decide how the data "
             "are shared.")
    return notes
