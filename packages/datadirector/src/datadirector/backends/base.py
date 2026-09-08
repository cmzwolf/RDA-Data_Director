"""Shared backend behaviour.

The invariant every backend must preserve: `system`, `user_content` and
`trusted_instructions` occupy structurally distinct positions in the outbound
request and are never concatenated into one string.

This is where the shield of commitment C-4 is either kept or lost, and it is
lost in exactly one line of careless string formatting. Hence a shared helper
rather than three hand-written payload builders.
"""

from __future__ import annotations

from datadirector_contracts import CapabilityManifest, ModelRequest, Residency


def build_messages(request: ModelRequest) -> list[dict[str, str]]:
    """Compose a chat message list with the three inputs kept apart.

    Ingested material goes in the user turn and nowhere else. Instructions from
    an authenticated channel go in their own system turn, labelled, so that no
    concatenation can make untrusted text look like a directive.
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": request.system}]
    if request.trusted_instructions:
        messages.append({
            "role": "system",
            "content": (
                "The following instructions come from the authenticated depositor "
                "and carry directive authority:\n" + request.trusted_instructions
            ),
        })
    messages.append({"role": "user", "content": request.user_content})
    return messages


def manifest_for(name: str, version: str, residency: Residency,
                 *, offline: bool) -> CapabilityManifest:
    return CapabilityManifest(
        name=name, version=version, protocol="ModelBackend",
        offline_capable=offline, residency=residency, requires_network=not offline,
    )
