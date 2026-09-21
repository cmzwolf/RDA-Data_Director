"""Ontology Lookup Service vocabulary provider.

Requirement R3. OLS4 at the EBI indexes several hundred ontologies and needs no
credentials, which makes it the one vocabulary service a default deployment can
reach without an account.

The provider grounds a proposed term or reports that it could not. It never
invents a URI: a term with no match comes back as no match, which is what lets
the metadata agent report it as ungrounded (R2) rather than passing a guess off
as a controlled term.

**A search service always returns something.** Live testing against OLS asked it
for "station 14" — a column label from someone's spreadsheet — and got back a
plant species, `Oldenlandia sp. Hamersley Station`, ranked first. A caller taking
the top hit would have written that URI into a published record as a controlled
subject term, and R2's requirement to state openly where no vocabulary exists
would have become unreachable, because nothing is ever ungrounded.

Hence the separation below. `search` returns candidates and is for a human or a
model to consider. `ground` returns a term only when the match is close enough to
be a citation, and returns nothing otherwise. Agents use `ground`.
"""

from __future__ import annotations

import httpx
from datadirector_contracts import CapabilityManifest, VocabularyTerm

from ..errors import ExternalServiceError

OLS4 = "https://www.ebi.ac.uk/ols4/api"

# Ontologies worth preferring when a term matches in several. Not a filter:
# a match anywhere is still a match, but a hit in a well-governed domain
# ontology is a better citation than one in a niche application ontology.
PREFERRED = ("envo", "obi", "chebi", "go", "efo", "uo", "pato", "ncit", "iao")


class OlsVocabularyProvider:
    SERVES = ("R2", "R3")
    def __init__(self, *, base_url: str = OLS4, timeout: float = 20.0,
                 ontologies: list[str] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.ontologies = ontologies
        # Synonyms of the most recent search, keyed by URI. Held rather than
        # returned because VocabularyTerm is a contract type shared with
        # third-party providers, and widening it for one provider's convenience
        # would be the wrong direction.
        self._synonyms: dict[str, list[str]] = {}

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="vocabulary-ols", version="0.1.0",
            protocol="VocabularyProvider", requirements_supported=list(self.SERVES),
            offline_capable=False, requires_network=True,
        )

    def search(self, query: str, *, scheme: str | None = None,
               limit: int = 10) -> list[VocabularyTerm]:
        params: dict = {
            "q": query, "rows": max(limit * 3, 10), "type": "class",
            "exact": "false",
            # Synonyms are requested because they are part of the vocabulary,
            # not a fuzzy match: a term recorded as a synonym of a concept
            # denotes that concept, and refusing to ground it would report an
            # absence that does not exist.
            "fieldList": "iri,label,synonym,ontology_name,ontology_prefix,"
                         "short_form,description",
        }
        ontology = scheme or (",".join(self.ontologies) if self.ontologies else None)
        if ontology:
            params["ontology"] = ontology.lower()
        try:
            response = httpx.get(f"{self.base_url}/search", params=params,
                                 timeout=self.timeout)
            response.raise_for_status()
            docs = response.json().get("response", {}).get("docs", [])
        except Exception as exc:
            # A vocabulary service that is unreachable must not silently produce
            # "no controlled term exists": that conclusion belongs to R2 and is
            # reported to the depositor, so it must mean what it says.
            raise ExternalServiceError(
                f"the vocabulary service at {self.base_url} could not be "
                f"reached ({type(exc).__name__}). Terms cannot be grounded "
                "while it is unavailable, and reporting them as ungrounded "
                "would misstate why."
            ) from None

        terms = []
        self._synonyms = {}
        for doc in docs:
            if not doc.get("iri"):
                continue
            synonyms = [str(x) for x in (doc.get("synonym") or [])]
            self._synonyms[doc["iri"]] = synonyms
            terms.append(VocabularyTerm(
                uri=doc["iri"], label=doc.get("label") or query,
                scheme=(doc.get("ontology_prefix")
                        or doc.get("ontology_name", "")).upper(),
                definition=(doc["description"][0]
                            if isinstance(doc.get("description"), list)
                            and doc["description"] else None),
            ))
        terms.sort(key=lambda t: (
            t.label.lower() != query.lower(),           # exact label first
            _preference(t.scheme),
        ))
        return terms[:limit]

    def ground(self, term: str, *, scheme: str | None = None
               ) -> VocabularyTerm | None:
        """Return a term only if it genuinely denotes what was asked for.

        The test is on the label, not on the ranking: a search engine's first
        result is its best guess at relevance, which is a different question
        from whether the concept is the one named. Where the answer is no, the
        caller reports the term as ungrounded, which is a finding rather than a
        failure (R2).

        Deliberately strict. A near miss written into a published record is
        worse than an absence, because it is machine-readable, will be
        aggregated by others, and looks authoritative.
        """
        wanted = _normalise(term)
        candidates = self.search(term, scheme=scheme, limit=10)
        for candidate in candidates:
            if _normalise(candidate.label) == wanted:
                return candidate
        for candidate in candidates:
            if any(_normalise(s) == wanted
                   for s in self._synonyms.get(candidate.uri, [])):
                return candidate
        return None

    def suggest(self, term: str, *, limit: int = 3) -> list[VocabularyTerm]:
        """Near candidates for a term that did not ground.

        Reported to the depositor rather than discarded. "No controlled term
        exists" and "none of these three is quite it" are different findings,
        and the second is one a person can act on.
        """
        return self.search(term, limit=limit)

    def scheme_exists_for(self, domain: str) -> bool:
        """Whether any ontology covers this domain at all.

        R2 requires the system to state openly when no controlled vocabulary
        exists for a domain. Answering it needs a question about coverage, not
        about a single term.
        """
        try:
            response = httpx.get(f"{self.base_url}/ontologies",
                                 params={"size": 1, "search": domain},
                                 timeout=self.timeout)
            response.raise_for_status()
        except Exception:
            return False
        payload = response.json()
        return bool(payload.get("page", {}).get("totalElements", 0))


def _normalise(value: str) -> str:
    """Compare labels ignoring case, punctuation and spacing.

    'Air Temperature' and 'air temperature' are the same concept; 'air
    temperature measurement' is not, and the difference matters more than the
    inconvenience of missing it.
    """
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _preference(scheme: str) -> int:
    lowered = (scheme or "").lower()
    return PREFERRED.index(lowered) if lowered in PREFERRED else len(PREFERRED)
