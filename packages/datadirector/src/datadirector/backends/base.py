"""Shared backend behaviour.

The invariant every backend must preserve: `system`, `user_content` and
`trusted_instructions` occupy structurally distinct positions in the outbound
request and are never concatenated into one string.

This is where the shield of commitment C-4 is either kept or lost, and it is
lost in exactly one line of careless string formatting. Hence a shared helper
rather than three hand-written payload builders.
"""

from __future__ import annotations

from datadirector_contracts import (
    CapabilityManifest, ModelCapability, ModelRequest, Residency,
)

# Capability names accepted in wiring configuration, mapped to the enum. Declared
# rather than probed: a backend selected for a capability it lacks fails
# mid-workflow, which is the failure C7 forbids.
CAPABILITY_NAMES = {c.value: c for c in ModelCapability}


def capabilities_from_config(names: list[str] | None) -> set[ModelCapability]:
    """Parse declared capability names, defaulting to text only.

    An unknown name is an error rather than an omission: silently dropping it
    would leave an operator believing their backend can do something it will
    never be asked to do.
    """
    if not names:
        return {ModelCapability.TEXT_GENERATION, ModelCapability.STRUCTURED_OUTPUT}
    out = set()
    for name in names:
        capability = CAPABILITY_NAMES.get(str(name).strip().lower())
        if capability is None:
            raise ValueError(
                f"unknown model capability {name!r}; "
                f"expected one of {sorted(CAPABILITY_NAMES)}"
            )
        out.add(capability)
    out.add(ModelCapability.TEXT_GENERATION)
    return out


def build_messages(request: ModelRequest) -> list[dict]:
    """Compose a chat message list with the inputs kept apart.

    Ingested material goes in the user turn and nowhere else. Instructions from
    an authenticated channel go in their own system turn, labelled, so that no
    concatenation can make untrusted text look like a directive.

    Images attach to the user turn for the same reason. Text rendered inside an
    image is instruction-shaped material that a vision model reads, so an image
    in the system position would be a visual prompt injection channel — the
    direct analogue of the textual case.
    """
    messages: list[dict] = [{"role": "system", "content": request.system}]
    if request.trusted_instructions:
        messages.append({
            "role": "system",
            "content": (
                "The following instructions come from the authenticated depositor "
                "and carry directive authority:\n" + request.trusted_instructions
            ),
        })
    user: dict = {"role": "user", "content": request.user_content}
    if request.images:
        user["images"] = [img.data_base64 for img in request.images]
        user["image_media_types"] = [img.media_type for img in request.images]
    messages.append(user)
    return messages


def manifest_for(name: str, version: str, residency: Residency, *, offline: bool,
                 capabilities: set[ModelCapability] | None = None) -> CapabilityManifest:
    caps = capabilities or {ModelCapability.TEXT_GENERATION}
    return CapabilityManifest(
        name=name, version=version, protocol="ModelBackend",
        offline_capable=offline, residency=residency, requires_network=not offline,
        model_capabilities=sorted(caps, key=lambda c: c.value),
    )
