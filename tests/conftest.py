"""Shared fixtures, and a report of optional dependencies that are absent.

Three times now a newly declared optional dependency has produced confusing
failures on a machine whose environment predated it. The dependencies are
genuinely optional — the software degrades correctly without them — so the right
answer is not to require them, but the run should say plainly which capabilities
are unavailable rather than leaving a reader to infer it from skip counts.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from datadirector_contracts import Orcid

# ---------------------------------------------------------------------------
# Refuse to run against a checkout that is not this one
# ---------------------------------------------------------------------------
#
# A virtual environment copied from another checkout — which is how this one
# arrived, its `activate` and every console-script shebang still naming the
# directory it was copied from — leaves `python` and `pytest` resolving
# `datadirector` from that other tree. The suite then passes, green, while
# testing code that no longer matches what is on disk here. A run that measures
# the wrong source is worse than a failing one, because it is readable as
# evidence.
#
# A venv is not portable: recreate it with `python -m venv .venv` and reinstall
# rather than copying it between checkouts.

ROOT = Path(__file__).resolve().parent.parent
OWNED = ("datadirector", "datadirector_contracts")


def pytest_configure(config):
    """Fail before the first test if a package under test comes from elsewhere."""
    strays = []
    for name in OWNED:
        module = importlib.import_module(name)
        location = Path(getattr(module, "__file__", "")).resolve().parent
        if ROOT not in location.parents:
            strays.append((name, location))
    if strays:
        detail = "; ".join(f"{name} from {location}" for name, location in strays)
        raise pytest.UsageError(
            f"the suite is at {ROOT} but {detail}. A copied or shared virtual "
            f"environment is testing another checkout. Repair it with:\n"
            f"  {ROOT}/.venv/bin/python -m pip install --no-deps -e "
            f"packages/datadirector-contracts -e packages/datadirector")



# Import name to the capability it provides and the extra that installs it.
OPTIONAL = {
    "jsonschema": ("R4 schema validation", "validate"),
    "PIL": ("image structure measurement and image test fixtures", "media"),
    "pyflakes": ("static checks over live-only code", "dev"),
    "fastapi": ("the core API (C5)", "api"),
    "jinja2": ("the web interface (C6)", "api"),
    "multipart": ("HTML form posts in the web interface", "api"),
}


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    missing = []
    for module, (capability, extra) in OPTIONAL.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append((module, capability, extra))
    if not missing:
        return
    terminalreporter.write_sep("-", "optional dependencies not installed")
    for module, capability, extra in missing:
        terminalreporter.write_line(f"  {module}: {capability} is unavailable")
    extras = sorted({extra for _, _, extra in missing})
    terminalreporter.write_line(
        f"  install with: pip install -e "
        f"'packages/datadirector[{','.join(extras)}]'")


@pytest.fixture
def researcher() -> Orcid:
    """Josiah S. Carberry, the fictitious professor ORCID publishes for use in
    examples and testing. Not a real person.

    https://orcid.org/0000-0002-1825-0097
    """
    return Orcid(value="0000-0002-1825-0097")


@pytest.fixture
def data_steward() -> Orcid:
    """A second identity, needed where a test must distinguish two people.

    Synthetic: constructed to satisfy the MOD 11-2 checksum, with no claim that
    it is unassigned. It exists only to be different from `researcher` and is
    never sent to the ORCID API or to any other service.
    """
    return Orcid(value="0000-0001-2345-6789")


@pytest.fixture
def job_id() -> str:
    """A well-formed job identifier (ULID form)."""
    return "job-01JBQ7X9ABCDEFGHJKMNPQRSTV"


@pytest.fixture
def resolved_config():
    """The example configuration, resolved.

    Shared because more than one test file needs a real runtime, and building
    one in each would drift.
    """
    from pathlib import Path

    from datadirector.config.loader import load_policy, load_wiring, resolve
    from datadirector.plugins.discovery import PluginRegistry

    root = Path(__file__).parent.parent
    return resolve(load_wiring(root / "config/wiring.example.yaml"),
                   load_policy(root / "config/policy.example.yaml"),
                   PluginRegistry(), application_version="0.1.0")


@pytest.fixture
def runtime(resolved_config, tmp_path):
    from datadirector.runtime import Runtime

    return Runtime(resolved_config, state_root=tmp_path / "state",
                   working_root=tmp_path / "work")


# ---------------------------------------------------------------------------
# No unit test reaches the network
# ---------------------------------------------------------------------------
#
# A test that quietly makes a real HTTP request is slow on a good connection and
# catastrophic on a bad one: this suite took fifty minutes on a machine where it
# takes thirty seconds here, and the difference was `Pipeline.ingest` consulting
# a repository registry over the internet.
#
# Blocked rather than tolerated, and blocked with a message naming the host, so
# the next one is found in seconds instead of being absorbed as "the tests are
# slow".

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}


class NetworkAccessInTest(RuntimeError):
    """A unit test tried to reach the network."""


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Refuse outbound connections except to a local test server.

    The live suite is exempt: reaching real services is its entire purpose.
    """
    if "live" in str(request.node.fspath):
        return

    import socket

    real_create = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        if str(host) not in _ALLOWED_HOSTS:
            raise NetworkAccessInTest(
                f"{request.node.name} tried to connect to {host!r}. Unit tests "
                "do not reach the network: stub the client, or move the test to "
                "tests/live/ where real services are the point."
            )
        return real_create(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)
