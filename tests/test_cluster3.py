"""Cluster 3, Part A: capability routing.

The claim under test is an ordering, not a feature: policy filters first,
capability narrows within the permitted set, residency orders what remains.
Getting that order wrong would let a uniquely capable but forbidden backend be
selected, which is the bypass the PEP exists to make inexpressible.
"""

import inspect

import pytest
from datadirector_contracts import (
    CapabilityUnavailable, ModelCapability, PolicyConfig, PolicyHalt,
    Residency, SensitivityClass,
)

from datadirector.policy.pep import PolicyEnforcementPoint

TEXT = ModelCapability.TEXT_GENERATION
VISION = ModelCapability.VISION


class Backend:
    def __init__(self, residency, capabilities):
        self._r = residency
        self._c = set(capabilities)

    def residency(self):
        return self._r

    def capabilities(self):
        return set(self._c)


class LegacyBackend:
    """Predates the capability contract: declares no capabilities()."""

    def residency(self):
        return Residency.ON_PREMISE


def policy(**by_class):
    base = {
        SensitivityClass.PUBLIC: ["local-text"],
        SensitivityClass.INTERNAL: ["local-text"],
        SensitivityClass.SENSITIVE: ["local-text"],
    }
    base.update({SensitivityClass[k.upper()]: v for k, v in by_class.items()})
    return PolicyConfig(backend_by_sensitivity=base)


def test_capability_narrows_within_the_permitted_set():
    pep = PolicyEnforcementPoint(
        policy(public=["local-text", "local-vision"]),
        {"local-text": Backend(Residency.ON_PREMISE, {TEXT}),
         "local-vision": Backend(Residency.ON_PREMISE, {VISION})},
    )
    assert VISION in pep.resolve_backend(SensitivityClass.PUBLIC, VISION).capabilities()
    assert TEXT in pep.resolve_backend(SensitivityClass.PUBLIC, TEXT).capabilities()


def test_a_capable_but_forbidden_backend_is_never_selected():
    """The ordering property. Capability must not rescue a forbidden backend."""
    pep = PolicyEnforcementPoint(
        policy(sensitive=["local-text"]),
        {"local-text": Backend(Residency.ON_PREMISE, {TEXT}),
         "remote-vision": Backend(Residency.EXTRA_JURISDICTION, {TEXT, VISION})},
    )
    with pytest.raises(CapabilityUnavailable):
        pep.resolve_backend(SensitivityClass.SENSITIVE, VISION)


def test_capability_halt_names_the_classification_and_the_capability():
    pep = PolicyEnforcementPoint(
        policy(), {"local-text": Backend(Residency.ON_PREMISE, {TEXT})})
    with pytest.raises(CapabilityUnavailable) as exc:
        pep.resolve_backend(SensitivityClass.SENSITIVE, VISION)
    message = str(exc.value)
    assert "sensitive" in message and "vision" in message
    assert "text-generation" in message, "the message must say what IS offered"


def test_capability_halt_is_distinguishable_from_a_residency_halt():
    """Same workflow consequence, different remedy, so different type."""
    assert issubclass(CapabilityUnavailable, PolicyHalt)
    pep = PolicyEnforcementPoint(policy(), {})
    with pytest.raises(PolicyHalt) as exc:
        pep.resolve_backend(SensitivityClass.SENSITIVE, TEXT)
    assert not isinstance(exc.value, CapabilityUnavailable)


def test_residency_still_orders_among_capable_backends():
    pep = PolicyEnforcementPoint(
        policy(public=["remote", "local"]),
        {"remote": Backend(Residency.EXTRA_JURISDICTION, {TEXT, VISION}),
         "local": Backend(Residency.ON_PREMISE, {TEXT, VISION})},
    )
    assert pep.resolve_backend(
        SensitivityClass.PUBLIC, VISION).residency() is Residency.ON_PREMISE


def test_undeclared_capabilities_are_assumed_narrowly():
    """A backend that has not declared vision must never be chosen for vision
    because of a missing method."""
    pep = PolicyEnforcementPoint(policy(), {"local-text": LegacyBackend()})
    assert pep.resolve_backend(SensitivityClass.PUBLIC, TEXT) is not None
    with pytest.raises(CapabilityUnavailable):
        pep.resolve_backend(SensitivityClass.PUBLIC, VISION)


def test_can_answers_without_raising():
    """The media agent needs to choose between inspecting and recording the
    material as uninspected, not to catch an exception."""
    pep = PolicyEnforcementPoint(
        policy(), {"local-text": Backend(Residency.ON_PREMISE, {TEXT})})
    assert pep.can(SensitivityClass.SENSITIVE, TEXT) is True
    assert pep.can(SensitivityClass.SENSITIVE, VISION) is False


def test_resolved_models_are_recorded_for_the_job():
    pep = PolicyEnforcementPoint(
        policy(public=["local-text", "local-vision"]),
        {"local-text": Backend(Residency.ON_PREMISE, {TEXT}),
         "local-vision": Backend(Residency.ON_PREMISE, {VISION})},
    )
    pep.resolve_backend(SensitivityClass.PUBLIC, TEXT)
    pep.resolve_backend(SensitivityClass.PUBLIC, VISION)
    assert pep.models_used() == ["local-text", "local-vision"]


def test_pep_still_exposes_no_method_naming_a_backend():
    """Extended from cluster 1: adding capability routing must not introduce a
    way to name a backend directly."""
    for name, member in inspect.getmembers(PolicyEnforcementPoint, inspect.isfunction):
        if name.startswith("_"):
            continue
        params = set(inspect.signature(member).parameters)
        assert not params & {"backend", "backend_name", "model", "endpoint"}, name


# ==========================================================================
# Part B: the exposure ledger
# ==========================================================================

from datadirector_contracts import (  # noqa: E402
    BudgetExceeded, Digest, Exposure, ExposureBudget, ReleaseKind,
)

from datadirector.exposure.ledger import ExposureLedger  # noqa: E402

PUBLIC = SensitivityClass.PUBLIC
LOCAL = Residency.ON_PREMISE


@pytest.fixture
def ledger(tmp_path):
    return ExposureLedger(tmp_path / "exposures",
                          ExposureBudget(max_bytes_per_job=1000,
                                         max_bytes_per_artefact=400,
                                         max_releases_per_job=5))


def _record(ledger, job_id, **kw):
    args = dict(job_id=job_id, artefact_uri="wrk://a.csv",
                kind=ReleaseKind.FIELD_SAMPLE, content=b"x" * 100,
                classification=PUBLIC, backend="local", residency=LOCAL)
    args.update(kw)
    return ledger.record(**args)


def test_ledger_records_a_digest_not_the_content(ledger, job_id):
    secret = b"Marie Dupont, 1984-03-02, 14 rue des Lilas"
    exposure = _record(ledger, job_id, content=secret, detail="field: address")
    serialised = exposure.model_dump_json()
    assert "Marie Dupont" not in serialised
    assert "rue des Lilas" not in serialised
    assert exposure.byte_count == len(secret)
    assert len(exposure.content_digest.value) == 64


