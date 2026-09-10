"""Recording and replay backends.

Lets the whole test suite run offline and lets a reviewer reproduce the paper's
experiments without holding credentials. Fixtures are keyed by the digest of the
composed messages, so a change to prompt construction invalidates the fixture
rather than silently replaying a response to a different question.
"""

from __future__ import annotations

import json
from pathlib import Path

from datadirector_contracts import (
    CapabilityManifest, Digest, ModelCapability, ModelRequest, ModelResponse,
    Residency,
)

from ..errors import ExternalServiceError
from .base import build_messages, manifest_for


class ReplayBackend:
    """Serves recorded responses. Residency on-premise: nothing leaves."""

    def __init__(self, name: str, fixture_dir: Path | str) -> None:
        self.name = name
        self.dir = Path(fixture_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def manifest(self) -> CapabilityManifest:
        return manifest_for(self.name, "replay", Residency.ON_PREMISE, offline=True)

    def capabilities(self) -> set[ModelCapability]:
        return {ModelCapability.TEXT_GENERATION, ModelCapability.STRUCTURED_OUTPUT}

    def residency(self) -> Residency:
        return Residency.ON_PREMISE

    def _key(self, request: ModelRequest) -> str:
        return Digest.of_canonical_json(build_messages(request)).value[:32]

    def complete(self, request: ModelRequest) -> ModelResponse:
        path = self.dir / f"{self._key(request)}.json"
        if not path.exists():
            raise ExternalServiceError(
                f"no recorded response for this request (key {self._key(request)}). "
                "Record it against a live backend first, or the prompt has changed "
                "since the fixture was made."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return ModelResponse(
            text=data["text"], model_id=data["model_id"],
            input_digest=Digest(value=data["input_digest"]),
        )


class RecordingBackend:
    """Wraps a live backend and writes fixtures as it goes."""

    def __init__(self, inner, fixture_dir: Path | str) -> None:
        self.inner = inner
        self.dir = Path(fixture_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def manifest(self) -> CapabilityManifest:
        return self.inner.manifest()

    def capabilities(self) -> set[ModelCapability]:
        return self.inner.capabilities()

    def residency(self) -> Residency:
        return self.inner.residency()

    def complete(self, request: ModelRequest) -> ModelResponse:
        response = self.inner.complete(request)
        key = Digest.of_canonical_json(build_messages(request)).value[:32]
        (self.dir / f"{key}.json").write_text(
            json.dumps({"text": response.text, "model_id": response.model_id,
                        "input_digest": response.input_digest.value},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return response
