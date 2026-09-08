# datadirector-contracts

Interface contracts for the Data Director: type definitions and protocols, with
no implementation. Plugin authors depend on this package and nothing else.

Licensed under **Apache 2.0**, separately from the Data Director application
(EUPL 1.2), so that a plugin may be released under any licence. See ADR-025.

## What is here

| Module | Contents |
|---|---|
| `primitives` | Identifiers, digests, artefact references, residency |
| `sensitivity` | The classification lattice and its tightening asymmetry |
| `assertions` | Shared envelope for claims extracted from documents |
| `payloads` | Declaration claims, DMP commitments, instructions |
| `events` | Append-only log, hash chain, human-act enforcement |
| `decisions` | Structured decision records and field provenance |
| `provenance` | PROV-O activities, reason codes, visibility partitions |
| `policy` | Policy configuration and the Policy Enforcement Point contract |
| `plugins` | The eight extension protocols |

## Writing a plugin

Implement one protocol from `plugins`, declare a `CapabilityManifest`, and
advertise it through an entry point:

```toml
[project.entry-points."datadirector.plugins"]
my-repository = "my_package:MyRepositoryDriver"
```

Three constraints apply to every plugin, and they are load-bearing rather than
stylistic:

1. A plugin never holds model credentials and never calls a model backend
   directly. Model access is routed through the Policy Enforcement Point.
2. A plugin requests repository credentials by scope from the credential
   broker; it never receives a secret as a value.
3. A plugin declares its network and residency characteristics in its manifest.
   A deployment may refuse to load a plugin whose residency is incompatible
   with local policy.
