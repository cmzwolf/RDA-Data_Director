"""Live tests: the ones that need a real model.

Skipped unless DD_LIVE_TESTS=1. They are separated from the main suite because
they are slow, non-deterministic, and require infrastructure a reviewer may not
have; but they test the only claims the offline suite cannot reach.

What the offline suite establishes is that our prompt construction keeps
ingested material out of the instruction position, and that an unconfirmed claim
carries no authority. What only these tests can establish is whether a given
model returns usable output at all, and how often it can be steered by text
inside a document.
"""

import os

import pytest

LIVE = os.environ.get("DD_LIVE_TESTS") == "1"
requires_live = pytest.mark.skipif(not LIVE, reason="set DD_LIVE_TESTS=1 to run")


@pytest.fixture(scope="session")
def ollama_backend():
    from datadirector.backends.ollama import OllamaBackend

    backend = OllamaBackend(
        name="local",
        endpoint=os.environ.get("DD_OLLAMA_ENDPOINT", "http://localhost:11434"),
        model=os.environ.get("DD_OLLAMA_MODEL", "qwen3.8:27b"),
    )
    backend.check_available()  # fails loudly with the installed list
    return backend


@pytest.fixture
def recording(ollama_backend, tmp_path_factory):
    """Wrap the live backend so responses become replayable fixtures."""
    from datadirector.backends.recording import RecordingBackend

    return RecordingBackend(ollama_backend, "tests/fixtures/model")
