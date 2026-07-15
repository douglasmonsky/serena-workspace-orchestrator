# Subagent Serena Ownership Inheritance Design

## Goal

Let a Codex subagent reuse its root parent task's brokered Serena service when
both operate on the same canonical Git worktree. Preserve fail-closed logical
task ownership wherever Codex supplies task identity. Where the desktop MCP
transport supplies only host identity, make reloads stable, preserve separate
services for different worktrees, and require unrelated concurrent tasks to
use separate worktrees.

## Runtime findings

The broker originally chose the first available identity from
`WORKSPACE_HARBOR_OWNER_ID`, `CODEX_THREAD_ID`, or a process-local fallback.
Codex assigns every subagent a new `CODEX_THREAD_ID` and does not export
`CODEX_PARENT_THREAD_ID` or `WORKSPACE_HARBOR_OWNER_ID` into the child. The
session-lineage implementation below resolves that case when a broker command
runs inside a task shell.

Codex does record the relationship in the first `session_meta` event of the
child's local session JSONL. That record contains the child ID, a top-level
`parent_thread_id`, and the same parent ID under
`source.subagent.thread_spawn.parent_thread_id`.

The production MCP path has a separate constraint. The Codex desktop app
launches configured stdio MCP servers from its long-lived app-server process,
not from an individual task shell. That process does not receive
`CODEX_THREAD_ID`. The broker therefore used its own short-lived PID as the
fallback owner. When Codex reloaded an MCP server, the replacement broker had a
new PID, was rejected as an unrelated owner, and Codex omitted Serena's tools
from subsequent task tool inventories even though the persistent Serena HTTP
service remained healthy.

A bounded reproduction proved the boundary: an ordinary fresh Codex process
could not expose Serena while its replacement broker was rejected, while the
same process exposed Serena and successfully called `initial_instructions`
when the existing owner identity was passed explicitly.

## Session-lineage resolution

Add a read-only session-lineage resolver to `serena-worktree-broker`.
`WORKSPACE_HARBOR_OWNER_ID` remains the highest-priority explicit override. If
it is absent and `CODEX_THREAD_ID` is set, the resolver finds that thread's
session file, reads only its first JSONL record, and validates the
`session_meta` payload. For a valid subagent record, it follows parent links
until it reaches a root task and returns the root task's thread ID as the
logical Harbor owner.

The resolver will:

- require UUID-shaped thread IDs;
- require exactly one matching session file for each followed thread;
- require `payload.id` to match the requested thread;
- require the top-level and nested parent IDs to agree for subagents;
- reject self-links, cycles, and ancestry deeper than eight generations;
- read only the first bounded line of each candidate session file;
- return the current child thread ID when lineage is missing, malformed,
  ambiguous, or inconsistent; and
- continue to the host-resolution path when no Codex thread ID exists.

Failing back to the child ID is fail-closed: reuse is denied, but unrelated
tasks are never merged into one owner.

## Stable Codex-host fallback

When neither an explicit owner nor a Codex thread ID is available, the broker
will inspect its immediate parent process. If that parent is a validated Codex
host process, the broker will derive a stable owner from the parent's PID and
process start identity. Replacement broker subprocesses launched by the same
live Codex host will therefore resolve to the same owner. A different Codex
host process, including a restarted app server, resolves to a different owner.

The complete owner precedence is:

1. a valid explicit `WORKSPACE_HARBOR_OWNER_ID`;
2. a valid root task ID resolved from `CODEX_THREAD_ID` and session lineage;
3. a validated Codex-host process identity; and
4. the existing broker-process fallback when the parent cannot be identified
   safely as Codex.

Host validation must use live process identity, including start time, rather
than PID alone. It must recognize the supported Codex app-server and CLI host
shapes without accepting an arbitrary parent process whose command merely
contains the word `codex`. Status output may expose the owner category and an
opaque identifier, but not the full parent command.

### Legacy lease migration

The first corrected broker may encounter a live lease created by the previous
`manual-pid-<broker-pid>` fallback. It may migrate that lease atomically only
when all of the following are true:

- the legacy PID still identifies the recorded live broker process;
- that process is running the deployed broker entry point;
- its validated Codex parent has the same host identity as the replacement
  broker's validated parent; and
