# Workspace Harbor

An IntelliJ lifecycle and worktree orchestrator for Serena-powered coding
agents.

Workspace Harbor keeps IntelliJ IDEA project windows, Serena services, and
Codex tasks from colliding. It opens and trusts exact Git worktree roots,
reuses one IntelliJ application process, assigns logical ownership per task,
and closes only broker-owned idle projects that pass every safety check.

IntelliJ IDEA Ultimate is the only supported IDE. PyCharm remains installed
but is not opened, trusted, inventoried, or closed by Workspace Harbor.

## Local requirements

- IntelliJ IDEA at `$HOME/Applications/IntelliJ IDEA.app`
- Workspace Harbor and Serena IntelliJ plugins
- Official JetBrains language plugins required by the repository
- Git and Python 3.9 or newer

The build and verification commands use IntelliJ's bundled JBR; a separate
system JDK is not required:

```sh
export INTELLIJ_APP_PATH="$HOME/Applications/IntelliJ IDEA.app"
export JAVA_HOME="$INTELLIJ_APP_PATH/Contents/jbr/Contents/Home"
./gradlew test buildPlugin verifyPlugin --console=plain
python3 -m unittest discover -s tests/python -p 'test_*.py' -v
```

## Commands

- `open-codex-project-in-intellij ROOT` validates, trusts, opens, and registers
  one exact Git root.
- `intellij-project-trust allow|status ROOT` and `audit` manage only the active
  IntelliJ trusted-path registry. The helper preserves both the current
  `TRUSTED_PROJECT_PATHS` schema and the legacy `TRUSTED_PATHS` schema, and
  reports bounded failure details instead of returning an unexplained status.
- `intellij-project-reaper status --json` reports lifecycle state; `inspect ROOT`
  returns the exact project's indexing, modal, activity, and safety snapshot.
  `recycle ROOT` closes only that registered project using the task-owned window
  policy: it may stop indexing and task-local run, terminal, or debugger
  activity, while still protecting unsaved documents, modal ambiguity, closing
  races, and unknown data-safety state. `restart-hung` stops only the exact
  validated IntelliJ host after three failed authenticated event-thread probes.
  `cleanup` retains its normal fully-eligible policy, and `unregister ROOT`
  removes a record only after a worktree has been safely closed and removed.
- `serena-codex jetbrains-service-status ROOT` requires one IntelliJ-owned
  Serena service and rejects foreign or duplicate matches. Its service,
  semantic-probe, and health-check commands first distinguish denied local
  loopback access from a genuinely missing plugin service; sandbox denial is
  reported as unavailable and never as evidence that IntelliJ must be reopened.
- `serena-project-doctor ROOT` performs one read-only bounded health and
  language-coverage check. `--recover` opens a missing exact project, waits
  through indexing, retries semantics, then may recycle its managed owned window
  or restart a positively validated hung IDE. `--bootstrap` performs explicit
  dependency preparation after reporting any one-time decisions. `--history`
  aggregates semantic outcomes plus recovery results and actions without
  storing prompts, source, or raw tool output.
- `workspace-harbor-bootstrap status|run ROOT` plans and caches deterministic
  repository setup; `decide` records local language, tracking, or custom-command
  consent.
- `serena-worktree-broker status|cleanup` reports ownership and reclaims only
  broker-owned idle services.
- `serena-bridge-doctor status|recover` diagnoses the boundary between a
  healthy IntelliJ/Serena backend, a working MCP handshake, and Serena tools
  actually exposed to the current Codex task. Its guarded restart commands are
  disabled by default and require a sole-active-task attestation plus a
  one-shot task heartbeat.

## Hybrid C/C++ semantics

For a repository whose Serena project doctor reports the `cpp` language and
whose `PATH` contains an executable `clangd`, the broker keeps JetBrains as the
primary Serena backend and starts one additional broker-owned LSP service.
The exposed tool catalog does not change. Calls to these JetBrains-named tools
are transparently served by clangd when `relative_path` names an existing C or
C++ source or header inside the repository:

