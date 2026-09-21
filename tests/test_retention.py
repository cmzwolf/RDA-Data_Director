"""Retention: what may be deleted, and on what evidence.

The governing fact is that our working copy may be the only copy. Ingestion
copies from the watched folder; a researcher who then clears their own folder
has nothing else. So these tests are mostly about what the sweeper *refuses* to
do.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from datadirector_contracts import (
    EffectKind, Event, EventKind, Orcid, RetentionCategory,
)

from datadirector.errors import AuthorityError
from datadirector.retention.sweeper import TOMBSTONE, RetentionSweeper
from datadirector.state.store import EventStore

NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)

# Job identifiers are ULID-shaped and validated: Crockford base32, which omits
# I, L, O and U. The fixtures therefore build real ones from a counter rather
# than readable stand-ins, because the validator refuses the stand-ins and is
# right to.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ISSUED: dict[str, str] = {}


def job(marker: str) -> str:
    """A distinct, well-formed job identifier for this marker."""
    if marker not in _ISSUED:
        index = len(_ISSUED)
        _ISSUED[marker] = ("job-" + ALPHABET[index % 32] * 4
                           + ALPHABET[:22])
    return _ISSUED[marker]


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "work").mkdir()
    return tmp_path


@pytest.fixture
def store(workspace):
    return EventStore(workspace / "state")


CARBERRY = Orcid(value="0000-0002-1825-0097")

# Events that are human acts must name a human; the Event type refuses
# otherwise, which is the cluster-1 rule and not something to work around here.
HUMAN_ACT_KINDS = {EventKind.DEPOSIT_COMPLETED, EventKind.METADATA_APPROVED,
                   EventKind.DECLARATION_CONFIRMED, EventKind.REDACTION_DECIDED}


def _job(store, job_id, *kinds_and_payloads, human=None, at=None):
    store.append(Event(sequence=1, job_id=job_id,
                       kind=EventKind.WORKFLOW_CREATED, agent="test/1.0"))
    for kind, payload in kinds_and_payloads:
        actor = human or (CARBERRY if kind in HUMAN_ACT_KINDS else None)
        store.append(Event(sequence=1, job_id=job_id, kind=kind,
                           agent="test/1.0", human=actor, payload=payload,
                           occurred_at=at or NOW))


def _material(workspace, job_id, files=("observations.csv",)):
    directory = workspace / "work" / job_id
    directory.mkdir(parents=True, exist_ok=True)
    for name in files:
        (directory / name).write_text("station,temp\nS14,4.1\n")
    return directory


def _sweeper(store, workspace, probers=None):
    return RetentionSweeper(store, workspace / "work", probers=probers or {})


def _published_prober(finding="took-effect", detail="record exists"):
    return {EffectKind.REPOSITORY_PUBLISH: lambda intent: (finding, detail)}


# -- categorising from the log, not the filesystem ------------------------

def test_a_published_job_is_released(store, workspace):
    _job(store, job("a"), (EventKind.DEPOSIT_COMPLETED,
                          {"pid": "10.5072/zenodo.1"}), human=None)
    _material(workspace, job("a"))
    candidate = _sweeper(store, workspace).candidates(now=NOW)[0]
    assert candidate.category is RetentionCategory.RELEASED
    assert candidate.pid == "10.5072/zenodo.1"


def test_idle_is_not_abandoned(store, workspace):
    """Work left at the approval gate for three weeks is live work, and the
    difference is invisible from outside."""
    _job(store, job("b"), (EventKind.VALIDATION_COMPLETED, {}),
         at=NOW - timedelta(days=21))
    _material(workspace, job("b"))
    candidate = _sweeper(store, workspace).candidates(now=NOW)[0]
    assert candidate.category is RetentionCategory.ACTIVE


def test_long_silence_is_idle_and_still_not_deletable(store, workspace):
    _job(store, job("c"), (EventKind.VALIDATION_COMPLETED, {}),
         at=NOW - timedelta(days=200))
    _material(workspace, job("c"))
    candidate = _sweeper(store, workspace).candidates(now=NOW)[0]
    assert candidate.category is RetentionCategory.IDLE
    assert candidate.deletable_automatically is False


def test_a_directory_with_no_job_is_unattributed_not_rubbish(store, workspace):
    """We do not know what it is, which is a reason for caution."""
    _material(workspace, job("zz"))
    candidate = _sweeper(store, workspace).candidates(now=NOW)[0]
    assert candidate.category is RetentionCategory.UNATTRIBUTED
    assert candidate.deletable_automatically is False


def test_a_deliberate_decision_not_to_publish_is_its_own_category(store,
                                                                  workspace):
    _job(store, job("d"), (EventKind.WORKFLOW_CLOSED_NOT_SHARED,
                          {"reason": "the plan commits to not sharing"}))
    _material(workspace, job("d"))
    candidate = _sweeper(store, workspace).candidates(now=NOW)[0]
    assert candidate.category is RetentionCategory.CLOSED_NOT_SHARED
    assert candidate.deletable_automatically is False


# -- evidence, not belief -------------------------------------------------

def test_a_published_job_is_deletable_only_once_survival_is_confirmed(store,
                                                                      workspace):
    _job(store, job("e"), (EventKind.DEPOSIT_COMPLETED, {"pid": "10.5072/x"}))
    _material(workspace, job("e"))

    unverified = _sweeper(store, workspace).assess(now=NOW)[0]
    assert unverified.survival.checked is False
    assert unverified.deletable_automatically is False, (
        "a PID in our log is a belief; deletion needs evidence")

    verified = _sweeper(store, workspace, _published_prober()).assess(now=NOW)[0]
    assert verified.deletable_automatically is True


def test_a_withdrawn_record_makes_deletion_unsafe(store, workspace):
    """Our log says published, the repository says otherwise. This may now be
    the only copy."""
    _job(store, job("f"), (EventKind.DEPOSIT_COMPLETED, {"pid": "10.5072/y"}))
    _material(workspace, job("f"))
    sweeper = _sweeper(store, workspace,
                       _published_prober("did-not-take-effect", "no such record"))
    candidate = sweeper.assess(now=NOW)[0]
    assert candidate.survival.survives is False
    assert candidate.deletable_automatically is False
    assert "only copy" in candidate.survival.detail


def test_an_unreachable_repository_blocks_automatic_deletion(store, workspace):
    """Not knowing is not permission."""
    def broken(intent):
        raise OSError("unreachable")

    _job(store, job("g"), (EventKind.DEPOSIT_COMPLETED, {"pid": "10.5072/z"}))
    _material(workspace, job("g"))
    sweeper = _sweeper(store, workspace, {EffectKind.REPOSITORY_PUBLISH: broken})
    candidate = sweeper.assess(now=NOW)[0]
    assert candidate.survival.survives is None
    assert candidate.deletable_automatically is False


# -- marking, and the window it leaves ------------------------------------

def test_marking_leaves_an_explanation_a_researcher_can_act_on(store, workspace):
    _job(store, job("h"), (EventKind.DEPOSIT_COMPLETED, {"pid": "10.5072/a"}))
    directory = _material(workspace, job("h"))
    sweeper = _sweeper(store, workspace, _published_prober())
    mark = sweeper.mark(sweeper.assess(now=NOW)[0], now=NOW)

    tombstone = (directory / TOMBSTONE).read_text()
    assert "scheduled for deletion" in tombstone
    assert "remove this file and the deletion is\\ncancelled" in tombstone \
        or "cancelled" in tombstone
    assert mark.delete_after > mark.marked_at


def test_removing_the_tombstone_cancels_the_deletion(store, workspace):
    """Returning to the work should be enough to keep it."""
    _job(store, job("i"), (EventKind.DEPOSIT_COMPLETED, {"pid": "10.5072/b"}))
    directory = _material(workspace, job("i"))
    sweeper = _sweeper(store, workspace, _published_prober())
    sweeper.mark(sweeper.assess(now=NOW)[0], now=NOW)
    assert sweeper.read_mark(directory) is not None

    sweeper.unmark(directory)
    later = NOW + timedelta(days=400)
    assert sweeper.due(now=later) == []


def test_an_unreadable_tombstone_is_not_a_licence_to_delete(store, workspace):
    _job(store, job("j"), (EventKind.DEPOSIT_COMPLETED, {"pid": "10.5072/c"}))
    directory = _material(workspace, job("j"))
    (directory / TOMBSTONE).write_text("{ this is not json")
    sweeper = _sweeper(store, workspace, _published_prober())
    assert sweeper.read_mark(directory) is None
    assert sweeper.due(now=NOW + timedelta(days=400)) == []


def test_an_active_job_cannot_be_marked(store, workspace):
    _job(store, job("k"), (EventKind.VALIDATION_COMPLETED, {}))
    _material(workspace, job("k"))
    sweeper = _sweeper(store, workspace)
    with pytest.raises(AuthorityError, match="active job"):
        sweeper.mark(sweeper.candidates(now=NOW)[0], now=NOW)


def test_closed_not_shared_is_never_marked_by_a_schedule(store, workspace,
                                                         researcher):
    """Nothing else holds this material, so a person must order its removal."""
    _job(store, job("l"), (EventKind.WORKFLOW_CLOSED_NOT_SHARED, {"reason": "x"}))
    _material(workspace, job("l"))
    sweeper = _sweeper(store, workspace)
    candidate = sweeper.candidates(now=NOW)[0]

    with pytest.raises(AuthorityError, match="a person must order"):
        sweeper.mark(candidate, now=NOW)

    mark = sweeper.mark(candidate, now=NOW, human=researcher)
    assert mark.marked_by == researcher


# -- deleting --------------------------------------------------------------

def test_nothing_is_deleted_before_the_grace_period(store, workspace):
    _job(store, job("m"), (EventKind.DEPOSIT_COMPLETED, {"pid": "10.5072/d"}))
    directory = _material(workspace, job("m"))
    sweeper = _sweeper(store, workspace, _published_prober())
    sweeper.mark(sweeper.assess(now=NOW)[0], now=NOW)

    assert sweeper.due(now=NOW + timedelta(days=1)) == []
    assert sweeper.due(now=NOW + timedelta(days=31))
    assert directory.exists()


def test_deletion_records_what_was_removed(store, workspace):
    """The audit trail describes the deletion without reconstructing what was
    deleted."""
    _job(store, job("n"), (EventKind.DEPOSIT_COMPLETED, {"pid": "10.5072/e"}))
    directory = _material(workspace, job("n"), files=("a.csv", "b.csv"))
    sweeper = _sweeper(store, workspace, _published_prober())
    sweeper.mark(sweeper.assess(now=NOW)[0], now=NOW)

    later = NOW + timedelta(days=31)
    path, mark = sweeper.due(now=later)[0]
    record = sweeper.delete(path, mark)

    assert not directory.exists()
    assert record.file_count == 2
    assert len(record.digests) == 2
    assert record.pid == "10.5072/e"
    assert all(len(d.value) == 64 for d in record.digests)


def test_deletion_is_bracketed_by_an_intent(store, workspace):
    """A crash part-way through leaves a partially deleted directory that
    nothing would otherwise record."""
    _job(store, job("o"), (EventKind.DEPOSIT_COMPLETED, {"pid": "10.5072/f"}))
    _material(workspace, job("o"))
    sweeper = _sweeper(store, workspace, _published_prober())
    sweeper.mark(sweeper.assess(now=NOW)[0], now=NOW)
    path, mark = sweeper.due(now=NOW + timedelta(days=31))[0]
    sweeper.delete(path, mark)

    kinds = [e.kind for e in store.load(job("o"))]
    assert EventKind.STEP_STARTED in kinds
    assert EventKind.STEP_COMPLETED in kinds


def test_a_schedule_cannot_delete_what_only_a_person_may(store, workspace,
                                                         researcher):
    _job(store, job("p"), (EventKind.WORKFLOW_CLOSED_NOT_SHARED, {"reason": "x"}))
    _material(workspace, job("p"))
    sweeper = _sweeper(store, workspace)
    candidate = sweeper.candidates(now=NOW)[0]
    mark = sweeper.mark(candidate, now=NOW, human=researcher)
    path = Path(candidate.path)

    with pytest.raises(AuthorityError, match="a person must order"):
        sweeper.delete(path, mark.model_copy(update={"marked_by": None}))

    record = sweeper.delete(path, mark, ordered_by=researcher)
    assert record.ordered_by == researcher
    assert not path.exists()


def test_an_absent_working_root_is_not_an_error(store, tmp_path):
    """Nothing ingested yet is not a failure; there is simply nothing to sweep."""
    sweeper = RetentionSweeper(store, tmp_path / "never-created")
    assert sweeper.candidates(now=NOW) == []
    assert sweeper.due(now=NOW) == []
