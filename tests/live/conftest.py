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

from ._ollama import endpoint as _endpoint, installed_models as _installed_models

LIVE = os.environ.get("DD_LIVE_TESTS") == "1"
requires_live = pytest.mark.skipif(not LIVE, reason="set DD_LIVE_TESTS=1 to run")


@pytest.fixture(scope="session")
def model_name() -> str:
    """The model under test. Required, never defaulted.

    A default here would silently attribute results to whichever tag happened to
    be hardcoded, which is unusable for a paper: every measurement must name the
    model that produced it. Absence is a configuration error, not a fallback.
    """
    name = os.environ.get("DD_OLLAMA_MODEL")
    if not name:
        installed = _installed_models()
        raise pytest.UsageError(
            "DD_OLLAMA_MODEL is not set. Name the model explicitly so results are "
            "attributable.\n"
            f"Installed on {_endpoint()}: {installed or '(could not reach Ollama)'}\n"
            "  DD_LIVE_TESTS=1 DD_OLLAMA_MODEL=<tag> pytest tests/live -q -s"
        )
    return name


@pytest.fixture(scope="session")
def ollama_backend(model_name):
    from datadirector.backends.ollama import OllamaBackend

    backend = OllamaBackend(
        name="local",
        endpoint=_endpoint(),
        model=model_name,
        timeout=float(os.environ.get("DD_OLLAMA_TIMEOUT", "600")),
    )
    backend.check_available()  # fails loudly with the installed list
    return backend


@pytest.fixture
def recording(ollama_backend, model_name):
    """Wrap the live backend so responses become replayable fixtures.

    Fixtures are filed per model, because a recording replayed under a different
    model's name would misattribute the result.
    """
    from datadirector.backends.recording import RecordingBackend

    safe = model_name.replace(":", "_").replace("/", "_")
    return RecordingBackend(ollama_backend, f"tests/fixtures/model/{safe}")


@pytest.fixture(scope="session")
def record_measurement():
    """Append one measurement per run to a machine-readable log.

    Printed output scrolls past and is lost. The paper needs figures that can be
    recomputed and compared across models, so each observation is written as a
    JSON line with the model that produced it, the fixture, and the timestamp.
    The file is append-only for the same reason the event log is: a measurement
    that disagrees with an earlier one is a finding, not a correction.
    """
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    path = Path("tests/results/measurements.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(**fields):
        fields["recorded_at"] = datetime.now(timezone.utc).isoformat()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(fields, ensure_ascii=False) + "\n")

    return write


@pytest.fixture(scope="session")
def pillow_required():
    """Image fixtures need Pillow, and so does the structure check.

    Skipped rather than degraded: without Pillow the text fixtures cannot be
    built and the contradiction check cannot be made, so any result would be
    about the harness rather than about the model.
    """
    return pytest.importorskip(
        "PIL", reason="pip install 'pillow>=10.0' to run the image tests")
