"""Tests for the spine: event store, projection, provenance, policy, workflow.

Structured as claims about the architecture rather than as coverage of methods.
"""

import json
import multiprocessing as mp
import subprocess
import sys
from pathlib import Path

import pytest
from datadirector_contracts import (
    Digest, Event, EventKind, PolicyConfig, ProvActivity, Residency,
    SensitivityClass, Visibility,
)
from datadirector_contracts.policy import BackendDeclaration
from datadirector_contracts.provenance import ProvAgent

from datadirector.config.loader import capability_report_text, resolve
from datadirector.config.models import (
    DeploymentProfile, PluginDeclaration, StoragePaths, WiringConfig,
)
from datadirector.credentials.broker import CredentialBroker, Secret
from datadirector.errors import ChainIntegrityError, ConfigurationError, CredentialError
from datadirector.plugins.discovery import PluginRegistry
from datadirector.policy.pep import PolicyEnforcementPoint
from datadirector.provenance.recorder import Recorder
from datadirector.provenance.restricted import RestrictedStore
from datadirector.state.projection import fold, fold_to
from datadirector.state.store import ConcurrentAppendError, EventStore


@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path / "state")


def ev(job_id, kind=EventKind.WORKFLOW_CREATED, **kw):
    return Event(sequence=1, job_id=job_id, kind=kind, agent="test/1.0", **kw)


# -- The event store -------------------------------------------------------

def test_append_builds_a_verifiable_chain(store, job_id):
    for _ in range(5):
        store.append(ev(job_id))
    events = store.load(job_id)
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5]
    assert events[0].prev_digest is None
    assert events[3].prev_digest == events[2].digest()


def test_hand_edited_event_file_is_detected(store, job_id):
    for _ in range(3):
        store.append(ev(job_id))
    target = sorted((store.root / job_id).glob("0000*.json"))[1]
    data = json.loads(target.read_text())
    data["agent"] = "tampered/9.9"
    target.write_text(json.dumps(data))
    with pytest.raises(ChainIntegrityError, match="cannot be trusted"):
        store.load(job_id)


def test_deleted_event_is_detected(store, job_id):
    for _ in range(3):
        store.append(ev(job_id))
    sorted((store.root / job_id).glob("0000*.json"))[1].unlink()
    with pytest.raises(ChainIntegrityError):
        store.load(job_id)


def test_partial_write_leaves_the_log_valid(store, job_id):
    """A crash between write and rename must not corrupt the log."""
    store.append(ev(job_id))
    (store.root / job_id / ".tmp-00002-9999").write_text('{"broken')
    assert len(store.load(job_id)) == 1


def test_store_exposes_no_mutation_operations():
    """The absence is the contract (ADR-008): rollback is an append."""
    forbidden = {"delete", "remove", "truncate", "update", "rewrite", "drop"}
    assert not forbidden & {m for m in dir(EventStore) if not m.startswith("_")}


def _child_append(root, job_id, hold, q):
    s = EventStore(root)
    try:
        with s._job_lock(job_id):
            hold.wait(timeout=5)
        q.put("ok")
    except ConcurrentAppendError:
        q.put("refused")


def test_concurrent_append_is_refused_not_merged(store, job_id):
    """Two writers computing prev_digest from one tail would fork the chain."""
    store.append(ev(job_id))
    with store._job_lock(job_id):
        ctx = mp.get_context("fork")
        q = ctx.Queue()
        hold = ctx.Event()
        hold.set()
        p = ctx.Process(target=_child_append, args=(store.root, job_id, hold, q))
        p.start()
        p.join(timeout=10)
        assert q.get(timeout=5) == "refused"


# -- Projection ------------------------------------------------------------

def test_fold_is_pure(store, job_id):
    store.append(ev(job_id, payload={"config_digest": "abc"}))
    store.append(ev(job_id, EventKind.MATERIAL_REGISTERED, payload={"artefacts": ["a.csv"]}))
    events = store.load(job_id)
    assert fold(events) == fold(events)


def test_classification_tightens_but_never_loosens_through_the_fold(store, job_id, researcher):
    store.append(ev(job_id))
    store.append(ev(job_id, EventKind.DECLARATION_CONFIRMED, human=researcher,
                    payload={"sensitivity": int(SensitivityClass.PUBLIC)}))
    store.append(ev(job_id, EventKind.CLASSIFICATION_COMPLETED,
                    payload={"sensitivity": int(SensitivityClass.SENSITIVE),
                             "rationale": "indirect identifier"}))
    state = fold(store.load(job_id))
    assert state.classification.level is SensitivityClass.SENSITIVE

    # A later scan claiming a lower level must not lower it.
    store.append(ev(job_id, EventKind.CLASSIFICATION_COMPLETED,
                    payload={"sensitivity": int(SensitivityClass.PUBLIC)}))
    assert fold(store.load(job_id)).classification.level is SensitivityClass.SENSITIVE


def test_fold_to_reproduces_earlier_state(store, job_id):
    store.append(ev(job_id))
    store.append(ev(job_id, EventKind.MATERIAL_REGISTERED, payload={"artefacts": ["a.csv"]}))
    store.append(ev(job_id, EventKind.MATERIAL_REGISTERED, payload={"artefacts": ["b.csv"]}))
    events = store.load(job_id)
    assert fold_to(events, 2).material == ["a.csv"]
    assert fold(events).material == ["a.csv", "b.csv"]


# -- Provenance ------------------------------------------------------------