- `jet_brains_get_symbols_overview`
- `jet_brains_find_symbol`
- `jet_brains_find_referencing_symbols`
- `jet_brains_find_declaration`
- `jet_brains_find_implementations`
- `jet_brains_run_inspections`

Java, JavaScript, TypeScript, TSX, and Python continue through JetBrains.
Pathless and directory-scoped searches also remain JetBrains-backed, as do
semantic edits, refactors, type hierarchy, and debugger calls. Native routing
is deliberately limited to safe file-scoped reads whose result contract can
be preserved.

A repository-owned `compile_commands.json` gives clangd the most accurate
include paths, defines, and compiler flags. Its absence does not block startup,
but results may have lower confidence because clangd uses fallback flags.
Workspace Harbor never runs a build or package-manager command to generate the
compile database.

Set `SERENA_HYBRID_CPP_ENABLED=0` (also `false`, `no`, or `off`) on the broker
environment to disable the secondary backend. If secondary startup fails after
hybrid activation, the JetBrains connection remains available and eligible
native calls return the explicit `clangd-unavailable` error; they do not fall
through to an empty JetBrains C/C++ result. If the hybrid gateway itself is
missing or its runtime preflight fails, the broker records
`hybrid-gateway-missing` or `hybrid-gateway-unavailable` and uses the normal
single-backend proxy.

Verify activation without inspecting source or tool arguments:

```sh
serena-worktree-broker status --json
```

An active hybrid project appears as two records for the same root, one with
`backend` set to `JetBrains` and one set to `LSP`. The LSP record reports
`native_semantic_confidence` as `compile-database` when a root-level
`compile_commands.json` exists and `fallback-flags` otherwise.

## Codex task bridge recovery

Serena bridge health has three independent boundaries:

1. IntelliJ and the exact-root Serena service are semantically healthy.
2. A bounded synthetic MCP client can complete `initialize` and `tools/list`
   through the configured broker.
3. The current Codex task actually exposes Serena MCP tools.

Do not infer the third boundary from the first two. Report the current task's
tool inventory as `present`, `missing`, or `unknown`, then run:

```sh
serena-bridge-doctor status "$(git rev-parse --show-toplevel)" \
  --reported-tools unknown --probe
serena-bridge-doctor recover "$(git rev-parse --show-toplevel)" \
  --reported-tools missing
serena-bridge-doctor resume "$(git rev-parse --show-toplevel)" \
  --incident INCIDENT --thread-id THREAD --reported-tools present
```

Recovery performs one bounded lower-layer repair cycle. It removes only stale
broker-owned state for the exact root, or delegates an unhealthy semantic
backend to `serena-project-doctor --recover`; it never loops against unchanged
state. A bridge-only failure never authorizes an IntelliJ restart.

The bridge journal stores only digests, stable stage/outcome/reason codes, and
timings. It never stores prompts, source, MCP arguments, environment values,
response bodies, or stderr. Restart incidents, attestations, checkpoints, and
policy are private `0600` files beneath
`$CODEX_HOME/state/serena-bridge/`.

Codex restart is a last bridge-only recovery step. It is disabled by default
and is accepted only when the app-derived task inventory proves the current
task is the sole active task, no child or host state is ambiguous, and an
enabled one-shot heartbeat targets the same task and incident 30–180 seconds
ahead. The detached relauncher revalidates the exact ChatGPT app PID, process
start time, executable, and bundle ID; it requests a graceful app quit and
never sends `TERM`, `KILL`, `pkill`, or `killall`. It then opens the exact
`codex://threads/THREAD` deep link. The heartbeat resumes the incident once and
must be disabled or deleted after `healthy` or `fresh-task-required`. A failed
attempt cannot schedule a second restart.

## Trust model

Managed roots must be exact canonical Git top levels beneath
`$HOME/Documents/Codex`, `$HOME/Developer/Codex`, or `$HOME/.codex/src`. The
trust helper rejects nested paths, non-Git directories, symlink escapes,
malformed state, and roots outside those parents. It writes IntelliJ's native
registry with locking, backup, and atomic replacement. When IntelliJ is
running, it also calls Workspace Harbor's authenticated loopback endpoint so
the in-memory trust state changes before the project is opened; this prevents
a stale live registry from producing a GUI trust prompt.

