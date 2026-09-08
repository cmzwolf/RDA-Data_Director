"""The provenance recorder: deterministic middleware, not an agent.

ADR-009. Provenance produced by a non-deterministic component is not audit
evidence, so no model appears anywhere in this module.

The recorder writes the PROV-O activity and appends the corresponding event
through the event store, so the provenance graph and the event log are two
projections of one sequence and cannot disagree.
"""

from __future__ import annotations

import json
from pathlib import Path

from datadirector_contracts import Event, ProvActivity, Visibility

from ..errors import DataDirectorError
from ..state.store import EventStore

PROV_CONTEXT = {
    "prov": "http://www.w3.org/ns/prov#",
    "dd": "https://w3id.org/datadirector/ns#",
    "used": "prov:used",
    "generated": "prov:generated",
    "wasAssociatedWith": "prov:wasAssociatedWith",
    "actedOnBehalfOf": "prov:actedOnBehalfOf",
    "startedAtTime": "prov:startedAtTime",
    "endedAtTime": "prov:endedAtTime",
}


class ProvenanceError(DataDirectorError):
    """An action occurred but could not be recorded.

    Not a degradation: an unrecorded action is a defect, because R10 makes the
    record a product of the system rather than a by-product.
    """


class Recorder:
    """Records activities and the events that accompany them."""

    def __init__(self, store: EventStore, graph_root: Path | str) -> None:
        self.store = store
        self.graph_root = Path(graph_root)
        self.graph_root.mkdir(parents=True, exist_ok=True)

    def _graph_path(self, job_id: str) -> Path:
        d = self.graph_root / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d / "activities.jsonl"

    def record(self, job_id: str, activity: ProvActivity, event: Event) -> Event:
        """Write the activity and append the event atomically enough.

        Order matters: the activity is written first, then the event. If the
        process dies between them the event is missing and the fold does not
        advance, which is recoverable. The reverse order could advance state
        past an unrecorded action, which is not.
        """
        self._assert_no_payload(activity)
        line = json.dumps(
            json.loads(activity.model_dump_json()),
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        with open(self._graph_path(job_id), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return self.store.append(event)

    @staticmethod
    def _assert_no_payload(activity: ProvActivity) -> None:
        """Entities are referenced by identifier and digest (commitment C-3).

        The types already forbid payload; this catches the case where a caller
        smuggles content into a free-text field that was meant for a label.
        """
        for ref in list(activity.used) + list(activity.generated):
            if ref.description and len(ref.description) > 512:
                raise ProvenanceError(
                    f"artefact description for {ref.uri} is {len(ref.description)} "
                    "characters; descriptions label derived artefacts, they do not "
                    "carry their content"
                )

    def load(self, job_id: str) -> list[ProvActivity]:
        path = self._graph_path(job_id)
        if not path.exists():
            return []
        return [ProvActivity.model_validate_json(l)
                for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def export(self, job_id: str, *, up_to: Visibility = Visibility.OPEN) -> dict:
        """Emit subgraphs at or below a clearance level, as PROV-O in JSON-LD.

        The result stays internally consistent at every level because restricted
        portions were only ever digests: nothing dangles when they are omitted.
        """
        order = {Visibility.OPEN: 0, Visibility.RESTRICTED: 1, Visibility.CONFIDENTIAL: 2}
        ceiling = order[up_to]
        activities = [a for a in self.load(job_id) if order[a.visibility] <= ceiling]
        return {
            "@context": PROV_CONTEXT,
            "@id": f"dd:job/{job_id}",
            "dd:visibility": up_to.value,
            "@graph": [json.loads(a.model_dump_json()) for a in activities],
        }
