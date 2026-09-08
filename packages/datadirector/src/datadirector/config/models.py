"""Configuration types.

Two surfaces with different owners (ADR-018): wiring belongs to the system
administrator and changes at deployment; policy belongs to the data steward and
must be auditable. `PolicyConfig` lives in the contracts package because the
Policy Enforcement Point contract refers to it; wiring is internal.
"""

from __future__ import annotations

import json
from enum import StrEnum

from datadirector_contracts import Digest, PolicyConfig, Residency
from datadirector_contracts.policy import BackendDeclaration
from pydantic import BaseModel, ConfigDict, Field


class DeploymentProfile(StrEnum):
    """Scopes non-functional requirements to where they are meaningful (§12)."""

    SINGLE_USER_LOCAL = "single-user-local"
    INSTITUTIONAL = "institutional"
    NATIONAL = "national"


class PluginDeclaration(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    enabled: bool = True
    settings: dict[str, str] = Field(default_factory=dict)


class StoragePaths(BaseModel):
    model_config = ConfigDict(frozen=True)

    state_root: str
    working_root: str
    restricted_root: str
    watched_folder: str | None = None


class WiringConfig(BaseModel):
    """Owned by the system administrator. Holds no secret values.

    Credential fields are the *names* of environment variables. The type has no
    field capable of carrying a secret, which is the enforcement rather than a
    convention.
    """

    model_config = ConfigDict(frozen=True)

    profile: DeploymentProfile
    storage: StoragePaths
    backends: list[BackendDeclaration]
    plugins: list[PluginDeclaration] = Field(default_factory=list)
    credential_env_vars: dict[str, str] = Field(
        default_factory=dict,
        description="Scope -> environment variable NAME, e.g. "
        "{'zenodo:deposit': 'DD_ZENODO_TOKEN'}. Never a value.",
    )


class InstalledPlugin(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    protocol: str
    requirements_supported: list[str] = Field(default_factory=list)
    residency: Residency | None = None
    offline_capable: bool = False


class ResolvedConfig(BaseModel):
    """The fully materialised startup state, hashed into provenance (§11.1).

    This is what ties a published dataset to the exact software configuration
    that produced its metadata. It must serialise canonically, or the same
    configuration would hash differently on two machines and the reproducibility
    claim would be false.
    """

    model_config = ConfigDict(frozen=True)

    profile: DeploymentProfile
    wiring: WiringConfig
    policy: PolicyConfig
    installed_plugins: list[InstalledPlugin]
    application_version: str

    def digest(self) -> Digest:
        return Digest.of_canonical_json(json.loads(self.model_dump_json()))

    def capability_report(self) -> dict[str, list[str]]:
        """Which Blueprint requirements the installed plugin set can serve.

        A deployment lacking a DMPSource reports R8 as unavailable rather than
        silently omitting it (§6.3).
        """
        served: dict[str, list[str]] = {}
        for p in self.installed_plugins:
            for req in p.requirements_supported:
                served.setdefault(req, []).append(p.name)
        return dict(sorted(served.items()))