When Codex resumes a task whose stored startup directory resolves to a Serena
project outside those managed roots, the broker starts a projectless MCP
surface instead of exiting. This preserves `initial_instructions` and
`activate_project`, allowing the task to select its actual managed worktree.
The fallback does not open or trust the stale project, bypass worktree
ownership, or weaken validation for an explicitly requested project.

`intellij-project-trust audit` reports broad and out-of-scope entries but never
removes them automatically.

## Ownership model

The canonical worktree is the ownership boundary. When Codex supplies
`CODEX_THREAD_ID`, Workspace Harbor reads the bounded `session_meta` record and
resolves validated subagent ancestry to the root parent task automatically. No
capsule, prompt token, or manual environment export is required for ordinary
Codex subagents.

The desktop MCP launcher currently supplies only its long-lived Codex host
identity. Workspace Harbor validates the exact Codex executable, process ID,
and process start time so broker reloads under that host reuse the same Serena
service. Unrelated concurrent desktop tasks must use separate canonical
worktrees because host fallback cannot distinguish their task identities.
`WORKSPACE_HARBOR_OWNER_ID` remains available as an explicit override for
non-Codex callers and custom integrations.

Different worktrees or repositories may run concurrently in separate IntelliJ
project windows. When task identity is available, a second logical owner is
rejected for an already-owned worktree. Missing, malformed, ambiguous, cyclic,
or inconsistent lineage fails closed to the child's own thread ID instead of
granting reuse. A live legacy process-scoped lease is migrated only when its
PID, start time, exact broker command, and validated parent Codex host all
match; mixed or cross-host ownership remains rejected without partial changes.

The broker exposes owners in `serena-worktree-broker status`. IntelliJ itself
remains one application process; project-window and worktree ownership is the
unit Workspace Harbor manages.

Use `serena-worktree-broker owner --json` to inspect the current thread, its
resolved owner, the resolution source, and any concise fail-closed reason. The
command never reads beyond the first bounded session metadata record and never
prints prompts or session content.

## Queued IntelliJ opener

The canonical worktree is the single-flight boundary. Requests for one root
join one open operation. Different roots enter a durable FIFO launch queue for
the brief exact-root trust and IntelliJ open request, then wait for indexing
and Serena readiness concurrently. Queueing is normal recovery state; only a
validated deadline or unsafe owner/state condition is a failure.

The default allowances are:

```text
INTELLIJ_OPENER_OPERATION_TIMEOUT=300
INTELLIJ_OPENER_QUEUE_TIMEOUT=300
INTELLIJ_OPENER_LAUNCH_COMMAND_TIMEOUT=30
INTELLIJ_SERENA_READY_TIMEOUT=120
```

The operation, queue, and readiness allowances form one overall opener
deadline. The launch-command timeout separately bounds trust and the IntelliJ
open subprocess within the remaining overall budget. Normal diagnostics say
`joined existing worktree open` or `queued launch completed`. Terminal
classifications distinguish `operation-deadline`, `queue-deadline`, and
`readiness-deadline`; doctor history stores only the phase and bounded timing
metrics.

Queue presence is not a reaper lease. A queued root is not registered as
managed until its exact-root Serena service and native IntelliJ model are
ready. The queue never closes a project or starts another IntelliJ application
process.

## Persistent dependency bootstrap

The IntelliJ opener runs one idempotent preparation check before its
already-open shortcut. An unchanged worktree is a fast cache hit: no package
manager runs when an agent stops, restarts, or reconnects. Setup runs again
only when a selected command, manifest, lockfile, runtime/tool identity,
required environment marker, or Harbor recipe version changes, or when
`--force` is requested.