def test_export_omits_restricted_subgraphs(tmp_path, store, job_id, researcher):
    rec = Recorder(store, tmp_path / "prov")
    agent = ProvAgent(software="test/1.0", human=researcher)
    rec.record(job_id, ProvActivity(activity_id="a1", activity_type="dd:Generate",
                                    agent=agent, visibility=Visibility.OPEN),
               ev(job_id))
    rec.record(job_id, ProvActivity(activity_id="a2", activity_type="dd:Redact",
                                    agent=agent, visibility=Visibility.CONFIDENTIAL),
               ev(job_id))
    assert len(rec.export(job_id, up_to=Visibility.OPEN)["@graph"]) == 1
    assert len(rec.export(job_id, up_to=Visibility.CONFIDENTIAL)["@graph"]) == 2


def test_restricted_store_refuses_non_auditor(tmp_path):
    rs = RestrictedStore(tmp_path / "restricted")
    digest = rs.put("a1", "the informant is identifiable by role")
    with pytest.raises(Exception, match="may not read"):
        rs.get("a1", role="researcher")
    assert rs.verify("a1", digest, role="auditor")


# -- Credentials -----------------------------------------------------------

def test_secret_never_prints_itself():
    s = Secret("tok_abcdef123456", "zenodo:deposit")
    assert "tok_abcdef" not in repr(s)
    assert "tok_abcdef" not in str(s)
    assert "tok_abcdef" not in f"{s}"
    assert s.reveal() == "tok_abcdef123456"


def test_traceback_from_a_credentialed_call_carries_no_secret():
    s = Secret("tok_secret_value", "x")
    try:
        raise RuntimeError(f"call failed with {s}")
    except RuntimeError as exc:
        assert "tok_secret_value" not in str(exc)


def test_missing_credential_names_the_variable_not_the_value():
    b = CredentialBroker({"zenodo:deposit": "DD_ZENODO_TOKEN"}, environ={})
    with pytest.raises(CredentialError, match="DD_ZENODO_TOKEN"):
        b.get("zenodo:deposit")


# -- Policy ----------------------------------------------------------------

class FakeBackend:
    def __init__(self, residency):
        self._r = residency

    def residency(self):
        return self._r


def _policy(overrides=None):
    base = {
        SensitivityClass.PUBLIC: ["local", "remote"],
        SensitivityClass.INTERNAL: ["local"],
        SensitivityClass.SENSITIVE: ["local"],
    }
    base.update(overrides or {})
    return PolicyConfig(backend_by_sensitivity=base)


LOCAL_ONLY = {
    SensitivityClass.PUBLIC: ["local"],
    SensitivityClass.INTERNAL: ["local"],
    SensitivityClass.SENSITIVE: ["local"],
}


def test_pep_resolves_the_most_restrictive_permitted_backend():
    pep = PolicyEnforcementPoint(_policy(), {
        "local": FakeBackend(Residency.ON_PREMISE),
        "remote": FakeBackend(Residency.EXTRA_JURISDICTION),
    })
    assert pep.resolve_backend(SensitivityClass.PUBLIC).residency() is Residency.ON_PREMISE


def test_pep_halts_rather_than_degrading():
    """No fallback to a permitted-but-less-appropriate path."""
    from datadirector_contracts import PolicyHalt
    pep = PolicyEnforcementPoint(
        _policy({SensitivityClass.SENSITIVE: ["local"]}),
        {"remote": FakeBackend(Residency.EXTRA_JURISDICTION)},
    )
    with pytest.raises(PolicyHalt, match="no permitted model backend"):
        pep.resolve_backend(SensitivityClass.SENSITIVE)


def test_pep_exposes_no_method_naming_a_backend():
    """The bypass is inexpressible, so the test checks the structure."""
    import inspect
    for name, member in inspect.getmembers(PolicyEnforcementPoint, inspect.isfunction):
        if name.startswith("_"):
            continue
        params = set(inspect.signature(member).parameters)
        assert not params & {"backend", "backend_name", "model", "endpoint"}, name


# -- Configuration ---------------------------------------------------------

def _wiring(**kw):
    base = dict(
        profile=DeploymentProfile.SINGLE_USER_LOCAL,
        storage=StoragePaths(state_root="s", working_root="w", restricted_root="r"),
        backends=[BackendDeclaration(name="local", kind="ollama",
                                     residency=Residency.ON_PREMISE)],
        plugins=[PluginDeclaration(name="zenodo")],
    )
    base.update(kw)
    return WiringConfig(**base)


def test_policy_naming_an_undeclared_backend_fails_at_startup():
    with pytest.raises(ConfigurationError, match="wiring does not"):
        resolve(_wiring(), _policy(), PluginRegistry(), application_version="0.1.0")


def test_empty_permitted_list_is_a_startup_failure():
    with pytest.raises(ConfigurationError, match="silently unusable"):
        resolve(_wiring(),
                _policy({**LOCAL_ONLY, SensitivityClass.SENSITIVE: []}),
                PluginRegistry(), application_version="0.1.0")


def test_resolved_config_hashes_stably():
    p = _policy(LOCAL_ONLY)
    a = resolve(_wiring(), p, PluginRegistry(), application_version="0.1.0")
    b = resolve(_wiring(), p, PluginRegistry(), application_version="0.1.0")
    assert a.digest() == b.digest()


def test_capability_report_names_what_is_unavailable():
    p = _policy(LOCAL_ONLY)
    r = resolve(_wiring(), p, PluginRegistry(), application_version="0.1.0")
    text = capability_report_text(r, refused=[])
    assert "R8" in text and "NOT AVAILABLE" in text
