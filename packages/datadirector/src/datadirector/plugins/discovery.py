"""Plugin discovery through entry points.

Document A §6.2 and §6.3. A third party installs a package and the system finds
it; there is no central registry to maintain, which is what P9 and P7 want.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from datadirector_contracts import CapabilityManifest, Residency
from datadirector_contracts.plugins import (
    DMPSource, IdentityProvider, ModelBackend, RegistryDriver,
    RepositoryDriver, SchemaProfile, ValidatorDriver, VocabularyProvider,
)
from datadirector_contracts.containers import ContainerFormat

from ..errors import PluginError

ENTRY_POINT_GROUP = "datadirector.plugins"

PROTOCOLS: dict[str, type] = {
    "RepositoryDriver": RepositoryDriver,
    "SchemaProfile": SchemaProfile,
    "VocabularyProvider": VocabularyProvider,
    "ValidatorDriver": ValidatorDriver,
    "ModelBackend": ModelBackend,
    "RegistryDriver": RegistryDriver,
    "DMPSource": DMPSource,
    "IdentityProvider": IdentityProvider,
    "ContainerFormat": ContainerFormat,
}


class PluginRegistry:
    SERVES = ("P9", "P7")
    """Discovered plugins, indexed by protocol and by name."""

    def __init__(self) -> None:
        self._by_protocol: dict[str, dict[str, Any]] = {p: {} for p in PROTOCOLS}
        self._manifests: dict[str, CapabilityManifest] = {}
        self.refused: list[str] = []

    def register(self, instance: Any, *, permitted_residencies: set[Residency] | None = None) -> None:
        try:
            manifest = instance.manifest()
        except Exception as exc:
            raise PluginError(
                f"{type(instance).__name__} does not provide a usable manifest: {exc}. "
                "Every plugin must declare its capabilities (§6.3)."
            ) from exc

        proto_name = manifest.protocol
        proto = PROTOCOLS.get(proto_name)
        if proto is None:
            raise PluginError(
                f"plugin {manifest.name!r} declares unknown protocol {proto_name!r}; "
                f"known protocols are {sorted(PROTOCOLS)}"
            )
        if not isinstance(instance, proto):
            raise PluginError(
                f"plugin {manifest.name!r} claims protocol {proto_name!r} but does not "
                "satisfy it. Checked at registration rather than at first call, so the "
                "failure is a startup problem and not a mid-workflow one."
            )
        if permitted_residencies is not None and manifest.residency is not None:
            if manifest.residency not in permitted_residencies:
                raise PluginError(
                    f"plugin {manifest.name!r} declares residency "
                    f"{manifest.residency.value!r}, which local policy does not permit"
                )
        if manifest.name in self._by_protocol[proto_name]:
            raise PluginError(
                f"duplicate registration of {manifest.name!r} for protocol {proto_name!r}"
            )
        self._by_protocol[proto_name][manifest.name] = instance
        self._manifests[manifest.name] = manifest

    def discover(self, *, permitted_residencies: set[Residency] | None = None) -> None:
        """Load every advertised plugin.

        A plugin that fails validation is refused and named; the system starts
        without it rather than failing entirely, and the capability report
        records the absence.
        """
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                self.register(ep.load()(), permitted_residencies=permitted_residencies)
            except Exception as exc:
                self.refused.append(f"{ep.name}: {exc}")

    def get(self, protocol: str, name: str) -> Any:
        try:
            return self._by_protocol[protocol][name]
        except KeyError as exc:
            raise PluginError(
                f"no plugin named {name!r} is registered for protocol {protocol!r}; "
                f"available: {sorted(self._by_protocol.get(protocol, {}))}"
            ) from exc

    def all_for(self, protocol: str) -> dict[str, Any]:
        return dict(self._by_protocol.get(protocol, {}))

    def manifests(self) -> list[CapabilityManifest]:
        return [self._manifests[n] for n in sorted(self._manifests)]
