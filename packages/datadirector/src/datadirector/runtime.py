"""The composition root: where the parts become a system.

This module exists because of an audit finding. Every agent, every plugin and
most of the infrastructure had been written, tested and documented — and none of
it was reachable from any entry point. `ingest` ran the ingestion agent and
stopped; classification, metadata, validation, the DMP, the repository and the
gate were reachable only from tests.

The failure is worth naming because it is not obvious from inside. Each cluster
ended with its components complete and its tests passing, and nothing in that
process asks whether the components have been *assembled*. A conformance row can
name a component that exists and is unreachable, and be wrong in a way no test
catches, which is a subtler version of the R8 failure in Appendix B.5.

So: one place where configuration becomes running parts, and one place to look
when asking what this deployment can actually do.
"""

from __future__ import annotations

from pathlib import Path

from datadirector_contracts import ModelCapability, SensitivityClass

from .agents import AgentContext, AgentRegistry, build_registry
from .agents.classification import ClassificationAgent
from .backends.base import capabilities_from_config
from .backends.ollama import OllamaBackend
from .config.models import ResolvedConfig
from .credentials.broker import CredentialBroker
from .dmp.sources import DocumentDmpSource, MaDmpSource
from .exposure.ledger import ExposureLedger
from .policy.pep import PolicyEnforcementPoint
from .probing.executor import ProbeExecutor
from .provenance.recorder import Recorder
from .provenance.restricted import RestrictedStore
from .watch.folder import WatchedFolder
from .registries.re3data import Re3dataRegistry
from .repositories.zenodo import DepositionRegistry, ZenodoDriver
from .schemas.datacite import ZenodoDataCiteProfile
from .schemas.rocrate import RoCrateProfile
from .state.store import EventStore
from .validators.jsonschema_driver import DATACITE_MINIMAL, JsonSchemaValidator
from .vocabularies.ols import OlsVocabularyProvider


