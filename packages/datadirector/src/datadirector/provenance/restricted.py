"""The restricted store: free-text justifications kept out of the open record.

Document A §7.5. A complete provenance record can disclose what it was meant to
protect. Recording "the informant's name was removed because she is the only
midwife in the district" defeats the redaction it documents.

Free text lives here, keyed by activity id, with only its digest in the chain.
Presenting the text later and recomputing the digest proves it is the original,
which is sufficient under tamper evidence (ADR-007).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from datadirector_contracts import Digest

from ..errors import AuthorityError

AUDITOR_ROLES = frozenset({"auditor", "data-steward"})


class RestrictedStore:
    SERVES = ("C2", "P5", "C12")
    """Append-only, like everything else.

    Entries are never deleted: a digest recorded in the chain must always
    resolve, or the chain contains a dangling proof. Retention applies to
    *access*, not to existence.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, activity_id: str) -> Path:
        safe = activity_id.replace("/", "_").replace("..", "_")
        return self.root / f"{safe}.json"

    def put(self, activity_id: str, text: str) -> Digest:
        """Store a justification and return its digest for the open record."""
        digest = Digest.of_bytes(text.encode("utf-8"))
        path = self._path(activity_id)
        if path.exists():
            raise AuthorityError(
                f"a justification already exists for activity {activity_id}; "
                "the restricted store is append-only"
            )
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"activity_id": activity_id, "text": text,
                        "digest": digest.value}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return digest

    def get(self, activity_id: str, *, role: str) -> str:
        """Retrieve a justification. Requires an auditing role.

        A non-auditor receives an error rather than an empty result: silence
        would be indistinguishable from absence, and absence is a meaningful
        finding for an auditor.
        """
        if role not in AUDITOR_ROLES:
            raise AuthorityError(
                f"role {role!r} may not read the restricted store; "
                f"one of {sorted(AUDITOR_ROLES)} is required"
            )
        path = self._path(activity_id)
        if not path.exists():
            raise KeyError(f"no justification recorded for activity {activity_id}")
        return json.loads(path.read_text(encoding="utf-8"))["text"]

    def verify(self, activity_id: str, expected: Digest, *, role: str) -> bool:
        """Recompute the digest of stored text and compare with the chain."""
        text = self.get(activity_id, role=role)
        return Digest.of_bytes(text.encode("utf-8")) == expected
