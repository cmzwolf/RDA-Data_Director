"""Shared helpers for the live suite.

A module rather than conftest functions: pytest injects fixtures into test
functions but not module-level helpers, and a helper defined only in conftest is
an undefined name at the point of use. That mistake cost a live run to discover
because the offline suite never executes this code.
"""

from __future__ import annotations

import os


def endpoint() -> str:
    return os.environ.get("DD_OLLAMA_ENDPOINT", "http://localhost:11434")


def installed_models() -> list[str]:
    """Model tags currently available, or an empty list if Ollama is unreachable."""
    try:
        import httpx

        response = httpx.get(f"{endpoint()}/api/tags", timeout=5.0)
        response.raise_for_status()
        return sorted(m["name"] for m in response.json().get("models", []))
    except Exception:
        return []