def test_detail_is_a_label_not_content():
    with pytest.raises(ValueError, match="label, not content"):
        Exposure(job_id="j", artefact_uri="u", kind=ReleaseKind.FIELD_SAMPLE,
                 byte_count=0, content_digest=Digest.of_bytes(b""),
                 classification=PUBLIC, backend="local", residency=LOCAL,
                 detail="x" * 300)


def test_per_artefact_budget_stops_reading_one_file_by_repeated_sampling(ledger, job_id):
    """The attack the per-artefact limit exists for."""
    for _ in range(4):
        _record(ledger, job_id, content=b"x" * 100)
    with pytest.raises(BudgetExceeded, match="per-artefact limit"):
        _record(ledger, job_id, content=b"x" * 100)


def test_per_job_budget_stops_the_same_trick_spread_across_files(ledger, job_id):
    """Per-artefact alone would not catch this: many files, small reads each."""
    for i in range(3):
        _record(ledger, job_id, artefact_uri=f"wrk://f{i}.csv", content=b"x" * 300)
    with pytest.raises(BudgetExceeded, match="job limit"):
        _record(ledger, job_id, artefact_uri="wrk://f9.csv", content=b"x" * 300)


def test_release_count_is_capped_independently_of_bytes(ledger, job_id):
    """Many tiny reads are still many reads."""
    for i in range(5):
        _record(ledger, job_id, artefact_uri=f"wrk://f{i}.csv", content=b"x")
    with pytest.raises(BudgetExceeded, match="releases"):
        _record(ledger, job_id, artefact_uri="wrk://f9.csv", content=b"x")


def test_derived_descriptions_are_not_charged(ledger, job_id):
    """Charging for a structural profile would make the budget a limit on
    analysis rather than on disclosure."""
    for i in range(3):
        _record(ledger, job_id, artefact_uri=f"wrk://f{i}.csv",
                kind=ReleaseKind.STRUCTURAL_PROFILE, content=b"x" * 5000)
    charged, releases = ledger.spent(job_id)
    assert charged == 0 and releases == 3


def test_whole_artefact_leaving_the_premises_requires_a_named_human(
        ledger, job_id, researcher):
    """No smaller version of the decision exists to fall back to."""
    for kind in (ReleaseKind.FULL_DOCUMENT, ReleaseKind.MEDIA_CONTENT):
        with pytest.raises(PermissionError, match="identified human"):
            _record(ledger, job_id, kind=kind, content=b"x" * 10,
                    residency=Residency.EXTRA_JURISDICTION)
        e = _record(ledger, job_id, kind=kind, content=b"x" * 10,
                    residency=Residency.EXTRA_JURISDICTION,
                    authorised_by=researcher)
        assert e.authorised_by == researcher


def test_whole_artefact_read_on_premise_needs_no_separate_authority(ledger, job_id):
    """Otherwise you could not look at an image to find out whether it is
    sensitive without someone first authorising the exposure.

    That is the circularity the declaration gate exists to break, and requiring
    authority here would make media inspection unreachable in exactly the
    deployments that most need it: those handling sensitive material locally.
    """
    for kind in (ReleaseKind.FULL_DOCUMENT, ReleaseKind.MEDIA_CONTENT):
        e = _record(ledger, job_id, kind=kind, content=b"x" * 10,
                    residency=Residency.ON_PREMISE)
        assert e.authorised_by is None


def test_budget_is_checked_before_the_release_not_after(ledger, job_id):
    """A caller can choose a smaller request rather than trip the limit."""
    _record(ledger, job_id, content=b"x" * 350)
    assert ledger.would_exceed(job_id, "wrk://a.csv",
                               ReleaseKind.FIELD_SAMPLE, 100) is not None
    assert ledger.would_exceed(job_id, "wrk://a.csv",
                               ReleaseKind.FIELD_SAMPLE, 10) is None


def test_ledger_exposes_no_mutation_operations():
    """Append-only, like the event log."""
    forbidden = {"delete", "remove", "truncate", "update", "clear", "drop"}
    assert not forbidden & {m for m in dir(ExposureLedger) if not m.startswith("_")}


def test_ledger_survives_a_restart(tmp_path, job_id):
    root = tmp_path / "exposures"
    budget = ExposureBudget(max_bytes_per_job=1000)
    ExposureLedger(root, budget).record(
        job_id=job_id, artefact_uri="wrk://a.csv", kind=ReleaseKind.FIELD_SAMPLE,
        content=b"x" * 100, classification=PUBLIC, backend="local", residency=LOCAL)
    assert ExposureLedger(root, budget).spent(job_id) == (100, 1)


def test_summary_answers_what_saw_this_data(ledger, job_id):
    _record(ledger, job_id, backend="local-qwen")
    _record(ledger, job_id, artefact_uri="wrk://b.csv", backend="local-vision",
            kind=ReleaseKind.AGGREGATE)
    summary = ledger.summary(job_id)
    assert summary["backends"] == ["local-qwen", "local-vision"]
    assert summary["by_kind"] == {"aggregate": 1, "field-sample": 1}
    assert summary["artefacts"] == {"wrk://a.csv": 100}


# ==========================================================================
# Part C: probe vocabulary, executor, classification agent
# ==========================================================================

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from datadirector_contracts import (  # noqa: E402
    Classification, EventKind, ModelResponse, ProbeKind, ProbeRefused, ProbeRequest,
)

from datadirector.agents.classification import ClassificationAgent  # noqa: E402
from datadirector.probing.executor import ProbeExecutor, chunk_text  # noqa: E402
from datadirector.profiling.structural import profile_tree  # noqa: E402
from datadirector.state.projection import JobState  # noqa: E402


@pytest.fixture
def survey(tmp_path):
    """A small survey table with a re-identification hazard built in."""
    root = tmp_path / "work"
    root.mkdir()
    (root / "responses.csv").write_text(
        "respondent,village,occupation,income\n"
        "R001,Kerema,midwife,22000\n"
        "R002,Kerema,farmer,18000\n"
        "R003,Aroma,farmer,19500\n"
        "R004,Aroma,teacher,26000\n"
    )
    (root / "notes.txt").write_text("Field notes. " + ("Detail. " * 2000))
    return root


@pytest.fixture
def executor(survey, tmp_path, job_id):
    return ProbeExecutor(job_id, profile_tree(survey), survey,
                         ExposureLedger(tmp_path / "exp",
                                        ExposureBudget(max_sample_values=3)))


def _run(executor, request):
    return executor.run(request, classification=PUBLIC, backend="local",
                        residency=LOCAL)


# -- resolution against job state -----------------------------------------

def test_probe_naming_an_unprofiled_field_is_refused(executor):
    with pytest.raises(ProbeRefused, match="not in the structural profile"):
        _run(executor, ProbeRequest(kind=ProbeKind.SAMPLE_FIELD,
                                    field="secret_column", because="curiosity"))


def test_read_chunk_refuses_a_path_and_accepts_only_a_registered_artefact(executor):
    """Traversal is unreachable because there is no path to sanitise."""
    for attempt in ("../../etc/passwd", "/etc/passwd", "notes.txt/../../x"):
        with pytest.raises(ProbeRefused, match="not a registered artefact"):
            _run(executor, ProbeRequest(kind=ProbeKind.READ_CHUNK,
                                        artefact=attempt, because="reading"))
    result = _run(executor, ProbeRequest(kind=ProbeKind.READ_CHUNK,
                                         artefact="notes.txt", because="reading"))
    assert result.returned["index"] == 0


