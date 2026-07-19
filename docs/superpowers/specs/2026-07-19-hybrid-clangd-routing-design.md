# Hybrid Clangd Routing Design

## Goal

Preserve Serena's JetBrains-backed semantic workflow for supported languages
while transparently routing file-scoped C and C++ semantic reads to Serena's
LSP backend, which uses clangd.

The integration belongs to the workspace orchestrator. Application
repositories do not gain adapter code or new runtime dependencies.

## Scope

The first version transparently routes these JetBrains-named read tools when
`relative_path` identifies a C or C++ file:

- `jet_brains_get_symbols_overview`
- `jet_brains_find_symbol`
- `jet_brains_find_referencing_symbols`
- `jet_brains_find_declaration`
- `jet_brains_find_implementations`
- `jet_brains_run_inspections`

The recognized native suffixes are `.c`, `.h`, `.i`, `.ii`, `.cc`, `.cpp`,
`.cxx`, `.c++`, `.cppm`, `.ccm`, `.cxxm`, `.c++m`, `.hpp`, `.hh`, `.hxx`,
`.h++`, `.inl`, `.ipp`, `.ixx`, `.tpp`, `.txx`, and `.ino`.

Pathless and directory-scoped searches remain JetBrains-backed. Symbolic
edits, rename, move, inline, safe delete, type hierarchy, and debugger tools
remain JetBrains-backed. Expanding those operations requires a separate design
because their mutation and output contracts differ.

## Architecture

The worktree broker keeps its current persistent JetBrains Serena service. For
projects whose repaired Serena configuration includes `cpp`, it also starts or
reuses a persistent Serena LSP service for the same canonical worktree. The
existing backend-sensitive service key keeps the two services distinct while
the current owner and lease rules keep both within one logical task boundary.

A new stdio MCP gateway replaces the single-target `mcp-proxy` process only for
hybrid connections. It connects to both loopback Streamable HTTP endpoints
using the MCP Python SDK already installed with the required `mcp-proxy`
runtime. It exposes the JetBrains tool catalog and forwards ordinary requests
unchanged. For the six allowlisted tools and a recognized native file, it maps
the call to the corresponding LSP tool and returns that result under the
original request ID.

Repositories without detected C/C++ files retain the current single-service,
single-proxy path. They incur no second service or gateway process.

## Routing Contract

Routing is deterministic and based only on the normalized, project-relative
`relative_path` argument. Absolute paths, traversal components, external
symbol identifiers, empty paths, and directories never activate the native
fallback.

The mappings are:

| Exposed JetBrains tool | LSP destination |
| --- | --- |
| `jet_brains_get_symbols_overview` | `get_symbols_overview` |
| `jet_brains_find_symbol` | `find_symbol` |
| `jet_brains_find_referencing_symbols` | `find_referencing_symbols` |
| `jet_brains_find_declaration` | `find_declaration` |
| `jet_brains_find_implementations` | `find_implementations` |
| `jet_brains_run_inspections` | `get_diagnostics_for_file` |

Compatible arguments retain their names. JetBrains-only arguments are dropped
only where their default meaning is representable. A non-default unsupported
argument produces a clear error rather than silently weakening the request.

For inspection routing, JetBrains one-based optional line ranges become LSP
zero-based ranges. Severity names map to LSP severities: `ERROR` to `1`,
`WARNING` to `2`, `WEAK_WARNING` and `INFO` to `3`. A requested
`inspection_names` filter is rejected because clangd diagnostics do not expose
JetBrains inspection IDs.

The gateway does not merge or reinterpret successful tool content. The
selected Serena backend remains responsible for result formatting and answer
length limits.

## Activation and Failure Behavior

Hybrid startup is automatic when all of these are true:

1. The project doctor reports `cpp` among detected or configured languages.
2. `clangd` resolves to an executable.
3. The operator kill switch does not disable hybrid routing.

The kill switch is `SERENA_HYBRID_CPP_ENABLED`; values `0`, `false`, `no`, or
`off` disable the secondary backend.

JetBrains startup remains authoritative. If the LSP service or hybrid gateway
cannot start, the connection stays available through the normal JetBrains
proxy. Native calls in a healthy hybrid session never fall back to an empty
JetBrains response: an unavailable secondary backend returns an explicit
`clangd-unavailable` error.

A missing `compile_commands.json` does not block clangd startup because clangd
can use fallback flags, but health output reports reduced confidence. The
adapter never runs build, package-manager, or arbitrary repository commands to
generate a compile database.

## Ownership, Cleanup, and Privacy

The secondary Serena service uses the broker's existing process identity,
canonical-root ownership, backend-specific key, lease, idle cleanup, and
bounded termination rules. The broker adds and removes both leases in one
connection lifecycle and tolerates a missing secondary lease during degraded
startup.

Bridge journals store only existing allowlisted metadata plus stable hybrid
stage, outcome, and reason codes. They never store source text, symbol names,
tool arguments, raw MCP content, compiler diagnostics, or absolute project
paths.

The feature does not edit installed plugins, signal IDE processes, or alter
IntelliJ settings.

## Testing

Testing follows red-green-refactor cycles and covers:

- Native suffix and safe relative-path classification.
- Each tool-name and argument mapping.
- Diagnostics range and severity conversion.
- Rejection of unsupported non-default arguments.
- MCP fixture servers proving native calls reach LSP and TypeScript calls reach
  JetBrains while client request IDs and errors are preserved.
- Project activation, kill-switch behavior, and absence of hybrid startup for
  non-native repositories.
- Secondary service start, reuse, dual leases, cleanup, and degraded startup.
- Journal allowlists and the absence of source/tool payloads.
- Existing broker and bridge regression suites.
- A Gate C acceptance probe demonstrating nonzero C symbols through the hybrid
  route while TypeScript still uses JetBrains semantics.

## Rollout

The repository installer deploys the gateway beside the existing broker tools.
README and global-agent guidance describe transparent native reads, the
file-scoped limitation, compile-database expectations, and the kill switch.
The first release does not route semantic edits or global mixed-language
searches.
