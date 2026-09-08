"""Local model backend, served by Ollama.

Residency is on-premise: nothing sent here leaves the machine, which is what
makes it the backend the policy layer resolves to for sensitive material.
"""

from __future__ import annotations

import httpx
from datadirector_contracts import (
    CapabilityManifest, Digest, ModelRequest, ModelResponse, Residency,
)

from ..errors import ConfigurationError, ExternalServiceError
from .base import build_messages, manifest_for


class OllamaBackend:
    """Model tags are opaque strings; this module never parses their shape."""

    def __init__(self, name: str, endpoint: str, model: str, timeout: float = 120.0) -> None:
        self.name = name
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout

    def manifest(self) -> CapabilityManifest:
        return manifest_for(self.name, self.model, Residency.ON_PREMISE, offline=True)

    def residency(self) -> Residency:
        return Residency.ON_PREMISE

    def check_available(self) -> None:
        """Verify at startup that the configured tag is installed.

        A wrong model tag discovered mid-workflow is exactly the failure C7
        forbids, so the check happens once at startup and names what is actually
        present rather than only what is missing.
        """
        try:
            r = httpx.get(f"{self.endpoint}/api/tags", timeout=10.0)
            r.raise_for_status()
            installed = [m["name"] for m in r.json().get("models", [])]
        except Exception as exc:
            raise ConfigurationError(
                f"cannot reach Ollama at {self.endpoint}: {exc}. "
                "Start Ollama, or point DD_OLLAMA_ENDPOINT elsewhere."
            ) from exc
        if self.model not in installed:
            raise ConfigurationError(
                f"model {self.model!r} is not installed in Ollama at {self.endpoint}. "
                f"Installed: {sorted(installed) or '(none)'}. "
                f"Run: ollama pull {self.model}"
            )

    def complete(self, request: ModelRequest) -> ModelResponse:
        messages = build_messages(request)
        try:
            r = httpx.post(
                f"{self.endpoint}/api/chat",
                json={"model": self.model, "messages": messages, "stream": False,
                      "options": {"num_predict": request.max_tokens}},
                timeout=self.timeout,
            )
            r.raise_for_status()
            text = r.json()["message"]["content"]
        except Exception as exc:
            raise ExternalServiceError(
                f"Ollama call failed against {self.endpoint}: {exc}. "
                "The workflow will pause and can be resumed once it is reachable."
            ) from exc
        return ModelResponse(
            text=text, model_id=self.model,
            input_digest=Digest.of_canonical_json(messages),
        )