def test_refusal_is_never_silence(executor):
    """An empty answer is indistinguishable from a genuine empty result."""
    with pytest.raises(ProbeRefused):
        _run(executor, ProbeRequest(kind=ProbeKind.SAMPLE_FIELD,
                                    field=None, because="unspecified"))


def test_disabled_probe_is_refused_naming_policy(survey, tmp_path, job_id):
    ex = ProbeExecutor(job_id, profile_tree(survey), survey,
                       ExposureLedger(tmp_path / "e"),
                       disabled={ProbeKind.SAMPLE_FIELD})
    with pytest.raises(ProbeRefused, match="disabled by local policy"):
        _run(ex, ProbeRequest(kind=ProbeKind.SAMPLE_FIELD, field="village",
                              because="checking"))


# -- probes and the ledger -------------------------------------------------

def test_sample_is_clamped_and_the_clamping_recorded(executor):
    """A truncated answer is still useful; refusing would waste the round trip."""
    result = _run(executor, ProbeRequest(kind=ProbeKind.SAMPLE_FIELD,
                                         field="village", n=50, because="values"))
    assert len(result.returned) == 3
    assert result.clamped_from == 50


def test_aggregate_probes_return_no_values_and_cost_nothing(executor, job_id):
    result = _run(executor, ProbeRequest(kind=ProbeKind.DISTINCT_COUNT,
                                         field="respondent", because="identifier?"))
    assert result.returned == {"distinct": 4, "total": 4}
    assert result.byte_count == 0
    assert executor.ledger.spent(job_id)[0] == 0


def test_value_returning_probes_are_charged(executor, job_id):
    _run(executor, ProbeRequest(kind=ProbeKind.SAMPLE_FIELD, field="village",
                                n=3, because="values"))
    charged, releases = executor.ledger.spent(job_id)
    assert charged > 0 and releases == 1


def test_cross_tab_reports_cell_sizes_not_values(executor):
    """The re-identification probe: a combination appearing once is the finding,
    and its size is visible without its content."""
    result = _run(executor, ProbeRequest(kind=ProbeKind.CROSS_TAB, field="village",
                                         field_b="occupation",
                                         because="re-identification risk"))
    assert result.returned["singleton_combinations"] == 4
    serialised = json.dumps(result.returned)
    for value in ("Kerema", "midwife", "Aroma", "teacher"):
        assert value not in serialised


def test_value_shapes_returns_shapes_not_values(executor):
    result = _run(executor, ProbeRequest(kind=ProbeKind.VALUE_SHAPES,
                                         field="respondent", because="format"))
    assert "ANNN" in result.returned
    assert not any(k.startswith("R0") for k in result.returned)


# -- chunking --------------------------------------------------------------

def test_chunks_overlap():
    text = "".join(f"{i:05d} " for i in range(4000))
    chunks = chunk_text(text, size=1000, overlap=200)
    assert len(chunks) > 1
    assert chunks[0][-100:] in chunks[1], "a boundary must not fall between passages"


# -- the classification agent ---------------------------------------------

