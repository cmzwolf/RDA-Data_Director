"""re3data registry driver: which repository should this go to?

Requirement R1. re3data indexes several thousand research data repositories with
an open API and no credentials, which FAIRsharing's does require. Where a
deployment has FAIRsharing access, a second driver can implement the same
protocol; nothing here assumes one registry.

The driver **finds candidates and reports what it knows about them**. Choosing
is a decision with funder, journal and institutional consequences, and it is
made by a person: the recommendation agent surfaces the shortlist and the
reasons, and the depositor selects.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx
from datadirector_contracts import CapabilityManifest

from ..errors import ExternalServiceError

RE3DATA = "https://www.re3data.org/api/v1"
NS = {"r3d": "http://www.re3data.org/schema/2-2"}


class Re3dataRegistry:
    SERVES = ("R1",)
    def __init__(self, *, base_url: str = RE3DATA, timeout: float = 25.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="registry-re3data", version="0.1.0", protocol="RegistryDriver",
            requirements_supported=list(self.SERVES), offline_capable=False,
            requires_network=True,
        )

    def find_repositories(self, *, discipline: str | None = None,
                          limit: int = 10, **criteria) -> list[dict]:
        query = discipline or criteria.get("query") or ""
        try:
            response = httpx.get(f"{self.base_url}/repositories",
                                 params={"query": query} if query else {},
                                 timeout=self.timeout)
            response.raise_for_status()
            root = ET.fromstring(response.text)
        except Exception as exc:
            raise ExternalServiceError(
                f"the repository registry at {self.base_url} could not be "
                f"reached ({type(exc).__name__}); repository candidates cannot "
                "be listed while it is unavailable"
            ) from None

        out = []
        for entry in root.findall("repository")[:limit]:
            out.append({
                "id": _text(entry, "id"),
                "name": _text(entry, "name"),
                "doi": _text(entry, "doi"),
                "link": _text(entry, "link"),
            })
        return out

    def describe(self, repository_id: str) -> dict:
        """Everything the registry knows, for the fields a choice turns on."""
        try:
            response = httpx.get(f"{self.base_url}/repository/{repository_id}",
                                 timeout=self.timeout)
            response.raise_for_status()
            root = ET.fromstring(response.text)
        except Exception as exc:
            raise ExternalServiceError(
                f"cannot describe repository {repository_id} "
                f"({type(exc).__name__})"
            ) from None
        return parse_repository(root)


def parse_repository(root: ET.Element) -> dict:
    """Pull the fields a repository choice actually turns on.

    Namespaced and unnamespaced documents are both accepted: the registry has
    changed schema versions before, and a parser that breaks on the next change
    would take the recommendation with it.
    """
    def find_all(tag: str) -> list[str]:
        values = [e.text for e in root.iter() if _localname(e.tag) == tag and e.text]
        return [v.strip() for v in values if v and v.strip()]

    def first(tag: str) -> str | None:
        values = find_all(tag)
        return values[0] if values else None

    return {
        "id": first("re3data.orgIdentifier") or first("id"),
        "name": first("repositoryName") or first("name"),
        "url": first("repositoryURL") or first("link"),
        "description": first("description"),
        "subjects": find_all("subject"),
        "content_types": find_all("contentType"),
        "pid_systems": find_all("pidSystem"),
        "access_types": find_all("databaseAccessType"),
        "data_licenses": find_all("dataLicenseName"),
        "certificates": find_all("certificate"),
        "api_types": find_all("api"),
        "policies": find_all("policyName"),
    }


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(entry: ET.Element, tag: str) -> str | None:
    for child in entry.iter():
        if _localname(child.tag) == tag and child.text:
            return child.text.strip()
    return None