- no lease on the service belongs to another owner.

The migration rewrites only the matching lease owner to the stable host owner.
Unknown process state, a reused PID, a different parent, mixed owners, or an
unrecognized command remains a hard ownership conflict. No state file is
edited outside the broker's existing lock and validation path.

## Worktree and service isolation

Owner inheritance does not change the canonical worktree boundary. The broker
continues to key services by canonical worktree, backend, and context.

A child using the parent's worktree resolves to the parent's owner and may
lease the existing service. A child using another worktree may have the same
root task lineage, but the canonical root creates a distinct service and
project window. When task identity is available, an unrelated top-level task
has a different root thread ID and cannot acquire the first task's worktree.

The desktop MCP transport cannot distinguish individual tasks when Codex does
not supply task identity to the server process or tool calls. Tasks served by
the same validated Codex host can therefore share Serena only when they point
at the same canonical worktree. That is an explicit transport limitation, not
a claim that unrelated tasks may safely edit one checkout concurrently.
Workspace Harbor guidance will continue to require unrelated concurrent tasks
to use different worktrees. Different canonical roots always receive separate
Serena services even when one Codex host owns both.

## Configuration and compatibility

The live session root defaults to `$CODEX_HOME/sessions`. An injectable
`WORKSPACE_HARBOR_SESSION_DIR` path supports isolated tests. The existing
`WORKSPACE_HARBOR_OWNER_ID` override remains unchanged for non-Codex callers
and explicit integrations.

No Codex configuration format, spawn prompt, capsule, Serena installation, or
session log is modified. The resolver treats Codex session metadata as a
read-only compatibility boundary. If a future Codex version changes the
format, Harbor safely isolates that shell invocation under its current thread
ID. If a future Codex version supplies task identity to MCP subprocesses, the
higher-priority session-lineage path applies automatically.

## Observability

Broker `status` continues to display the resolved owner on leases. The
read-only `owner` command's JSON output reports:

- the current thread ID;
- the resolved owner ID;
- whether resolution came from `explicit`, `root-thread`, `subagent-lineage`,
  `codex-host`, or `process-fallback`; and
- a concise fail-closed lineage reason when child metadata could not be used.

The command must not expose session contents, prompts, user text, or unrelated
thread identifiers.

## Validation

Automated tests cover:

- a top-level thread retaining its own identity;
- one-level and nested subagents resolving to the root parent;
- explicit owner override precedence;
- missing, malformed, oversized, ambiguous, inconsistent, cyclic, and
  over-depth metadata falling back to the child ID;
- unrelated root tasks remaining distinct;
- a parent and child leasing the same canonical worktree successfully;
- an unrelated task being rejected for that worktree when task identity is
  available;
- the same lineage using another worktree receiving a separate service; and
- the `owner --json` contract without session-content leakage.

Corrective tests additionally cover:

- two broker subprocesses under the same validated Codex host resolving to one
  stable owner;
- broker subprocesses under different Codex hosts remaining separate;
- PID reuse and unrecognized parent commands falling back safely;
- a valid same-host legacy lease migration succeeding atomically;
- cross-host, mixed-owner, stale, and malformed legacy leases remaining
  rejected; and
- an MCP reload exposing Serena tools after the replacement broker attaches to
  the existing persistent service.

Live dogfood spawns one bounded child in the parent's worktree. The child must
report the same resolved owner as the parent, observe the same healthy
JetBrains Serena service, and perform one semantic read without opening
another IntelliJ window or creating another broker service. A second isolated
fixture proves an unrelated owner remains rejected.

Desktop dogfood then performs a controlled MCP reload under one Codex app
server. The replacement broker must retain one persistent service, successfully
complete MCP initialization and `tools/list`, and make `initial_instructions`
available in a new task. A different canonical worktree must still create a
separate service.

## Out of scope

- Changing Codex's native subagent environment propagation.
- Sharing edit ownership or Git write coordination beyond the existing
  canonical-worktree rule.
- Making concurrent edits by unrelated top-level tasks safe in one worktree;
  callers must use separate worktrees because the desktop MCP boundary does not
  identify individual tasks.
- Reading full session histories or deriving ownership from prompt text.
- Supporting recursive child spawning; global policy still prohibits it.
