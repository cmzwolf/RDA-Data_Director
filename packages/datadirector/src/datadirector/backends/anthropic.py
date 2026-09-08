"""Remote model backend (Anthropic API).

Residency is extra-jurisdiction, so policy will not resolve to it for material
classified above the level the deployment permits (§9.4).
"""

from __future__ import annotations

import httpx
from datadirector_contracts import (
    CapabilityManifest, Digest, ModelRequest, ModelResponse, Residency,
)

from ..credentials.broker import CredentialBroker
from ..errors import ExternalServiceError
from .base import build_messages, manifest_for

API = "https://api.anthropic.com/v1/messages"


class AnthropicBackend:
    def __init__(self, name: str, model: str, broker: CredentialBroker,
                 scope: str = "anthropic:api", timeout: float = 120.0) -> None:
        self.name = name
        self.model = model
        self._broker = broker
        self._scope = scope
        self.timeout = timeout

    def manifest(self) -> CapabilityManifest:
        return manifest_for(self.name, self.model, Residency.EXTRA_JURISDICTION, offline=False)

    def residency(self) -> Residency:
        return Residency.EXTRA_JURISDICTION

    def complete(self, request: ModelRequest) -> ModelResponse:
        messages = build_messages(request)
        # The Anthropic API takes the system prompt separately from the turns,
        # which suits the separation this system requires rather than obstructing it.
        system_turns = [m["content"] for m in messages if m["role"] == "system"]
        user_turns = [{"role": "user", "content": m["content"]}
                      for m in messages if m["role"] == "user"]
        secret = self._broker.get(self._scope)
        try:
            r = httpx.post(
                API,
                headers={"x-api-key": secret.reveal(),
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": self.model, "max_tokens": request.max_tokens,
                      "system": "\n\n".join(system_turns), "messages": user_turns},
                timeout=self.timeout,
            )
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []))
        except Exception as exc:
            # The message must not carry the credential; httpx exceptions carry
            # the request but not the headers' values in their str().
            raise ExternalServiceError(
                f"Anthropic API call failed: {type(exc).__name__}. "
                "The workflow will pause and can be resumed."
            ) from None
        return ModelResponse(
            text=text, model_id=self.model,
            input_digest=Digest.of_canonical_json(messages),
        )
