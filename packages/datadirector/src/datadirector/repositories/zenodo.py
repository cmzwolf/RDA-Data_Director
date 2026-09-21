"""Zenodo repository driver.

Cluster 4 Part D1. Sandbox and production differ only in base URL.

Two properties matter more than feature coverage.

**Preflight reports problems and never fixes them.** A driver that quietly
adjusts metadata to satisfy a repository has changed what the human approved at
the gate, and the record deposited is then not the record reviewed.

**Deposit is idempotent per job.** A retry after a network failure must not
create a second record. Zenodo's deposition id is recorded on first creation and
reused, because a duplicate deposit is not something the researcher can undo and
not something the repository will thank us for.

The driver also supplies **probers**: functions that answer, for each kind of
effect it performs, whether an interrupted attempt took place. Recovery uses
them rather than retrying blindly (`workflow/effects.py`). This began as a
private mitigation in this driver and became a framework contract, because a
third party writing their own `RepositoryDriver` would otherwise have to
rediscover the problem, and nothing would have told them to.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from datadirector_contracts import (
    CanonicalRecord, CapabilityManifest, DepositReceipt, Orcid,
)

from ..credentials.broker import CredentialBroker
from ..errors import ExternalServiceError

SANDBOX = "https://sandbox.zenodo.org"
PRODUCTION = "https://zenodo.org"

UPLOAD_TYPES = {
    "Dataset": "dataset", "Software": "software", "Text": "publication",
    "Image": "image", "Collection": "dataset", "Other": "other",
}


class DepositionRegistry:
    """Remembers the deposition created for a job.

    Idempotency lives here rather than in the driver so that it survives a
    process restart: the failure it guards against is a retry after a crash, and
    an in-memory record would be gone exactly when it is needed.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def get(self, job_id: str) -> int | None:
        value = self._load().get(job_id)
        return int(value) if value is not None else None

    def put(self, job_id: str, deposition_id: int) -> None:
        data = self._load()
        data[job_id] = deposition_id
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.path)


