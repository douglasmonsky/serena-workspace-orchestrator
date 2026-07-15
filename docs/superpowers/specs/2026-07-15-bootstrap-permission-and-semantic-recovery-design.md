# Bootstrap Permission and Semantic Recovery Hardening Design

## Goal

Make Workspace Harbor dependency preparation reliable inside a normal managed
Codex sandbox without weakening its execution safeguards or allowing an
optional bootstrap problem to disable IntelliJ-backed Serena semantics.

This is a focused reliability correction to the existing persistent-bootstrap
design. The broader Serena replacement and VS Code product idea is explicitly
out of scope.

## Confirmed failure

For an unchanged prepared worktree, `workspace-harbor-bootstrap status`
correctly reports `ready` with a cache hit. The corresponding `run` command
currently acquires the per-worktree lock before checking that status. Acquiring
the lock attempts to create or change files beneath
`$CODEX_HOME/state/workspace-harbor/bootstrap`, which is read-only in an
ordinary managed Codex sandbox.

That unnecessary write raises `EPERM`. The bootstrap CLI classifies every
`OSError` as `invalid`, and the IntelliJ opener suppresses the structured
result and converts exit status `2` into the generic message
"dependency bootstrap configuration or state is invalid." The opener then
aborts before restoring the exact-root IntelliJ/Serena service.

The resulting diagnosis is wrong in three ways:

- the cached dependency environment is already ready;
- the repository configuration and persisted success record are valid; and
- waiting for indexing, a branch transition, or another IDE state change
  cannot repair a local permission failure in the bootstrap runner.

## Product policy

Workspace Harbor will fail closed on dependency execution and state mutation,
but fail partially on optional dependency preparation. In particular:

- no setup command runs unless its plan, approval, and state checks pass;
- no success record is written unless the selected plans complete and their
  protected inputs remain unchanged;
- a cache hit performs no state mutation and requires no write permission;
- a bootstrap problem may leave dependency setup degraded, but it does not
  revoke the separate authorization to trust and open the exact project root;
  and
- trust, ownership, project identity, and IDE lifecycle safety failures retain
  their existing fail-closed behavior.

## Bootstrap run contract

`run_bootstrap` uses a read-only fast path before acquiring its execution lock:

1. Resolve and validate the canonical repository root.
2. Compute `bootstrap_status(root)` without creating state directories, lock
   files, or records.
3. Return immediately for `ready` cache hits when `--force` is absent, and for
   `disabled`, `not-needed`, or `needs-decision` results.
4. Acquire the per-worktree lock only when work may execute or mutable state
   may change.
5. Recompute status while holding the lock. This second check preserves
   single-flight behavior when another process finishes bootstrap while the
   caller waits.
6. Return the new cache hit without execution, or execute the validated plan
   and atomically record success using the existing safeguards.

`--force` deliberately bypasses the read-only `ready` return, acquires the
lock, and performs the forced run. Pending work continues to require a
writable private state directory. A failure to obtain that permission prevents
execution; it never causes an unlocked setup run.

## Error taxonomy and exit behavior

The CLI keeps the established status and exit-code surface, while separating
validation failures from operational failures:

- `invalid`, exit `2`: an invalid root, malformed configuration, malformed
  persisted state, invalid decision arguments, or another explicit validation
  failure;
- `failed`, exit `1`, `failure_kind: permission-denied`: `EACCES` or `EPERM`
  while acquiring the state lock or performing another required state
  mutation;
- `failed`, exit `1`, `failure_kind: io-error`: another operational filesystem
  failure after input validation;
- `failed`, exit `1`, `failure_kind: process-error`: a subprocess launch or
  supervision failure; and
- the existing structured failure kinds for nonzero setup commands, changed
  inputs, and missing environment markers.

Exception handling must classify errors at the boundary where the operation is
known. It must not globally reinterpret every `OSError` as either invalid or a
permission failure. Validation helpers should continue to raise or translate
explicit validation errors, while lock, record, and execution boundaries add a
sanitized operation name and failure kind.

All machine-readable failures include a bounded, sanitized `error` or
`failure_context`. They must not include environment variables, credentials,
private dependency URLs, or unbounded command output.

## IntelliJ opener behavior

Dependency preparation remains ordered before the already-open project
shortcut, but it is not a prerequisite for semantic startup. The opener will:

- run bootstrap once;
- retain a bounded structured result instead of discarding both output
  streams;
- report the actionable category and sanitized detail once; and
- continue opening or reusing the exact trusted IntelliJ project for every
  bootstrap non-success result, including `failed`, `needs-decision`,
  `invalid`, helper unavailable, and an unknown bootstrap exit status.

Continuing means only that IntelliJ and Serena may start. It does not convert
bootstrap to `ready`, run a fallback installer, record success, retry the same
command, or conceal the degraded dependency state.

The opener still stops for an invalid project root, a trust failure, ambiguous
window ownership, unsafe IDE state, or another existing lifecycle guard. This
change therefore separates dependency readiness from semantic availability
without relaxing project or process safety.

## Doctor and recovery behavior

`serena-project-doctor --recover` continues to use the opener as its sole
project-open path. Because bootstrap degradation no longer makes the opener
fail, recovery may proceed to the bounded IntelliJ/Serena health checks and
report dependency preparation as a warning alongside the actual semantic
result.

No new retry loop is added. A permission failure is immediately actionable and
must not be described as a condition that indexing or an unrelated IDE restart
will repair. The existing one-recovery-attempt-per-state-epoch policy remains
unchanged.

## Verification

Automated tests will prove these contracts:

- a `ready` cache hit returns without entering `_worktree_lock` or writing
  private state;
- `disabled`, `not-needed`, and `needs-decision` also remain read-only;
- `--force` and pending work still acquire the lock;
- status is recomputed under the lock so concurrent callers execute at most
  once;
- `EPERM` and `EACCES` at a mutation boundary produce `failed` with
  `failure_kind: permission-denied` and exit `1`;
- malformed configuration remains `invalid` with exit `2`;
- subprocess and non-permission I/O failures receive their distinct bounded
  classifications;
- the opener continues to the exact-root project path for every bootstrap
  degradation class and reports one actionable warning;
- bootstrap degradation never invokes an unapproved fallback command; and
- existing trust, ownership, concurrency, redaction, and lifecycle tests stay
  green.

A fixture-level restricted-state scenario will exercise a prepared cache hit
with the mutable bootstrap state path made unavailable. It must reproduce the
managed-sandbox constraint without interacting with an installed plugin or a
live IDE.

## Rollout

Source changes land together for the bootstrap helper, IntelliJ opener, tests,
and operator documentation. Deployment then updates the compatible helper set
under `~/.codex/bin` through the repository's existing installer and verifies
the read-only cache-hit scenario before any live recovery exercise.

No plugin change, broker-state migration, repository configuration change, or
new user decision is required. Existing valid success records remain valid.

## Acceptance criteria

The fix is complete when a prepared worktree can pass `bootstrap run` from a
managed read-only Codex sandbox, an actual bootstrap permission failure is
reported as operational degradation rather than invalid configuration, and
the same failure cannot prevent the guarded opener/doctor path from restoring
IntelliJ-backed Serena semantics.

## Out of scope

- Building or forking a Serena replacement.
- Adding VS Code or another editor backend.
- Changing Serena's indexing or language-server implementation.
- Broadening trust allowlists or sandbox filesystem permissions.
- Adding unbounded bootstrap retries or manual IDE process control.