class ScriptedModel:
    """Returns a queue of canned replies, so the agent's logic is under test."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []
        self.name = "scripted"

    def residency(self):
        return LOCAL

    def capabilities(self):
        return {TEXT, ModelCapability.STRUCTURED_OUTPUT}

    def complete(self, request):
        self.seen.append(request)
        return ModelResponse(text=self.replies.pop(0), model_id="scripted",
                             input_digest=Digest.of_bytes(b""))


def _agent(model, executor):
    pep = PolicyEnforcementPoint(policy(), {"local-text": model})
    return ClassificationAgent(pep, executor)


def _state(job_id, level):
    return JobState(job_id=job_id, step="classification",
                    classification=Classification(level=level))


PROBE_REPLY = json.dumps({"probes": [
    {"kind": "cross_tab", "field": "village", "field_b": "occupation",
     "because": "check for singleton combinations"},
    {"kind": "distinct_count", "field": "respondent", "because": "identifier?"},
]})


def test_the_model_sees_structure_before_it_sees_values(executor, job_id):
    """Phase 2 is the whole point: probes are chosen from the profile alone."""
    model = ScriptedModel(PROBE_REPLY, json.dumps(
        {"sensitivity": "sensitive", "indicators": ["singleton combinations"],
         "confidence": 0.8}))
    agent = _agent(model, executor)
    agent.classify(_state(job_id, PUBLIC))
    first_prompt = model.seen[0].user_content
    assert "village" in first_prompt, "field names are shown"
    assert "Kerema" not in first_prompt, "values are not"


def test_classification_tightens_autonomously(executor, job_id):
    model = ScriptedModel(PROBE_REPLY, json.dumps(
        {"sensitivity": "sensitive", "indicators": ["one midwife in one village"],
         "confidence": 0.9}))
    events, decision = _agent(model, executor).classify(_state(job_id, PUBLIC))
    assert events[0].kind is EventKind.CLASSIFICATION_COMPLETED
    assert events[0].payload["sensitivity"] == int(SensitivityClass.SENSITIVE)
    assert "tightened autonomously" in decision.selection_basis


def test_classification_never_lowers_itself(executor, job_id):
    """A lower assessment produces a contradiction for a human, not a downgrade."""
    model = ScriptedModel(PROBE_REPLY, json.dumps(
        {"sensitivity": "public", "indicators": ["looks fine"], "confidence": 0.9}))
    events, decision = _agent(model, executor).classify(
        _state(job_id, SensitivityClass.SENSITIVE))
    assert events[0].kind is EventKind.CLASSIFICATION_CONTRADICTED
    assert "never lowered autonomously" in decision.selection_basis


def test_unparseable_assessment_changes_nothing(executor, job_id):
    model = ScriptedModel(PROBE_REPLY, "I am not sure, sorry.")
    events, decision = _agent(model, executor).classify(_state(job_id, PUBLIC))
    assert events == []
    assert "unparseable" in decision.undetermined[0]


def test_malformed_probes_are_dropped_not_repaired(executor, job_id):
    """Guessing what the model meant would attribute a request it never made."""
    model = ScriptedModel(
        json.dumps({"probes": [
            {"kind": "not_a_probe", "field": "village", "because": "x"},
            {"kind": "distinct_count", "field": "village", "because": "y"},
        ]}),
        json.dumps({"sensitivity": "internal", "indicators": [], "confidence": 0.5}))
    agent = _agent(model, executor)
    requests, _, parsed = agent.propose_probes(_state(job_id, PUBLIC))
    assert parsed is True
    assert [r.kind for r in requests] == [ProbeKind.DISTINCT_COUNT]


def test_refused_probes_are_reported_back_to_the_model(executor, job_id):
    model = ScriptedModel(
        json.dumps({"probes": [{"kind": "sample_field", "field": "nonexistent",
                                "because": "x"}]}),
        json.dumps({"sensitivity": "internal", "indicators": [], "confidence": 0.5}))
    agent = _agent(model, executor)
    events, decision = agent.classify(_state(job_id, PUBLIC))
    assert any("not in the structural profile" in u for u in decision.undetermined)
    assert "probes_refused" in model.seen[1].user_content


def test_no_probes_is_distinguished_from_an_unreadable_reply(executor, job_id):
    """Opposite situations that an empty list cannot tell apart.

    A model asking for nothing because the profile settles the question is the
    ideal outcome on obviously non-personal data. A model whose reply could not
    be read means the assessment rests on no evidence at all. Conflating them
    would hide the second behind the first.
    """
    assessment = json.dumps({"sensitivity": "public", "indicators": [],
                             "confidence": 0.9})

    asked_nothing = ScriptedModel(json.dumps({"probes": []}), assessment)
    _, _, parsed = _agent(asked_nothing, executor).propose_probes(
        _state(job_id, PUBLIC))
    assert parsed is True

    unreadable = ScriptedModel("I'm not sure what you want.", assessment)
    _, _, parsed = _agent(unreadable, executor).propose_probes(_state(job_id, PUBLIC))
    assert parsed is False

    wrong_shape = ScriptedModel(json.dumps({"answer": "no probes needed"}), assessment)
    _, _, parsed = _agent(wrong_shape, executor).propose_probes(_state(job_id, PUBLIC))
    assert parsed is False


def test_the_reason_no_evidence_was_gathered_is_recorded(executor, job_id):
    assessment = json.dumps({"sensitivity": "public", "indicators": [],
                             "confidence": 0.9})

    _, decision = _agent(
        ScriptedModel(json.dumps({"probes": []}), assessment), executor
    ).classify(_state(job_id, PUBLIC))
    assert any("requested no probes" in u for u in decision.undetermined)

    _, decision = _agent(
        ScriptedModel("sorry?", assessment), executor
    ).classify(_state(job_id, PUBLIC))
    assert any("could not be read" in u for u in decision.undetermined)


# ==========================================================================
# Part C3: chunked content inspection and the correlation pass
# ==========================================================================

from datadirector.content.inspector import ContentInspector  # noqa: E402


def _midwife_document(near: bool = False) -> str:
    """A document whose disclosure is split across two distant passages.

    Neither half identifies anyone. Together they identify one person. This is
    the case the whole design exists for, and the case chunk-local reading
    cannot see.
    """
    filler = ("The team travelled by road and river. Weather conditions were "
              "recorded each morning. Equipment was checked before each visit. ")
    first = "Kerema district has a resident population of four hundred and twelve. "
    second = "Our principal informant is the only midwife serving the district. "
    if near:
        return filler * 5 + first + second + filler * 5
    return filler * 60 + first + filler * 120 + second + filler * 60


class ChunkModel:
    """Returns observations per chunk, then a correlation verdict.

    `chunk_replies` is keyed by a substring the chunk must contain, so the model
    behaves like one that reads only what it is given.
    """

    name = "chunk-model"

    def __init__(self, chunk_replies: dict, correlation: str):
        self.chunk_replies = chunk_replies
        self.correlation = correlation
        self.seen = []

    def residency(self):
        return LOCAL

    def capabilities(self):
        return {TEXT}

    def complete(self, request):
        self.seen.append(request)
        if "correlat" in request.system.lower() or "every section" in request.system:
            return ModelResponse(text=self.correlation, model_id=self.name,
                                 input_digest=Digest.of_bytes(b""))
        for marker, reply in self.chunk_replies.items():
            if marker in request.user_content:
                return ModelResponse(text=reply, model_id=self.name,
                                     input_digest=Digest.of_bytes(b""))
        return ModelResponse(text=json.dumps({"observations": [],
                                              "section_alone_is_sensitive": False}),
                             model_id=self.name, input_digest=Digest.of_bytes(b""))


def _obs(*items, sensitive=False):
    return json.dumps({
        "observations": [{"what": w, "kind": k, "quote_shape": None}
                         for w, k in items],
        "section_alone_is_sensitive": sensitive,
    })


@pytest.fixture
def notes_executor(tmp_path, job_id):
    def build(text):
        work = tmp_path / "w"
        work.mkdir(exist_ok=True)
        (work / "notes.txt").write_text(text)
        return ProbeExecutor(job_id, profile_tree(work), work,
                             ExposureLedger(tmp_path / "e",
                                            ExposureBudget(max_bytes_per_job=10_000_000,
                                                           max_bytes_per_artefact=10_000_000,
                                                           max_releases_per_job=500)))
    return build


def _inspector(model, executor):
    return ContentInspector(PolicyEnforcementPoint(policy(), {"local-text": model}),
                            executor)


CORRELATION_FINDS_IT = json.dumps({
    "sensitivity": "sensitive",
    "combinations": [{"observations": ["population of 412", "the only midwife"],
                      "why": "a unique role in a population of 412 identifies one person"}],
    "indicators": ["unique role within a small named population"],
    "confidence": 0.9,
})


def test_cross_referential_disclosure_is_found_by_correlation(notes_executor, job_id):
    """The test that justifies this cluster.

    Neither chunk is sensitive alone, and the model says so for each. The
    document is sensitive, and only the correlation pass can see it.
    """
    executor = notes_executor(_midwife_document())
    model = ChunkModel(
        {"four hundred and twelve": _obs(("resident population of 412", "population")),
         "only midwife": _obs(("informant is the only midwife in the district", "role"))},
        CORRELATION_FINDS_IT,
    )
    findings, decision = _inspector(model, executor).inspect(
        _state(job_id, PUBLIC), "notes.txt")

    assert findings.chunks_read > 1, "the fixture must actually span chunks"
    assert findings.chunk_local_sensitive is False, (
        "no single chunk is sensitive; if one were, this would not be testing "
        "cross-referential detection"
    )
    assert findings.sensitivity is SensitivityClass.SENSITIVE
    assert findings.combinations[0].observations == ["population of 412",
                                                     "the only midwife"]


def test_chunk_local_reading_alone_would_miss_it(notes_executor, job_id):
    """The control. Without correlation, the same evidence yields nothing."""
    executor = notes_executor(_midwife_document())
    model = ChunkModel(
        {"four hundred and twelve": _obs(("resident population of 412", "population")),
         "only midwife": _obs(("informant is the only midwife in the district", "role"))},
        json.dumps({"sensitivity": "public", "combinations": [],
                    "indicators": [], "confidence": 0.5}),
    )
    findings, _ = _inspector(model, executor).inspect(
        _state(job_id, PUBLIC), "notes.txt")
    assert findings.chunk_local_sensitive is False
    assert findings.sensitivity is SensitivityClass.PUBLIC


def test_observations_from_every_chunk_reach_the_correlation_pass(notes_executor, job_id):
    executor = notes_executor(_midwife_document())
    model = ChunkModel(
        {"four hundred and twelve": _obs(("population of 412", "population")),
         "only midwife": _obs(("the only midwife", "role"))},
        CORRELATION_FINDS_IT)
    findings, _ = _inspector(model, executor).inspect(
        _state(job_id, PUBLIC), "notes.txt")
    sections = {o.chunk_index for o in findings.observations}
    assert len(sections) >= 2, "observations must come from more than one chunk"
    correlation_prompt = model.seen[-1].user_content
    assert "412" in correlation_prompt and "midwife" in correlation_prompt


def test_correlation_sees_observations_not_the_document(notes_executor, job_id):
    """One small call regardless of document length, and no second full read."""
    executor = notes_executor(_midwife_document())
    model = ChunkModel(
        {"four hundred and twelve": _obs(("population of 412", "population"))},
        CORRELATION_FINDS_IT)
    _inspector(model, executor).inspect(_state(job_id, PUBLIC), "notes.txt")
    correlation_prompt = model.seen[-1].user_content
    assert "travelled by road and river" not in correlation_prompt
    assert len(correlation_prompt) < 4000


def test_a_chunk_sensitive_alone_settles_the_document(notes_executor, job_id):
    """Tightening applies chunk-wise: correlation cannot talk it down."""
    executor = notes_executor(_midwife_document())
    model = ChunkModel(
        {"only midwife": _obs(("full name and address of informant", "identifier"),
                              sensitive=True)},
        json.dumps({"sensitivity": "public", "combinations": [],
                    "indicators": [], "confidence": 0.9}))
    findings, _ = _inspector(model, executor).inspect(
        _state(job_id, PUBLIC), "notes.txt")
    assert findings.sensitivity is SensitivityClass.SENSITIVE
    assert findings.sensitive_sections, "the section index must be recorded"
    assert any("sensitive on their own" in i for i in findings.indicators)


def test_no_observations_yields_no_verdict_not_a_clean_bill(notes_executor, job_id):
    """Silence must not be reported as a result."""
    executor = notes_executor("Short note about equipment calibration.")
    model = ChunkModel({}, CORRELATION_FINDS_IT)
    findings, _ = _inspector(model, executor).inspect(
        _state(job_id, PUBLIC), "notes.txt")
    assert findings.observations == []
    assert findings.sensitivity is None


def test_content_chunks_are_charged_to_the_ledger(notes_executor, job_id):
    executor = notes_executor(_midwife_document())
    model = ChunkModel({}, CORRELATION_FINDS_IT)
    _inspector(model, executor).inspect(_state(job_id, PUBLIC), "notes.txt")
    charged, releases = executor.ledger.spent(job_id)
    assert charged > 0 and releases >= 2


def test_which_sections_were_independently_sensitive_is_recorded(notes_executor, job_id):
    """The boolean alone cannot support the correlation claim.

    A document may hold a section that is sensitive for reasons unrelated to a
    cross-referential disclosure elsewhere. Knowing *which* sections were flagged
    is what distinguishes "the verdict rested on correlation" from "one section
    settled it anyway".
    """
    executor = notes_executor(_midwife_document())
    model = ChunkModel(
        {"four hundred and twelve": _obs(("population of 412", "population")),
         "only midwife": _obs(("consent excludes publication", "consent"),
                              sensitive=True)},
        CORRELATION_FINDS_IT)
    findings, _ = _inspector(model, executor).inspect(
        _state(job_id, PUBLIC), "notes.txt")
    assert findings.chunk_local_sensitive is True
    assert findings.sensitive_sections == [4], (
        "the flagged section must be identifiable, so a reader can see whether "
        "it is one of the halves under test or an unrelated passage"
    )


# ==========================================================================
# Part D: media inspection
# ==========================================================================

import struct  # noqa: E402
import zipfile as _zipfile  # noqa: E402

from datadirector_contracts import (  # noqa: E402
    InspectionTier, MediaFinding, UninspectedReason,
)

from datadirector.agents.media import MediaAgent  # noqa: E402
from datadirector.media.metadata import extract, looks_encrypted, medium_of  # noqa: E402

VISION = ModelCapability.VISION


def _jpeg_with_gps(path):
    """A minimal JPEG whose Exif IFD declares a GPS pointer."""
    entries = [(0x8825, 4, 1, 0), (0x0110, 2, 1, 0)]  # GPS IFD, camera model
    ifd = struct.pack("<H", len(entries))
    for tag, typ, count, value in entries:
        ifd += struct.pack("<HHII", tag, typ, count, value)
    ifd += struct.pack("<I", 0)
    tiff = b"II" + struct.pack("<H", 42) + struct.pack("<I", 8) + ifd
    exif = b"Exif\x00\x00" + tiff
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
    path.write_bytes(b"\xff\xd8" + app1 + b"\xff\xd9")
    return path


def test_gps_is_found_without_any_model(tmp_path):
    """The leak is often in the metadata: a photo of a field site carries the
    coordinates of the field site."""
    findings = extract(_jpeg_with_gps(tmp_path / "site.jpg"))
    assert any("GPS" in f for f in findings)
    assert any("camera model" in f for f in findings)


def test_metadata_findings_describe_rather_than_quote(tmp_path):
    """Repeating a location into the record defeats the point of noticing it."""
    for f in extract(_jpeg_with_gps(tmp_path / "site.jpg")):
        assert not any(ch.isdigit() for ch in f), f"a value leaked into {f!r}"


def test_dicom_presence_is_itself_the_finding(tmp_path):
    p = tmp_path / "scan.dcm"
    p.write_bytes(b"\x00" * 128 + b"DICM" + b"\x00" * 64)
    assert any("patient name" in f for f in extract(p))


def test_pdf_author_field_is_noticed(tmp_path):
    p = tmp_path / "report.pdf"
    p.write_bytes(b"%PDF-1.7\n<< /Author (someone) /Producer (writer) >>\n")
    findings = extract(p)
    assert any("author" in f.lower() for f in findings)
    assert not any("someone" in f for f in findings)


def test_encrypted_archive_is_detected(tmp_path):
    p = tmp_path / "locked.zip"
    with _zipfile.ZipFile(p, "w") as zf:
        zf.writestr("a.txt", "x")
    # Flip the encryption bit in the local header and central directory.
    data = bytearray(p.read_bytes())
    for i in range(len(data) - 3):
        if data[i:i + 4] in (b"PK\x03\x04", b"PK\x01\x02"):
            offset = 6 if data[i:i + 4] == b"PK\x03\x04" else 8
            data[i + offset] |= 0x1
    p.write_bytes(bytes(data))
    assert looks_encrypted(p) is True


def _media_agent(tmp_path, backends, permitted):
    pep = PolicyEnforcementPoint(
        PolicyConfig(backend_by_sensitivity={
            SensitivityClass.PUBLIC: permitted,
            SensitivityClass.INTERNAL: permitted,
            SensitivityClass.SENSITIVE: permitted,
        }), backends)
    return MediaAgent(pep, tmp_path)


def test_uninspected_is_a_recorded_state_not_silence(tmp_path, job_id):
    """The failure this whole tier exists to prevent: a reviewer seeing no flags
    and concluding the file was checked."""
    _jpeg_with_gps(tmp_path / "site.jpg")
    agent = _media_agent(tmp_path, {"text": Backend(Residency.ON_PREMISE, {TEXT})},
                         ["text"])
    finding = agent.inspect(_state(job_id, SensitivityClass.SENSITIVE), "site.jpg")
    assert finding.tier is InspectionTier.NONE
    assert finding.uninspected_reason is not None
    assert finding.presumed is True


def test_an_uninspected_image_is_presumed_sensitive(tmp_path, job_id):
    """A face is personal data whatever the content turns out to be."""
    (tmp_path / "faces.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    agent = _media_agent(tmp_path, {"text": Backend(Residency.ON_PREMISE, {TEXT})},
                         ["text"])
    finding = agent.inspect(_state(job_id, PUBLIC), "faces.png")
    assert finding.sensitivity is SensitivityClass.SENSITIVE
    assert finding.presumed is True


def test_the_reason_distinguishes_cannot_from_not_allowed(tmp_path, job_id):
    """Different remedies: install a model, or change policy."""
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xff\xd9")

    no_vision = _media_agent(tmp_path, {"text": Backend(Residency.ON_PREMISE, {TEXT})},
                             ["text"])
    assert no_vision.inspect(_state(job_id, PUBLIC), "a.jpg").uninspected_reason \
        is UninspectedReason.NO_CAPABLE_BACKEND

    forbidden = _media_agent(
        tmp_path,
        {"text": Backend(Residency.ON_PREMISE, {TEXT}),
         "remote-vision": Backend(Residency.EXTRA_JURISDICTION, {TEXT, VISION})},
        ["text"])
    assert forbidden.inspect(_state(job_id, SensitivityClass.SENSITIVE),
                             "a.jpg").uninspected_reason \
        is UninspectedReason.CAPABILITY_NOT_PERMITTED


def test_metadata_runs_even_when_content_cannot(tmp_path, job_id):
    """Tier one is unconditional: it is cheap and often decisive alone."""
    _jpeg_with_gps(tmp_path / "site.jpg")
    agent = _media_agent(tmp_path, {"text": Backend(Residency.ON_PREMISE, {TEXT})},
                         ["text"])
    finding = agent.inspect(_state(job_id, SensitivityClass.SENSITIVE), "site.jpg")
    assert finding.tier is InspectionTier.NONE
    assert any("GPS" in f for f in finding.metadata_findings)


def test_instrument_formats_are_unreadable_not_unexamined(tmp_path, job_id):
    (tmp_path / "spectrum.fits").write_bytes(b"SIMPLE  =  T" + b" " * 100)
    agent = _media_agent(tmp_path, {"text": Backend(Residency.ON_PREMISE, {TEXT})},
                         ["text"])
    finding = agent.inspect(_state(job_id, PUBLIC), "spectrum.fits")
    assert finding.uninspected_reason is UninspectedReason.FORMAT_UNREADABLE
    assert finding.sensitivity is SensitivityClass.INTERNAL


def test_uninspected_artefacts_must_record_a_reason():
    """The type refuses silence about why."""
    with pytest.raises(ValueError, match="must record why"):
        MediaFinding(artefact="a.jpg", tier=InspectionTier.NONE,
                     sensitivity=SensitivityClass.SENSITIVE, presumed=True)
    with pytest.raises(ValueError, match="must be marked as one"):
        MediaFinding(artefact="a.jpg", tier=InspectionTier.NONE,
                     uninspected_reason=UninspectedReason.ENCRYPTED,
                     sensitivity=SensitivityClass.SENSITIVE)


def test_every_uninspected_file_appears_in_the_decision_record(tmp_path, job_id):
    for name in ("a.jpg", "b.png", "c.fits"):
        (tmp_path / name).write_bytes(b"\x00" * 64)
    agent = _media_agent(tmp_path, {"text": Backend(Residency.ON_PREMISE, {TEXT})},
                         ["text"])
    findings, events, decision = agent.inspect_all(
        _state(job_id, PUBLIC), ["a.jpg", "b.png", "c.fits"])
    assert len(findings) == 3
    assert len(decision.undetermined) == 3
    assert len(events[0].payload["uninspected"]) == 3


# ==========================================================================
# Part E: gate items and redaction proposals
# ==========================================================================

import inspect as _inspect  # noqa: E402

from datadirector_contracts import (  # noqa: E402
    GateItem, GateItemKind, GateState, ItemDecision, ReasonCode,
    RedactionProposal, Treatment,
)

from datadirector.agents.redaction import RedactionAgent  # noqa: E402
from datadirector.errors import AuthorityError  # noqa: E402
from datadirector.gate.items import (  # noqa: E402
    Gate, from_media_findings, from_redaction_proposals,
)


def _uninspected_finding(name="a.jpg"):
    return MediaFinding(
        artefact=name, media_type="image", tier=InspectionTier.NONE,
        uninspected_reason=UninspectedReason.NO_CAPABLE_BACKEND,
        metadata_findings=["GPS coordinates present"],
        sensitivity=SensitivityClass.SENSITIVE, presumed=True)


def _proposal(location="village"):
    return RedactionProposal(
        artefact="responses.csv", location=location,
        reason_code=ReasonCode.PERSONAL_DATA, treatment=Treatment.COARSEN,
        evidence="village combined with role identifies one person")


# -- the gate blocks; the workflow does not ------------------------------

def test_unresolved_items_block_deposit(researcher):
    gate = Gate()
    gate.add(from_media_findings([_uninspected_finding()]))
    assert gate.blocks_deposit() is True
    gate.resolve("uninspected:a.jpg", ItemDecision.EXCLUDE_FROM_DEPOSIT,
                 human=researcher)
    assert gate.blocks_deposit() is False


def test_items_from_several_sources_are_reviewed_together(researcher):
    """Deciding 'can we publish this .fits?' in isolation is a worse decision
    than deciding it alongside everything else known about the deposit."""
    gate = Gate()
    gate.add(from_media_findings([_uninspected_finding("scan.jpg")]))
    gate.add(from_redaction_proposals([_proposal(), _proposal("dob")]))
    assert len(gate.unresolved()) == 3
    assert {i.kind for i in gate.unresolved()} == {
        GateItemKind.UNINSPECTED_FILE, GateItemKind.REDACTION_PROPOSAL}


# -- no bulk accept -------------------------------------------------------

def test_the_gate_has_no_bulk_accept():
    """A reviewer who can accept forty items with one call has reviewed nothing.

    The absence is the contract, so the test checks the interface rather than a
    behaviour.
    """
    for name, member in _inspect.getmembers(Gate, _inspect.isfunction):
        if name.startswith("_"):
            continue
        params = _inspect.signature(member).parameters
        for param in params.values():
            annotation = str(param.annotation)
            assert "list" not in annotation.lower() or name == "add", (
                f"Gate.{name} takes {param.name}: {annotation}; a method "
                "accepting several items at once is bulk accept by another name"
            )


def test_each_redaction_is_its_own_item():
    """Grouping proposals is bulk accept by another name."""
    items = from_redaction_proposals([_proposal("village"), _proposal("dob"),
                                      _proposal("clinician")])
    assert len({i.item_id for i in items}) == 3


# -- decisions are human acts with reasons where it matters ---------------

def test_publishing_an_uninspected_file_requires_a_recorded_reason(researcher):
    """It is a person accepting responsibility for material nobody looked at."""
    gate = Gate()
    gate.add(from_media_findings([_uninspected_finding()]))
    with pytest.raises(AuthorityError, match="requires a recorded reason"):
        gate.resolve("uninspected:a.jpg", ItemDecision.PUBLISH_AS_IS,
                     human=researcher)
    r = gate.resolve("uninspected:a.jpg", ItemDecision.PUBLISH_AS_IS,
                     human=researcher, reason="opened it myself; landscape only")
    assert r.decided_by == researcher and r.reason


def test_a_decision_not_offered_is_refused(researcher):
    gate = Gate()
    gate.add(from_redaction_proposals([_proposal()]))
    with pytest.raises(AuthorityError, match="not available"):
        gate.resolve("redaction:responses.csv:village",
                     ItemDecision.PUBLISH_AS_IS, human=researcher)


def test_an_item_with_no_permitted_decisions_is_rejected_at_construction():
    with pytest.raises(ValueError, match="offers no decisions"):
        GateItem(item_id="x", kind=GateItemKind.UNINSPECTED_FILE, summary="s",
                 permitted_decisions=[])


def test_uninspected_summary_says_it_was_not_inspected():
    """An item that reads like a finding invites approval on the assumption that
    someone checked."""
    item = from_media_findings([_uninspected_finding()])[0]
    assert "not inspected" in item.summary
    assert any("presumption" in d or "not on inspection" in d for d in item.detail)


def test_excluded_artefacts_are_reported(researcher):
    gate = Gate()
    gate.add(from_media_findings([_uninspected_finding("a.jpg"),
                                  _uninspected_finding("b.jpg")]))
    gate.resolve("uninspected:a.jpg", ItemDecision.EXCLUDE_FROM_DEPOSIT,
                 human=researcher)
    gate.resolve("uninspected:b.jpg", ItemDecision.CLASSIFY_SENSITIVE,
                 human=researcher)
    assert gate.state.excluded_artefacts() == ["a.jpg"]


def test_resolution_events_name_the_human(researcher, job_id):
    gate = Gate()
    gate.add(from_redaction_proposals([_proposal()]))
    gate.resolve("redaction:responses.csv:village", ItemDecision.APPROVE,
                 human=researcher)
    events = gate.resolution_events(job_id, "gate/0.1.0")
    assert events[0].human == researcher


# -- the redaction agent proposes and cannot apply ------------------------

def test_redaction_agent_has_no_apply_method():
    """ADR-013 rests on this: the agent produces proposals and has no way to
    put one into effect."""
    public = {m for m in dir(RedactionAgent) if not m.startswith("_")}
    assert not public & {"apply", "redact", "anonymise", "anonymize", "write"}


def test_proposals_prefer_least_destructive_treatments(job_id):
    reply = json.dumps({"proposals": [
        {"location": "village", "treatment": "coarsen",
         "reason_code": "personal-data", "evidence": "identifies one person"},
        {"location": "dob", "treatment": "generalise",
         "reason_code": "personal-data", "evidence": "date of birth"},
    ]})
    model = ScriptedModel(reply)
    agent = RedactionAgent(PolicyEnforcementPoint(policy(), {"local-text": model}))
    proposals, decision = agent.propose(
        _state(job_id, SensitivityClass.SENSITIVE), "responses.csv", ["finding"])
    assert [p.treatment for p in proposals] == [Treatment.COARSEN,
                                                Treatment.GENERALISE]
    assert "individual human approval" in decision.selection_basis


def test_unreadable_proposals_are_dropped_not_invented(job_id):
    """Inventing one would put a change in front of a reviewer that the agent
    never proposed."""
    reply = json.dumps({"proposals": [
        {"location": "village", "treatment": "obliterate",
         "reason_code": "personal-data", "evidence": "x"},
        {"location": "dob", "treatment": "coarsen",
         "reason_code": "not-a-reason", "evidence": "y"},
        {"location": "name", "treatment": "suppress",
         "reason_code": "personal-data", "evidence": "z"},
    ]})
    model = ScriptedModel(reply)
    agent = RedactionAgent(PolicyEnforcementPoint(policy(), {"local-text": model}))
    proposals, _ = agent.propose(_state(job_id, SensitivityClass.SENSITIVE),
                                 "responses.csv", ["finding"])
    assert [p.location for p in proposals] == ["name"]


def test_proposal_evidence_describes_rather_than_quotes(job_id):
    """The proposal travels through the open register; quoting would move the
    content into it."""
    agent = RedactionAgent(PolicyEnforcementPoint(
        policy(), {"local-text": ScriptedModel(json.dumps({"proposals": []}))}))
    agent.propose(_state(job_id, SensitivityClass.SENSITIVE), "a.csv", ["f"])
    assert "Do not quote the data" in RedactionAgent.__dict__.get(
        "propose").__doc__ or True  # documented in the prompt
    from datadirector.agents.redaction import PROPOSE_PROMPT
    assert "Do not quote the data" in PROPOSE_PROMPT


# -- Content tier: images actually reaching a vision backend ---------------

from datadirector_contracts import (  # noqa: E402
    ImageAttachment, ModelRequest, ReleaseKind as _RK,
)

from datadirector.backends.base import (  # noqa: E402
    build_messages, capabilities_from_config,
)


class VisionModel:
    """A backend declaring vision, recording what it was sent."""

    name = "local-vision"

    def __init__(self, reply):
        self.reply = reply
        self.seen = []

    def residency(self):
        return Residency.ON_PREMISE

    def capabilities(self):
        return {TEXT, VISION}

    def complete(self, request):
        self.seen.append(request)
        return ModelResponse(text=self.reply, model_id=self.name,
                             input_digest=Digest.of_bytes(b""))


def _vision_agent(tmp_path, model, ledger=None):
    pep = PolicyEnforcementPoint(
        PolicyConfig(backend_by_sensitivity={
            SensitivityClass.PUBLIC: ["v"],
            SensitivityClass.INTERNAL: ["v"],
            SensitivityClass.SENSITIVE: ["v"],
        }), {"v": model})
    return MediaAgent(pep, tmp_path, ledger=ledger)


def test_images_attach_to_the_user_turn_never_the_system_turn():
    """Text rendered in an image is instruction-shaped material a vision model
    reads. An image in the system position is a visual injection channel."""
    messages = build_messages(ModelRequest(
        system="You describe images.", user_content="Image artefact: a.jpg",
        trusted_instructions="Use DataCite.",
        images=[ImageAttachment(media_type="image/png", data_base64="AAAA")]))
    system = [m for m in messages if m["role"] == "system"]
    user = [m for m in messages if m["role"] == "user"]
    assert all("images" not in m for m in system)
    assert user[0]["images"] == ["AAAA"]


def test_an_inspected_image_is_not_a_presumption(tmp_path, job_id):
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    model = VisionModel(json.dumps({
        "observations": ["a boat at a wharf, no people"], "faces_visible": False,
        "identifying_text_visible": False, "sensitivity": "public"}))
    finding = _vision_agent(tmp_path, model).inspect(_state(job_id, PUBLIC), "a.jpg")
    assert finding.tier is InspectionTier.CONTENT
    assert finding.presumed is False
    assert finding.sensitivity is SensitivityClass.PUBLIC


def test_a_visible_face_tightens_regardless_of_the_overall_verdict(tmp_path, job_id):
    """A face is personal data whatever the model concluded overall."""
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    model = VisionModel(json.dumps({
        "observations": ["two people at a table"], "faces_visible": True,
        "identifying_text_visible": False, "sensitivity": "public"}))
    finding = _vision_agent(tmp_path, model).inspect(_state(job_id, PUBLIC), "a.jpg")
    assert finding.sensitivity is SensitivityClass.SENSITIVE


def test_an_unreadable_vision_reply_is_not_an_inspection(tmp_path, job_id):
    """Recording it as one would produce the false assurance this tier prevents."""
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    finding = _vision_agent(tmp_path, VisionModel("I can't see that")).inspect(
        _state(job_id, PUBLIC), "a.jpg")
    assert finding.tier is InspectionTier.NONE
    assert finding.presumed is True


def test_image_inspection_is_charged_to_the_ledger(tmp_path, job_id):
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8" + b"x" * 500 + b"\xff\xd9")
    ledger = ExposureLedger(tmp_path / "e")
    model = VisionModel(json.dumps({"observations": [], "faces_visible": False,
                                    "identifying_text_visible": False,
                                    "sensitivity": "public"}))
    _vision_agent(tmp_path, model, ledger=ledger).inspect(
        _state(job_id, PUBLIC), "a.jpg")
    exposures = ledger.load(job_id)
    assert exposures[0].kind is _RK.MEDIA_CONTENT
    assert exposures[0].byte_count > 500


def test_declared_capabilities_are_parsed_and_unknown_names_refused():
    assert VISION in capabilities_from_config(["vision"])
    assert TEXT in capabilities_from_config(["vision"]), "text is always implied"
    assert VISION not in capabilities_from_config(None)
    with pytest.raises(ValueError, match="unknown model capability"):
        capabilities_from_config(["clairvoyance"])


# -- A model that cannot resolve an image is not an inspection -------------

from datadirector.media.imagestats import measure  # noqa: E402

pytest.importorskip("PIL", reason="Pillow is needed to build image fixtures")


def _text_image_bytes(lines, width=900, line_height=34):
    from PIL import Image, ImageDraw, ImageFont
    import io
    image = Image.new("RGB", (width, line_height * (len(lines) + 2)), (250, 250, 248))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    for i, line in enumerate(lines):
        draw.text((30, line_height * (i + 1)), line, fill=(20, 20, 20), font=font)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_measurement_distinguishes_text_from_a_blank_field(tmp_path):
    blank = tmp_path / "blank.png"
    from PIL import Image
    Image.new("RGB", (900, 300), (250, 250, 248)).save(blank)
    (tmp_path / "text.png").write_bytes(_text_image_bytes(
        ["Participant name: Miriam Aroa", "Date of birth: 14 August 1971"]))

    assert measure(blank).has_substantial_content is False
    text = measure(tmp_path / "text.png")
    assert text.has_substantial_content is True
    assert text.looks_like_text is True


def test_a_model_reporting_blank_for_a_page_of_text_is_not_an_inspection(
        tmp_path, job_id):
    """Observed in live testing: a rendered consent form carrying a name, a date
    of birth and a telephone number was described as 'a uniform blank near-white
    field with no visible content'.

    The finding came back as inspected, not presumed, and classified public.
    That reads as 'checked and clean' when in fact the reader could not resolve
    the content, which is the false assurance this tier exists to prevent.
    """
    (tmp_path / "consent.png").write_bytes(_text_image_bytes(
        ["MATERNAL HEALTH STUDY — CONSENT",
         "Participant name: Miriam Aroa",
         "Date of birth: 14 August 1971",
         "Contact: +675 7xx xxx xx"]))
    model = VisionModel(json.dumps({
        "observations": ["The image is a uniform blank near-white field with no "
                         "visible content, people, text, or objects."],
        "faces_visible": False, "identifying_text_visible": False,
        "sensitivity": "public"}))
    finding = _vision_agent(tmp_path, model).inspect(_state(job_id, PUBLIC), "consent.png")

    assert finding.tier is InspectionTier.NONE
    assert finding.uninspected_reason is UninspectedReason.CONTENT_NOT_RESOLVABLE
    assert finding.presumed is True
    assert finding.sensitivity is SensitivityClass.SENSITIVE
    assert any("reported it as empty" in f for f in finding.metadata_findings)


def test_a_genuinely_blank_image_reported_blank_is_an_inspection(tmp_path, job_id):
    """The contradiction check must not fire on an image that really is empty."""
    from PIL import Image
    Image.new("RGB", (400, 300), (250, 250, 248)).save(tmp_path / "blank.png")
    model = VisionModel(json.dumps({
        "observations": ["A uniform blank field with no content."],
        "faces_visible": False, "identifying_text_visible": False,
        "sensitivity": "public"}))
    finding = _vision_agent(tmp_path, model).inspect(_state(job_id, PUBLIC), "blank.png")
    assert finding.tier is InspectionTier.CONTENT
    assert finding.presumed is False


def test_a_model_that_saw_something_is_believed(tmp_path, job_id):
    """The check fires on claims of emptiness only, not on every verdict."""
    (tmp_path / "photo.png").write_bytes(_text_image_bytes(["Some visible text"]))
    model = VisionModel(json.dumps({
        "observations": ["A page of printed text with a name and a date"],
        "faces_visible": False, "identifying_text_visible": True,
        "sensitivity": "sensitive"}))
    finding = _vision_agent(tmp_path, model).inspect(_state(job_id, PUBLIC), "photo.png")
    assert finding.tier is InspectionTier.CONTENT
    assert finding.sensitivity is SensitivityClass.SENSITIVE


def test_an_unverifiable_claim_of_emptiness_is_recorded_as_such(tmp_path, job_id,
                                                               monkeypatch):
    """When the contradiction check cannot run, the record must say so.

    An unmade check must not look like a check that passed. This is the same
    rule the tier applies to the model, applied to the tier itself.
    """
    from datadirector.agents import media as media_module
    from datadirector.media.imagestats import ImageStructure

    (tmp_path / "a.png").write_bytes(_text_image_bytes(["Some text here"]))
    monkeypatch.setattr(media_module, "measure",
                        lambda *_a, **_k: ImageStructure(available=False))
    model = VisionModel(json.dumps({
        "observations": ["A uniform blank field."], "faces_visible": False,
        "identifying_text_visible": False, "sensitivity": "public"}))
    finding = _vision_agent(tmp_path, model).inspect(_state(job_id, PUBLIC), "a.png")
    assert any("could not be verified" in f for f in finding.metadata_findings)


def test_text_fixtures_refuse_to_render_blank():
    """The harness bug that produced three meaningless live runs.

    text_png once fell back to a blank canvas when Pillow was missing. A vision
    model then correctly described the result as empty, and two rounds of
    interpretation were built on it before the cause was found. A fixture that
    cannot be built must fail loudly rather than render something else.
    """
    import sys
    from tests.live import _images

    png = _images.text_png(["Participant name: Miriam Aroa"])
    from datadirector.media.imagestats import measure
    path = Path("/tmp/_fixture_check.png")
    path.write_bytes(png)
    assert measure(path).looks_like_text, (
        "the text fixture rendered without legible text"
    )

    monkey = {k: v for k, v in sys.modules.items()}
    try:
        sys.modules["PIL"] = None
        sys.modules["PIL.Image"] = None
        with pytest.raises(Exception):
            _images.text_png(["anything"])
    finally:
        sys.modules.clear()
        sys.modules.update(monkey)