class Runtime:
    """Everything a deployment has, assembled once.

    Components that need a network are constructed but not contacted. A runtime
    that reached out on construction would make `check` a live test of the
    internet rather than of the configuration.
    """

    def __init__(self, resolved: ResolvedConfig, *, state_root: Path | str,
                 working_root: Path | str, environ: dict | None = None) -> None:
        self.config = resolved
        self.working_root = Path(working_root)
        self.store = EventStore(state_root)
        # The restricted store holds justifications withheld from the open
        # record (§7.5). Constructed here so the partition exists rather than
        # being an idea in the architecture document.
        self.restricted = RestrictedStore(
            resolved.wiring.storage.restricted_root)
        self.recorder = Recorder(self.store, Path(state_root).parent / "prov")
        self.watched = WatchedFolder(resolved.wiring.storage.watched_folder)
        self.ledger = ExposureLedger(Path(state_root).parent / "exposure")

        # Credentials are named by scope in the wiring, never held in it.
        self.broker = CredentialBroker(dict(resolved.wiring.credential_env_vars),
                                       environ=environ)

        self.backends = self._build_backends()
        self.pep = PolicyEnforcementPoint(resolved.policy, self.backends)

        # Plugins. Each is optional in the sense that a deployment may not have
        # configured it; none is optional in the sense of being skipped silently
        # — `capability_report` states what is absent.
        # Profiles this deployment can emit, by the name a person would use.
        # The profile was a fixed attribute, so nothing could choose one.
        self.profiles = {"DataCite": ZenodoDataCiteProfile(),
                         "RO-Crate": RoCrateProfile()}
        self.schema_profile = self.profiles["DataCite"]
        self.crate_profile = self.profiles["RO-Crate"]
        self.validator = JsonSchemaValidator({"DataCite": DATACITE_MINIMAL})
        # Network-dependent plugins are constructed where the deployment has a
        # plugin entry enabling them. Constructed, not contacted: a runtime that
        # reached out here would make configuration checking a live test of the
        # internet.
        enabled = {p.name for p in resolved.wiring.plugins if p.enabled}
        self.vocabulary = OlsVocabularyProvider()
        self.registry = Re3dataRegistry()
        self.repository = (self._build_repository(state_root, resolved)
                           if "zenodo" in enabled else None)
        self.repository_name = "zenodo" if "zenodo" in enabled else None

          # One registry, keyed by the name each agent gave itself. Nothing in
          # this deployment holds a list of agents, and nothing outside the
          # registry can reach one: there is no dictionary left to index.
        self.agent_registry = self._build_agents()

    def _build_agents(self) -> AgentRegistry:
        """Construct this deployment's agents from what it provides.

        Rebuildable on purpose. A caller that changes the backends or the policy
        after construction - a test, or a redeploy - rebuilds the agents so no
        agent keeps a reference to the deployment that was. The registry remains
        the only thing that holds them.
        """
        return build_registry(self._agent_context())

    @property
    def agents(self) -> AgentRegistry:
        """The agents, by the name each gave itself.

        A view of the registry rather than a second collection of them: the
        pipeline asks for an agent by name and the registry answers, so there is
        no dictionary left for a stale agent to hide in. Assigning replaces the
        registry rather than copying into it, which is what lets a caller rebuild
        the agents after changing the deployment.
        """
        return self.agent_registry

    @agents.setter
    def agents(self, registry: AgentRegistry) -> None:
        self.agent_registry = registry

    # -- construction ------------------------------------------------------

    def _build_backends(self) -> dict:
        """Construct each declared backend.

        A backend that cannot be constructed is treated as absent and named in
        `unavailable()`, rather than raising here: a deployment with one
        misconfigured remote backend should still be able to run its local work
        and be told what it is missing.
        """
        backends = {}
        self.unbuildable: list[str] = []
        for declaration in self.config.wiring.backends:
            capabilities = capabilities_from_config(declaration.capabilities)
            try:
                if declaration.kind == "ollama":
                    backends[declaration.name] = OllamaBackend(
                        name=declaration.name,
                        endpoint=declaration.endpoint or "",
                        model=declaration.model or declaration.name,
                        timeout=declaration.timeout_seconds,
                        capabilities=capabilities)
                elif declaration.kind == "anthropic":
                    from .backends.anthropic import AnthropicBackend
                    backends[declaration.name] = AnthropicBackend(
                        name=declaration.name,
                        model=declaration.model or "claude-sonnet-4-5",
                        broker=self.broker, capabilities=capabilities)
                else:
                    # An unrecognised kind fell through silently until an
                    # assembly test asked for it: the backend simply was not
                    # there, and nothing said so. A configuration error that
                    # produces a quietly smaller deployment is worse than one
                    # that complains.
                    self.unbuildable.append(
                        f"{declaration.name}: {declaration.kind!r} is not a "
                        "backend kind this build knows")
            except Exception as exc:
                self.unbuildable.append(
                    f"{declaration.name} ({declaration.kind}): "
                    f"{type(exc).__name__}: {exc}")
        return backends

    def _build_repository(self, state_root, resolved):
        try:
            settings = next((p.settings for p in resolved.wiring.plugins
                             if p.name == "zenodo"), {}) or {}
            return ZenodoDriver(
                self.broker,
                DepositionRegistry(Path(state_root).parent / "depositions.json"),
                base_url=settings.get("base_url",
                                      "https://sandbox.zenodo.org"))
        except Exception:
            return None

    def _agent_context(self) -> AgentContext:
        """What this deployment provides, as the agents' classes ask
        for it.

        Each agent class says how to build itself from what the
        deployment provides; this assembles that and hands it to the
        registry. Nothing here names an agent, and nothing here keeps
        one afterwards: the registry answers for a name, and it answers
        only for itself.
        """
        return AgentContext(
            pep=self.pep, working_root=self.working_root, store=self.store,
            recorder=self.recorder, ledger=self.ledger,
            vocabulary=self.vocabulary, re3data=self.registry,
            repository_driver=self.repository,
            repository_name=self.repository_name,
            schema_profile=self.schema_profile, profiles=self.profiles,
            validator=self.validator, profile_for=self.profile_for,
            default_plan_source=MaDmpSource(),
            dmp_source_factory=self.plan_source,
            probe_executor_factory=self.probe_executor,
            infer_sensitivity=self.config.policy
                              .infer_sensitivity_from_declaration)

    def classifier(self, job_id: str, profile) -> ClassificationAgent:
        """The classification agent, which is per-job rather than per-deployment.

        It holds a probe executor, and an executor is bound to one job's
        exposure budget and one job's material. Sharing one across jobs would
        let a probe name an artefact from another submission, which the executor
        exists to prevent.
        """
        return ClassificationAgent(self.pep,
                                   self.probe_executor(job_id, profile))

    # -- what this deployment can do --------------------------------------

    def content_inspector(self, job_id: str, profile):
        """Chunked inspection for documents that cannot be profiled structurally.

        Constructed per job for the same reason the classifier is: it holds a
        probe executor bound to one job's exposure budget.
        """
        from .content.inspector import ContentInspector
        return ContentInspector(self.pep, self.probe_executor(job_id, profile))

    def profile_for(self, standard: str | None):
        """The profile for a named standard, or the default.

        Unknown names resolve to the default *here* only because the caller has
        already put an unrecognised name to a person: this is the last step, not
        the place the decision is made.
        """
        return self.profiles.get(standard or "", self.schema_profile)

    def unpacked_root(self, job_id: str) -> Path:
        """Where this job's material actually is.

        One place that knows the layout. Ingestion writes to
        `<working>/<job>/unpacked`, and the probe executor was handed the bare
        working root, so every path it resolved was missing the job directory —
        a classification that failed with "no such file" on a file that was
        plainly there.
        """
        return self.working_root / job_id / "unpacked"

    def probe_executor(self, job_id: str, profile) -> ProbeExecutor:
        return ProbeExecutor(job_id, profile, self.unpacked_root(job_id),
                             self.ledger)

    def dmp_source(self, reference: str | None):
        """Pick a source by what the reference is, not by configuration.

        A `.json` plan is read from its structured fields; anything else is read
        by a model and marked provisional. The distinction is the whole reason
        the maDMP standard exists.
        """
        if not reference:
            return None
        return MaDmpSource() if reference.lower().endswith(".json") \
            else DocumentDmpSource()


    def plan_source(self, job_id: str, reference):
        """The plan source for one job, from the reference the agent
        named. The agent answers "where is the plan"; the deployment builds
        the thing that resolves it, and the agent never names a class.
        """
        return self.dmp_source(str(reference)) if reference is not None \
            else None

    def coverage(self) -> dict[str, list[str]]:
        """Which requirements this deployment's *constructed* components serve.

        Not what the source tree contains, and not what entry points advertise:
        what this runtime actually built, with this configuration. A deployment
        with no repository plugin enabled does not serve R7, however much code
        is installed.

        `check` previously reported from entry-point discovery alone, so every
        requirement read NOT AVAILABLE while `conformance` — which scans the
        source — reported full coverage. Two commands in one tool, contradicting
        each other about the same deployment. This is the answer both should
        have been giving.
        """
        served: dict[str, list[str]] = {}

        def record(component, name: str) -> None:
            declared = getattr(component, "serves", None) \
                or getattr(component, "SERVES", None)
            for requirement in declared or ():
                served.setdefault(requirement, []).append(name)

        for agent in self.agent_registry.all():
            record(agent, type(agent).__name__)
          # Classification is per-job, so the registry holds a builder
          # rather than a running one; it is still a component this
          # deployment can run.
        record(ClassificationAgent, ClassificationAgent.__name__)

        for component in (self.schema_profile, self.crate_profile,
                          self.validator, self.vocabulary, self.registry,
                          self.repository, self.store, self.recorder,
                          self.restricted, self.pep, self.broker, self.ledger,
                          self.watched):
            if component is not None:
                record(component, type(component).__name__)

        for backend in self.backends.values():
            record(backend, type(backend).__name__)

        # Requirements whose only claimant is unreachable in the workflow are
        # removed, because a component nothing leads to serves nothing. This is
        # the deduction that replaces a declaration: the class still states what
        # it intends — no analysis of Python establishes that a class satisfies a
        # sentence of English — but whether the claim is connected to anything
        # is a fact about the graph.
        try:
            from .workflow.definition import build as build_graph

            unreachable = build_graph({}).claimed_but_unreachable()
            for requirement in unreachable:
                served.pop(requirement, None)
        except Exception:
            pass

        # Third-party plugins discovered through entry points, which is what the
        # report used to consider and all it considered.
        for manifest in getattr(self.config, "plugin_manifests", ()) or ():
            for requirement in getattr(manifest, "requirements_supported", ()):
                served.setdefault(requirement, []).append(manifest.name)

        return {k: sorted(set(v)) for k, v in served.items()}

    def unavailable(self) -> list[str]:
        """What this deployment is missing, said plainly.

        The same discipline as the inspection tier: a capability that is absent
        must be reported, because a workflow that silently skips a step looks
        identical to one that ran it and found nothing.
        """
        missing = list(getattr(self, "unbuildable", []))
        if not self.backends:
            missing.append("no model backend is configured; every agent that "
                           "needs one will halt")
        if self.repository is None:
            missing.append("no repository driver: nothing can be deposited (R7)")
        if not any(ModelCapability.VISION in b.capabilities()
                   for b in self.backends.values()):
            missing.append("no vision-capable backend: images will be recorded "
                           "as not inspected and presumed sensitive")
        for level in SensitivityClass:
            if not self.pep.can(level, ModelCapability.TEXT_GENERATION):
                missing.append(f"no backend permitted for {level.label} "
                               "material; such jobs will halt")
        return missing