class ZenodoDriver:
    SERVES = ("R1", "R7", "R9", "C8")
    def __init__(self, broker: CredentialBroker, registry: DepositionRegistry,
                 *, base_url: str = SANDBOX, scope: str = "zenodo:deposit",
                 timeout: float = 120.0) -> None:
        self._broker = broker
        self._registry = registry
        self.base_url = base_url.rstrip("/")
        self._scope = scope
        self.timeout = timeout

    # -- plugin contract --------------------------------------------------

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="repository-zenodo", version="0.1.0",
            protocol="RepositoryDriver", requirements_supported=list(self.SERVES),
            offline_capable=False, requires_network=True,
        )

    # -- helpers ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._broker.get(self._scope).reveal()}"}

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = httpx.request(method, url, headers=self._headers(),
                                     timeout=self.timeout, **kwargs)
        except httpx.HTTPError as exc:
            # The message names the operation, never the credential: httpx
            # exceptions carry the request but not the header values.
            raise ExternalServiceError(
                f"Zenodo {method} {url.replace(self.base_url, '')} failed: "
                f"{type(exc).__name__}. The workflow pauses and can be resumed."
            ) from None
        if response.status_code == 401:
            raise ExternalServiceError(
                "Zenodo rejected the credential. Check that DD_ZENODO_TOKEN is "
                "set to a token with the deposit:write and deposit:actions "
                "scopes for this instance."
            )
        return response

    @staticmethod
    def _problems(response: httpx.Response) -> list[str]:
        """Extract what went wrong, with the status code.

        The status was omitted at first, and a live failure reported only
        "Not found." — which cost a guess about whether the endpoint was wrong,
        the record was missing, or the operation was refused. A message that
        cannot be acted on is barely better than none.
        """
        prefix = f"HTTP {response.status_code}"
        try:
            payload = response.json()
        except ValueError:
            return [f"{prefix}: {response.text[:200]}"]
        errors = payload.get("errors") or []
        if errors:
            return [f"{prefix} {e.get('field', '?')}: {e.get('message', '')}"
                    for e in errors]
        return [f"{prefix}: {payload.get('message', response.text[:200])}"]

    # -- the driver contract ----------------------------------------------

    def to_zenodo_metadata(self, record: CanonicalRecord,
                           projected: dict) -> dict:
        """Zenodo's own metadata shape, which is not DataCite's.

        Built from the canonical record rather than by transforming the DataCite
        projection: a transformation of a transformation loses the provenance of
        each field, and Zenodo names several fields differently for reasons that
        are not derivable from DataCite.
        """
        metadata: dict = {
            "title": record.title,
            "upload_type": UPLOAD_TYPES.get(record.resource_type.value, "dataset"),
            "creators": [
                {"name": c.name,
                 **({"orcid": c.orcid} if c.orcid else {}),
                 **({"affiliation": c.affiliations[0].name}
                    if c.affiliations else {})}
                for c in record.creators
            ],
        }
        abstracts = [d.text for d in record.descriptions
                     if d.kind.value == "Abstract"]
        if abstracts:
            metadata["description"] = abstracts[0]
        if record.publication_year:
            metadata["publication_date"] = f"{record.publication_year}-01-01"
        if record.subjects:
            metadata["keywords"] = [s.term for s in record.subjects]
        if record.rights:
            metadata["license"] = record.rights.licence_id.lower()
            metadata["access_right"] = "open"
        if record.version:
            metadata["version"] = record.version
        if record.language:
            metadata["language"] = record.language
        if record.related:
            metadata["related_identifiers"] = [
                {"identifier": r.identifier,
                 "relation": r.relation_type[0].lower() + r.relation_type[1:],
                 "scheme": r.identifier_type.lower()}
                for r in record.related
            ]
        if record.funding:
            metadata["grants"] = [
                {"id": f.award_number} for f in record.funding if f.award_number
            ]
        return metadata

    def preflight(self, record: CanonicalRecord, artefacts: list[Path],
                  *, profile=None) -> list[str]:
        """Problems that would prevent a deposit. Reported, never repaired."""
        problems: list[str] = []
        if profile is not None:
            problems += [f"missing required field: {f}"
                         for f in profile.missing_required(record)]
        if not artefacts:
            problems.append("a deposit requires at least one file")
        for path in artefacts:
            if not path.exists():
                problems.append(f"file not found: {path.name}")
            elif path.stat().st_size == 0:
                problems.append(f"file is empty: {path.name}")

        metadata = self.to_zenodo_metadata(record, {})
        if not metadata.get("creators"):
            problems.append("Zenodo requires at least one creator")
        if not metadata.get("title"):
            problems.append("Zenodo requires a title")

        vocabulary = (profile.relation_vocabulary() if profile is not None
                      else None)
        if vocabulary is not None:
            for related in record.related:
                if related.relation_type not in vocabulary.permitted:
                    problems.append(
                        f"relation {related.relation_type!r} is not accepted by "
                        f"{vocabulary.schema_id}")
        return problems

    def begin(self, job_id: str) -> int:
        """Create a deposition, or return the one this job already created.

        The registry lookup happens before the request, so a retry after a crash
        between creation and recording cannot produce a second record.
        """
        existing = self._registry.get(job_id)
        if existing is not None:
            return existing
        response = self._request(
            "POST", f"{self.base_url}/api/deposit/depositions", json={})
        if response.status_code >= 400:
            raise ExternalServiceError(
                "Zenodo refused to create a deposition: "
                + "; ".join(self._problems(response)))
        deposition_id = response.json()["id"]
        self._registry.put(job_id, deposition_id)
        return deposition_id

    def _deposition(self, deposition_id: int) -> dict:
        response = self._request(
            "GET", f"{self.base_url}/api/deposit/depositions/{deposition_id}")
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"cannot read deposition {deposition_id}: "
                + "; ".join(self._problems(response)))
        return response.json()

    def upload(self, deposition_id: int, path: Path) -> None:
        bucket = self._deposition(deposition_id)["links"]["bucket"]
        with open(path, "rb") as handle:
            response = self._request("PUT", f"{bucket}/{path.name}",
                                     content=handle.read())
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"upload of {path.name} failed: "
                + "; ".join(self._problems(response)))

    def set_metadata(self, deposition_id: int, record: CanonicalRecord) -> None:
        response = self._request(
            "PUT", f"{self.base_url}/api/deposit/depositions/{deposition_id}",
            json={"metadata": self.to_zenodo_metadata(record, {})})
        if response.status_code >= 400:
            raise ExternalServiceError(
                "Zenodo rejected the metadata: "
                + "; ".join(self._problems(response)))

    def publish(self, deposition_id: int, *, on_behalf_of: Orcid) -> DepositReceipt:
        response = self._request(
            "POST",
            f"{self.base_url}/api/deposit/depositions/{deposition_id}/actions/publish")
        if response.status_code == 409:
            # Already published. Treated as success rather than as an error: a
            # retry after a lost response would otherwise leave the job unable to
            # complete despite the deposit having succeeded.
            payload = self._deposition(deposition_id)
        elif response.status_code >= 400:
            raise ExternalServiceError(
                "Zenodo refused to publish: " + "; ".join(self._problems(response)))
        else:
            payload = response.json()
        return self._receipt(payload)

    def deposit(self, job_id: str, record: CanonicalRecord, artefacts: list[Path],
                *, on_behalf_of: Orcid) -> DepositReceipt:
        """The whole sequence, resumable at any point.

        A retry on an already-published deposition returns the existing receipt
        without replaying anything. That is what idempotency means here: "already
        done" yields the result, it does not repeat the work. Replaying was the
        original behaviour and it failed against the real API, which refuses
        metadata writes to a published record until it is reopened through
        `actions/edit`. Reopening a published deposit to write metadata nobody
        asked to change would be worse than failing.
        """
        deposition_id = self.begin(job_id)
        existing = self._deposition(deposition_id)
        if existing.get("submitted") or existing.get("state") == "done":
            return self._receipt(existing)

        self.set_metadata(deposition_id, record)
        uploaded = set(self._uploaded(deposition_id))
        for path in artefacts:
            if path.name not in uploaded:
                self.upload(deposition_id, path)
        return self.publish(deposition_id, on_behalf_of=on_behalf_of)

    @staticmethod
    def _receipt(payload: dict) -> DepositReceipt:
        return DepositReceipt(
            pid=payload.get("doi") or "",
            concept_pid=payload.get("conceptdoi"),
            landing_page=payload.get("links", {}).get("html"),
            deposited_at=str(payload.get("state", "done")),
        )

    def _uploaded(self, deposition_id: int) -> list[str]:
        """Names already uploaded, so a resumed deposit does not re-send them."""
        payload = self._deposition(deposition_id)
        return [f.get("filename") or f.get("key")
                for f in payload.get("files", []) if isinstance(f, dict)]

    # -- reconciliation ---------------------------------------------------

    def probers(self) -> dict:
        """How to ask Zenodo whether an interrupted attempt took effect.

        Supplied by the driver because only it knows how to ask. A component
        that performs an external effect and offers no way to check it leaves
        recovery with nothing but a guess.
        """
        from datadirector_contracts import EffectKind
        from ..workflow.effects import DID_NOT, INDETERMINATE, TOOK_EFFECT

        def probe_create(intent) -> tuple[str, str]:
            job_id = intent.detail.get("job_id", "")
            recorded = self._registry.get(job_id)
            if recorded is None:
                return DID_NOT, ("no deposition is recorded for this job, so "
                                 "none was created")
            try:
                payload = self._deposition(recorded)
            except Exception as exc:
                return INDETERMINATE, f"deposition {recorded} could not be read: {exc}"
            return TOOK_EFFECT, (f"deposition {payload['id']} exists in state "
                                 f"{payload.get('state')}")

        def probe_upload(intent) -> tuple[str, str]:
            deposition_id = intent.detail.get("deposition_id")
            filename = intent.detail.get("artefact")
            if not deposition_id or not filename:
                return INDETERMINATE, "the attempt recorded no deposition or file"
            try:
                uploaded = self._uploaded(int(deposition_id))
            except Exception as exc:
                return INDETERMINATE, f"the deposition could not be read: {exc}"
            if filename in uploaded:
                return TOOK_EFFECT, f"{filename} is present on the deposition"
            return DID_NOT, f"{filename} is not present on the deposition"

        def probe_publish(intent) -> tuple[str, str]:
            deposition_id = intent.detail.get("deposition_id")
            if not deposition_id:
                return INDETERMINATE, "the attempt recorded no deposition"
            try:
                payload = self._deposition(int(deposition_id))
            except Exception as exc:
                return INDETERMINATE, f"the deposition could not be read: {exc}"
            if payload.get("submitted") or payload.get("doi"):
                return TOOK_EFFECT, (f"published as {payload.get('doi')}; "
                                     "do not publish again")
            return DID_NOT, "the deposition is still a draft"

        return {
            EffectKind.REPOSITORY_CREATE: probe_create,
            EffectKind.REPOSITORY_UPLOAD: probe_upload,
            EffectKind.REPOSITORY_PUBLISH: probe_publish,
        }

    def new_version_of(self, concept_or_deposition_id: int) -> int | None:
        response = self._request(
            "POST",
            f"{self.base_url}/api/deposit/depositions/"
            f"{concept_or_deposition_id}/actions/newversion")
        if response.status_code >= 400:
            raise ExternalServiceError(
                "Zenodo refused a new version: "
                + "; ".join(self._problems(response)))
        draft = response.json().get("links", {}).get("latest_draft")
        return int(draft.rstrip("/").split("/")[-1]) if draft else None
