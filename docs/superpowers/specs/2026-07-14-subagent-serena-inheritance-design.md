# Subagent Serena Ownership Inheritance Design

## Goal

Let a Codex subagent reuse its root parent task's brokered Serena service when
both operate on the same canonical Git worktree. Preserve Workspace Harbor's
protection against unrelated tasks acquiring that worktree and preserve
separate services for different worktrees.

## Current behavior

The broker currently chooses the first available identity from
`WORKSPACE_HARBOR_OWNER_ID`, `CODEX_THREAD_ID`, or a process-local fallback.
Codex assigns every subagent a new `CODEX_THREAD_ID` and does not export
`CODEX_PARENT_THREAD_ID` or `WORKSPACE_HARBOR_OWNER_ID` into the child. The
broker therefore treats a parent and child as unrelated owners.

Codex does record the relationship in the first `session_meta` event of the
child's local session JSONL. That record contains the child ID, a top-level
`parent_thread_id`, and the same parent ID under
`source.subagent.thread_spawn.parent_thread_id`.

## Selected approach

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
- retain the existing process-local fallback when no Codex thread ID exists.

Failing back to the child ID is fail-closed: reuse is denied, but unrelated
tasks are never merged into one owner.

## Worktree and service isolation

Owner inheritance does not change the canonical worktree boundary. The broker
continues to key services by canonical worktree, backend, and context and
continues to reject a different logical owner for an already-owned root.

A child using the parent's worktree resolves to the parent's owner and may
lease the existing service. A child using another worktree may have the same
root task lineage, but the canonical root creates a distinct service and
project window. An unrelated top-level task has a different root thread ID and
cannot acquire the first task's worktree.

## Configuration and compatibility

The live session root defaults to `$CODEX_HOME/sessions`. An injectable
`WORKSPACE_HARBOR_SESSION_DIR` path supports isolated tests. The existing
`WORKSPACE_HARBOR_OWNER_ID` override remains unchanged for non-Codex callers
and explicit integrations.

No Codex configuration format, spawn prompt, capsule, Serena installation, or
session log is modified. The resolver treats Codex session metadata as a
read-only compatibility boundary. If a future Codex version changes the
format, Harbor safely returns to per-thread ownership until updated.

## Observability

Broker `status` continues to display the resolved owner on leases. Add a
read-only `owner` command with JSON output that reports:

- the current thread ID;
- the resolved owner ID;
- whether resolution came from `explicit`, `root-thread`, `subagent-lineage`,
  or `process-fallback`; and
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
- an unrelated task being rejected for that worktree;
- the same lineage using another worktree receiving a separate service; and
- the `owner --json` contract without session-content leakage.

Live dogfood spawns one bounded child in the parent's worktree. The child must
report the same resolved owner as the parent, observe the same healthy
JetBrains Serena service, and perform one semantic read without opening
another IntelliJ window or creating another broker service. A second isolated
fixture proves an unrelated owner remains rejected.

## Out of scope

- Changing Codex's native subagent environment propagation.
- Sharing edit ownership or Git write coordination beyond the existing
  canonical-worktree rule.
- Allowing unrelated top-level tasks to share one worktree.
- Reading full session histories or deriving ownership from prompt text.
- Supporting recursive child spawning; global policy still prohibits it.
