"""The Policy Enforcement Point.

Document A §9.4. Every model call in the system passes through here. Agents
never name a backend: they ask for the most restrictive permitted one for the
job's current classification, and this resolves it.

Note what is absent. No public method accepts a backend name, so a misconfigured
or careless agent cannot express a bypass. The guarantee is structural rather
than a rule developers are asked to observe.
"""

from __future__ import annotations

from datadirector_contracts import (
    PolicyConfig, PolicyHalt, ProvActivity, Residency, SensitivityClass,
)
from datadirector_contracts.provenance import ProvAgent, Visibility

# Most restrictive first. A backend earlier in this order is preferred, so
# "the most restrictive permitted backend" is a total order and not a
# preference expressed in prose.
RESIDENCY_ORDER: list[Residency] = [
    Residency.ON_PREMISE,
    Residency.IN_JURISDICTION,
    Residency.EXTRA_JURISDICTION,
]


class PolicyEnforcementPoint:
    def __init__(self, policy: PolicyConfig, backends: dict[str, object],
                 recorder=None) -> None:
        self._policy = policy
        self._backends = dict(backends)
        self._recorder = recorder

    def permitted_backends(self, classification: SensitivityClass) -> list[str]:
        return list(self._policy.backend_by_sensitivity.get(classification, []))

    def resolve_backend(self, classification: SensitivityClass):
        """Return the most restrictive permitted backend, or raise PolicyHalt.

        Halting is deliberate. Falling back to a permitted-but-less-appropriate
        backend would be a silent downgrade, and a deployment intending to handle
        sensitive material without a local backend should be told so rather than
        quietly served.
        """
        names = self.permitted_backends(classification)
        candidates = [(self._backends[n], n) for n in names if n in self._backends]
        if not candidates:
            configured = sorted(self._backends)
            raise PolicyHalt(
                f"no permitted model backend is available for classification "
                f"{classification.label!r}. Policy permits {names or '(none)'}; "
                f"configured backends are {configured or '(none)'}. "
                "Configure a backend with acceptable residency, or the workflow "
                "cannot proceed for material at this classification."
            )
        candidates.sort(key=lambda pair: RESIDENCY_ORDER.index(pair[0].residency()))
        backend, name = candidates[0]
        self._record(classification, name, backend.residency())
        return backend

    def _record(self, classification: SensitivityClass, name: str,
                residency: Residency) -> None:
        if self._recorder is None:
            return
        self._recorder(
            ProvActivity(
                activity_id=f"pep/{classification.label}/{name}",
                activity_type="dd:BackendResolution",
                agent=ProvAgent(software="policy-enforcement-point/0.1.0"),
                visibility=Visibility.RESTRICTED,
            )
        )
