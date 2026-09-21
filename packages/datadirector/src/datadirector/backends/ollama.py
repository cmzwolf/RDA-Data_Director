"""Local model backend, served by Ollama.

Residency is on-premise: nothing sent here leaves the machine, which is what
makes it the backend the policy layer resolves to for sensitive material.
"""

from __future__ import annotations

import httpx
from datadirector_contracts import (
    CapabilityManifest, Digest, ModelCapability, ModelRequest, ModelResponse,
    Residency,
)

from ..errors import ConfigurationError, ExternalServiceError
from .base import build_messages, capabilities_from_config, manifest_for


class OllamaBackend:
    SERVES = ("P10", "P11", "C17", "P14")
    """Model tags are opaque strings; this module never parses their shape."""

    def __init__(self, name: str, endpoint: str, model: str, timeout: float = 120.0,
                 capabilities: set[ModelCapability] | None = None,
                 declared: list[str] | None = None) -> None:
        self.name = name
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._capabilities = capabilities or capabilities_from_config(declared)

    def manifest(self) -> CapabilityManifest:
        return manifest_for(self.name, self.model, Residency.ON_PREMISE,
                            offline=True, capabilities=self._capabilities)

    def capabilities(self) -> set[ModelCapability]:
        """What this backend can do.

        Vision is configured explicitly rather than assumed: whether a local tag
        accepts images depends on the model, and assuming it broadly would let
        the PEP select a backend that then fails mid-workflow.
        """
        return set(self._capabilities)

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
        if request.images and ModelCapability.VISION not in self._capabilities:
            raise ConfigurationError(
                f"backend {self.name!r} was sent images but does not declare the "
                "vision capability. Add `capabilities: [vision]` to its wiring "
                "entry, or the policy layer will keep routing images elsewhere."
            )
        # Ollama takes base64 image strings on the message itself; the media type
        # is inferred from the data, so the parallel list is dropped here.
        wire = [{k: v for k, v in m.items() if k != "image_media_types"}
                for m in messages]
        try:
            r = httpx.post(
                f"{self.endpoint}/api/chat",
                json={"model": self.model, "messages": wire, "stream": False,
                      "options": {"num_predict": request.max_tokens}},
                timeout=self.timeout,
            )
            r.raise_for_status()
            text = r.json()["message"]["content"]
        except httpx.ReadTimeout as exc:
            raise ExternalServiceError(
                f"Ollama did not respond within {self.timeout:.0f}s for model "
                f"{self.model!r}. Large models routinely exceed this on ordinary "
                "hardware: raise timeout_seconds for this backend in the wiring "
                "configuration. The workflow pauses and can be resumed."
            ) from exc
        except Exception as exc:
            raise ExternalServiceError(
                f"Ollama call failed against {self.endpoint}: {exc}. "
                "The workflow will pause and can be resumed once it is reachable."
            ) from exc
        return ModelResponse(
            text=text, model_id=self.model,
            input_digest=Digest.of_canonical_json(messages),
        )