A cache hit is fully read-only: it does not acquire the mutable execution lock
or require write access to Harbor's private state directory. Pending or forced
setup still requires that lock and fails closed if state cannot be mutated.
Operational permission or I/O failures are reported as degraded `failed`
results, distinct from invalid repository configuration. Dependency
degradation never authorizes a fallback installer and does not prevent the
separately guarded IntelliJ opener from making Serena semantics available for
diagnosis.

The opener also applies an outer 1,860-second guard to the bootstrap helper,
slightly longer than the bootstrap runner's own 1,800-second command limit.
This covers failures before command execution, including a wedged state lock
or helper process. On timeout it terminates only that helper's new process
group, reports a bounded diagnostic, and continues opening IntelliJ. Tests and
operators can shorten the outer guard with
`WORKSPACE_HARBOR_BOOTSTRAP_OPENER_TIMEOUT_SECONDS`. The broker and doctor
derive and enforce a minimum opener timeout from that guard, its ten-second
termination grace, the worktree-operation allowance, FIFO queue allowance,
IntelliJ readiness allowance, and a ten-second caller grace. A shorter parent
override is clamped to that safe minimum, so a parent recovery request cannot
expire before its bounded child phases.
Bootstrap output is drained continuously into a rolling 64 KiB tail rather
than an unbounded memory or temporary-disk buffer.

Plan precedence is conservative:

1. A conventional `bootstrap` task in `.codex/tasks.toml`, or a task selected
   by `.serena/codex-integration.yml`.
2. An argv-form custom command that has matching local approval.
3. A Workspace Harbor recipe backed by an unambiguous manifest and lockfile.

Harbor never scrapes prose into commands and never runs every installer found
under a repository. Conflicting managers, source without a matching dependency
boundary, and custom-command changes return `needs-decision`. Local decisions
are shared by sibling Git worktrees; successful installation records remain
per worktree.

Built-in recipes currently cover npm, pnpm, Yarn Classic/Berry, Bun, uv,
Poetry, Cargo fetch, and Go module download. Gradle and Maven remain
IntelliJ-native model imports unless the repository configures a setup task.
Other ecosystems are reported and require an explicit task or command.

Examples:

```sh
workspace-harbor-bootstrap status "$(git rev-parse --show-toplevel)" --json
serena-project-doctor --bootstrap "$(git rev-parse --show-toplevel)"
workspace-harbor-bootstrap decide "$(git rev-parse --show-toplevel)" \
  language rust enable
workspace-harbor-bootstrap decide "$(git rev-parse --show-toplevel)" \
  tracking local
```

Tracked integration configuration describes desired policy but does not grant
permission to execute arbitrary custom commands. Exact custom-command approval
is stored privately under `$CODEX_HOME/state/workspace-harbor/bootstrap` and is
invalidated when its argv, working directory, declared inputs, or markers
change. Installer output is bounded and redacted; raw output is not retained by
the bootstrap helper.

## Safety and recovery

Normal cleanup is fail-closed. Unsaved documents, indexing, active
run/debug/terminal sessions, modal dialogs, unknown plugin state, a broker
lease, or ambiguous ownership all protect normal cleanup. Explicit doctor
recovery may recycle only its registered exact-root window. Because that window
belongs exclusively to the task, recovery may interrupt indexing and task-local
runs, terminals, or debugger sessions; unsaved documents, modal ambiguity,
closing transitions, and unknown data-safety state still fail closed. A
whole-IDE restart is reserved for an alive exact IntelliJ
process whose authenticated event-thread probe fails three times; PID, start
time, and executable are revalidated immediately before signaling, TERM is
tried before KILL, and a semantic timeout alone is never sufficient. Do not
kill IntelliJ or Serena processes broadly. Use:

```sh
serena-project-doctor --recover "$(git rev-parse --show-toplevel)"
workspace-harbor-bootstrap status "$(git rev-parse --show-toplevel)" --json
serena-worktree-broker status
serena-worktree-broker cleanup
```

If a newly spawned Serena service cannot be authenticated during startup, the
broker now terminates that exact child process group before returning an error;
it never leaves an unrecorded service for later broad cleanup.

Deployment backups live under `~/.codex/backups/workspace-harbor/`.
