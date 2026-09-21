"""Configuration loading and startup validation.

Loads the two YAML surfaces, validates both, resolves plugin references against
what is actually installed, and produces a ResolvedConfig.

Nothing continues past an invalid configuration. Blueprint C7 requires clear
failure, and a configuration defect that surfaces three steps into a workflow is
the failure mode this module exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from datadirector_contracts import PolicyConfig, SensitivityClass

from ..credentials.broker import CredentialBroker
from ..errors import ConfigurationError
from ..plugins.discovery import PluginRegistry
from .models import InstalledPlugin, ResolvedConfig, WiringConfig


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigurationError(f"configuration file not found: {path}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path} is not valid YAML: {exc}") from exc


def load_wiring(path: Path | str) -> WiringConfig:
    try:
        return WiringConfig.model_validate(_read_yaml(Path(path)))
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(f"invalid wiring configuration in {path}: {exc}") from exc


def load_policy(path: Path | str) -> PolicyConfig:
    raw = _read_yaml(Path(path))
    mapping = raw.get("backend_by_sensitivity", {})
    try:
        raw["backend_by_sensitivity"] = {
            SensitivityClass[str(k).upper()]: v for k, v in mapping.items()
        }
    except KeyError as exc:
        raise ConfigurationError(
            f"unknown sensitivity class {exc} in {path}; expected one of "
            f"{[c.label for c in SensitivityClass]}"
        ) from exc
    try:
        return PolicyConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigurationError(f"invalid policy configuration in {path}: {exc}") from exc


def resolve(wiring: WiringConfig, policy: PolicyConfig, registry: PluginRegistry,
            *, application_version: str, broker: CredentialBroker | None = None) -> ResolvedConfig:
    """Cross-validate the two surfaces and materialise the startup state."""
    declared = {b.name for b in wiring.backends}

    for sensitivity, names in policy.backend_by_sensitivity.items():
        if not names:
            raise ConfigurationError(
                f"policy permits no backend for {sensitivity.label!r}. An empty list "
                "is silently unusable policy, which is worse than absent policy: "
                "state 'halt' explicitly or name a backend."
            )
        unknown = [n for n in names if n not in declared]
        if unknown:
            raise ConfigurationError(
                f"policy for {sensitivity.label!r} names backends the wiring does not "
                f"declare: {unknown}. Declared: {sorted(declared)}"
            )

    if broker is not None:
        missing = broker.check_present()
        if missing:
            raise ConfigurationError(
                f"credentials configured but not set in the environment for scopes: "
                f"{missing}. Copy .env.example to .env and fill it in."
            )

    installed = [
        InstalledPlugin(
            name=m.name, version=m.version, protocol=m.protocol,
            requirements_supported=m.requirements_supported,
            residency=m.residency, offline_capable=m.offline_capable,
        )
        for m in registry.manifests()
    ]
    return ResolvedConfig(
        profile=wiring.profile, wiring=wiring, policy=policy,
        installed_plugins=installed, application_version=application_version,
    )


ALL_REQUIREMENTS = [f"R{i}" for i in range(1, 13)]


def capability_report_text(resolved: ResolvedConfig, refused: list[str],
                           coverage: dict[str, list[str]] | None = None) -> str:
    """Human-readable startup report (§6.3).

    Names what the deployment cannot do as well as what it can, so a missing
    plugin is visible rather than expressed as a silently absent feature.
    """
    # Prefer what the runtime actually constructed. Entry-point discovery alone
    # reported NOT AVAILABLE for everything, because the built-in components are
    # not entry points — which told an operator their deployment could do
    # nothing when it could do almost everything.
    served = coverage if coverage is not None else resolved.capability_report()
    lines = [
        f"Data Director {resolved.application_version} "
        f"[profile: {resolved.profile.value}]",
        f"Configuration digest: {resolved.digest()}",
        "",
        "Requirement coverage from the components this deployment builds:",
    ]
    for req in ALL_REQUIREMENTS:
        who = served.get(req)
        lines.append(f"  {req:<4} {'served by ' + ', '.join(who) if who else 'NOT AVAILABLE'}")
    if refused:
        lines += ["", "Plugins refused at startup:"] + [f"  {r}" for r in refused]
    return "\n".join(lines)
