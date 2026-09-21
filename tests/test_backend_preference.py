"""What a depositor may ask for, within what policy permits.

The constraint that carries the design: a preference may only **narrow**. Policy
decides which backends may see material at a given sensitivity; a preference
chooses among them and can never add one. A preference that could widen would be
a policy override wearing a friendlier name.
"""

from __future__ import annotations

import pytest
from datadirector_contracts import (
    BackendPreference, ModelCapability, PolicyConfig, PolicyHalt,
    PreferenceUnsatisfiable, Residency, SensitivityClass,
)

from datadirector.policy.pep import PolicyEnforcementPoint


class _Backend:
    def __init__(self, name: str, residency: Residency,
                 capabilities=None) -> None:
        self.name = name
        self._residency = residency
        self._capabilities = capabilities or {ModelCapability.TEXT_GENERATION}

    def residency(self):
        return self._residency

    def capabilities(self):
        return self._capabilities


@pytest.fixture
def pep():
    return PolicyEnforcementPoint(
        PolicyConfig(backend_by_sensitivity={
            SensitivityClass.PUBLIC: ["local", "regional", "remote"],
            SensitivityClass.INTERNAL: ["local", "regional"],
            SensitivityClass.SENSITIVE: ["local"],
        }),
        {"local": _Backend("local", Residency.ON_PREMISE),
         "regional": _Backend("regional", Residency.IN_JURISDICTION),
         "remote": _Backend("remote", Residency.EXTRA_JURISDICTION)})


# -- the constraint --------------------------------------------------------

def test_a_preference_cannot_reach_a_backend_policy_forbids(pep):
    """The whole point. Sensitive material permits only the local backend, and
    asking for the remote one does not make it reachable."""
    chosen = pep.resolve_backend(
        SensitivityClass.SENSITIVE, ModelCapability.TEXT_GENERATION,
        prefer=BackendPreference(backend_names=["remote"]))
    assert chosen.name == "local"


def test_a_preference_can_choose_among_permitted(pep):
    """Policy permits three for public material; the depositor picks."""
    chosen = pep.resolve_backend(
        SensitivityClass.PUBLIC, prefer=BackendPreference(
            backend_names=["remote"]))
    assert chosen.name == "remote"


def test_without_a_preference_policy_order_applies(pep):
    """Most restrictive residency wins, as before."""
    assert pep.resolve_backend(SensitivityClass.PUBLIC).name == "local"


def test_preference_order_is_honoured(pep):
    chosen = pep.resolve_backend(
        SensitivityClass.PUBLIC,
        prefer=BackendPreference(backend_names=["regional", "remote"]))
    assert chosen.name == "regional"


# -- residency is the depositor's judgement --------------------------------

def test_a_residency_ceiling_narrows(pep):
    """'I would rather this never left the building' is a judgement about their
    own material, and they are better placed to make it than the system is."""
    chosen = pep.resolve_backend(
        SensitivityClass.PUBLIC,
        prefer=BackendPreference(residency_at_most=Residency.IN_JURISDICTION,
                                 backend_names=["remote"]))
    assert chosen.name != "remote", "the ceiling did not narrow"


def test_an_unsatisfiable_ceiling_halts_rather_than_falling_back(pep):
    """Silently ignoring "keep this local" and sending the material abroad is a
    worse outcome than stopping."""
    only_remote = PolicyEnforcementPoint(
        PolicyConfig(backend_by_sensitivity={
            level: ["remote"] for level in SensitivityClass}),
        {"remote": _Backend("remote", Residency.EXTRA_JURISDICTION)})

    with pytest.raises(PreferenceUnsatisfiable) as exc:
        only_remote.resolve_backend(
            SensitivityClass.PUBLIC,
            prefer=BackendPreference(residency_at_most=Residency.ON_PREMISE))
    assert "Nothing has been sent anywhere" in str(exc.value)


def test_an_unsatisfiable_preference_is_a_policy_halt(pep):
    """So existing halt handling covers it rather than it escaping as a
    surprise."""
    assert issubclass(PreferenceUnsatisfiable, PolicyHalt)


# -- unknown names ---------------------------------------------------------

def test_a_backend_that_does_not_exist_is_ignored_not_obeyed(pep):
    """Honouring an unknown name would mean halting a job because a researcher
    mistyped, which is a poor trade for no safety gain: policy has already
    decided what is permitted."""
    chosen = pep.resolve_backend(
        SensitivityClass.PUBLIC,
        prefer=BackendPreference(backend_names=["gpt-imaginary"]))
    assert chosen.name == "local"


def test_capability_still_filters_before_preference(pep):
    """A preference cannot select a backend that cannot do the job: capability
    is a filter, never a selector, and that ordering is the security property."""
    vision = PolicyEnforcementPoint(
        PolicyConfig(backend_by_sensitivity={
            level: ["text-only", "sighted"] for level in SensitivityClass}),
        {"text-only": _Backend("text-only", Residency.ON_PREMISE),
         "sighted": _Backend("sighted", Residency.EXTRA_JURISDICTION,
                             {ModelCapability.TEXT_GENERATION,
                              ModelCapability.VISION})})

    chosen = vision.resolve_backend(
        SensitivityClass.PUBLIC, ModelCapability.VISION,
        prefer=BackendPreference(backend_names=["text-only"]))
    assert chosen.name == "sighted", (
        "a preference selected a backend that cannot do the job")


def test_an_empty_preference_changes_nothing(pep):
    assert BackendPreference().is_empty
    assert pep.resolve_backend(
        SensitivityClass.PUBLIC, prefer=BackendPreference()).name == "local"


# ==========================================================================
# Saying what was not checked
# ==========================================================================

def test_an_unconsulted_registry_is_not_reported_as_an_empty_one():
    """The message said "no repository called 'Zenodo' was found in the
    registry" when no registry had been asked.

    That is the failure this project keeps meeting from the other side — an
    unasked question reported as a negative answer — and here it sat in a
    message someone reads while deciding whether to publish.
    """
    from datadirector.repository_choice import (
        RepositoryPreference, confirmation_item,
    )

    item = confirmation_item(
        RepositoryPreference(named="Zenodo", verbatim="deposit in Zenodo"),
        candidates=[])
    joined = " ".join(item.detail)
    assert "has not been consulted" in joined
    assert "was found in the registry" not in joined
    assert "holds no repository" not in joined


def test_a_consulted_registry_that_holds_nothing_says_that_instead():
    from datadirector.agents.repository import Candidate
    from datadirector.repository_choice import (
        RepositoryPreference, confirmation_item,
    )

    item = confirmation_item(
        RepositoryPreference(named="Zonodo"),
        candidates=[Candidate(identifier="r1", name="Zenodo")])
    joined = " ".join(item.detail)
    assert "the registry was consulted" in joined
