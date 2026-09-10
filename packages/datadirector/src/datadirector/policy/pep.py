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
    CapabilityUnavailable, ModelCapability, PolicyConfig, PolicyHalt,
    ProvActivity, Residency, SensitivityClass,
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
        self._resolved: set[str] = set()

    def permitted_backends(self, classification: SensitivityClass) -> list[str]:
        return list(self._policy.backend_by_sensitivity.get(classification, []))

    def resolve_backend(self, classification: SensitivityClass,
                        capability: ModelCapability = ModelCapability.TEXT_GENERATION):
        """Return the most restrictive permitted *and capable* backend.

        Three steps, in this order, and the order is the security property:

          1. policy filters   -- which backends may see material at this level
          2. capability filters -- which of those can do the job
          3. residency orders -- most restrictive of what remains

        Capability is a filter, never a selector. A backend that policy forbids
        is never reachable however uniquely capable it is, so widening the
        capability vocabulary can never widen what a classification permits.

        Halting is deliberate at both stages. Falling back to a
        permitted-but-less-appropriate backend would be a silent downgrade, and
        a deployment that cannot inspect the material it holds should be told so
        rather than quietly served.
        """
        names = self.permitted_backends(classification)
        permitted = [(self._backends[n], n) for n in names if n in self._backends]
        if not permitted:
            configured = sorted(self._backends)
            raise PolicyHalt(
                f"no permitted model backend is available for classification "
                f"{classification.label!r}. Policy permits {names or '(none)'}; "
                f"configured backends are {configured or '(none)'}. "
                "Configure a backend with acceptable residency, or the workflow "
                "cannot proceed for material at this classification."
            )

        capable = [(b, n) for b, n in permitted if capability in self._capabilities(b)]
        if not capable:
            # Distinct from PolicyHalt because the remedy is different: this is
            # not a residency problem, it is an inability to do the job at all.
            offer = {n: sorted(c.value for c in self._capabilities(b))
                     for b, n in permitted}
            raise CapabilityUnavailable(
                f"no backend permitted at {classification.label!r} provides "
                f"{capability.value!r}. Permitted backends offer: {offer}. "
                "Either configure a capable backend permitted at this "
                "classification, or the material cannot be inspected here and "
                "must be decided by a human at the approval gate."
            )

        capable.sort(key=lambda pair: RESIDENCY_ORDER.index(pair[0].residency()))
        backend, name = capable[0]
        self._resolved.add(name)
        self._record(classification, name, backend.residency(), capability)
        return backend

    @staticmethod
    def _capabilities(backend) -> set[ModelCapability]:
        """Backends predating the capability contract are assumed text-only.

        Assumed narrowly rather than broadly: a backend that has not declared
        vision should never be selected for vision because of a missing method.
        """
        getter = getattr(backend, "capabilities", None)
        if getter is None:
            return {ModelCapability.TEXT_GENERATION}
        return set(getter())

    def can(self, classification: SensitivityClass,
            capability: ModelCapability) -> bool:
        """Whether the job could be done at this classification.

        Used at startup and by the media agent to decide between inspecting and
        recording the material as uninspected, without raising and catching.
        """
        try:
            self.resolve_backend(classification, capability)
            return True
        except PolicyHalt:
            return False

    def _record(self, classification: SensitivityClass, name: str,
                residency: Residency, capability: ModelCapability) -> None:
        if self._recorder is None:
            return
        self._recorder(
            ProvActivity(
                activity_id=f"pep/{classification.label}/{capability.value}/{name}",
                activity_type="dd:BackendResolution",
                agent=ProvAgent(software="policy-enforcement-point/0.1.0"),
                visibility=Visibility.RESTRICTED,
            )
        )

    def models_used(self) -> list[str]:
        """The set of backends this job has resolved to.

        Recorded at job level so a reviewer asking "what saw this data?" gets one
        answer rather than reassembling it from decision records.
        """
        return sorted(self._resolved)
