# Queued IntelliJ Opener Design

## Goal

Make simultaneous Serena recovery requests open the required IntelliJ project
windows reliably. Requests for different canonical worktrees must not fail
merely because another project is opening or indexing, while duplicate requests
for one worktree must still produce one managed window and one Serena service.

This design corrects the opener's concurrency model. It does not change Serena
activation, broker ownership, project trust policy, reaper safety policy, or
the rule that Workspace Harbor reuses one IntelliJ application process.

## Confirmed failure

`open-codex-project-in-intellij` currently owns one global directory lock from
immediately before project trust through the complete Serena and IntelliJ model
readiness wait. A readiness wait may take 120 seconds. A competing opener waits
only 30 seconds for that global lock.

Consequently, concurrent recovery has a deterministic failure mode:

1. Recovery A acquires the global opener lock, opens worktree A, and waits for
   indexing and Serena readiness.
2. Recovery B correctly requests an IntelliJ window for worktree B.
3. Recovery B cannot enter the global lock while A is waiting.
4. After 30 seconds, B reports `opener-failed` even though B's root, trust
   state, IntelliJ host, and Serena configuration may all be valid.

The failure is most visible after an application restart or scheduled cleanup,
when several active tasks simultaneously discover missing exact-root services.
Increasing the lock timeout would postpone the failure and serialize indexing;
it would not correct the ownership model.

## Ownership boundary

The canonical Git worktree root is the unit of IDE and Serena ownership.

- One canonical worktree has at most one managed IntelliJ project window and
  one brokered Serena service for the selected backend and context.
- A parent task and its subagents share the same window and service when they
  share the exact worktree.
- Different worktrees of the same repository, normally checked out on
  different branches, have separate windows and services.
- Two standalone clones are different worktrees even if they use the same
  repository and branch name.
- Changing branches inside one worktree does not create another IDE ownership
  boundary. The existing window remains attached to that canonical root.

Branch names and repository remotes are descriptive metadata, not lock keys.
Every concurrency key derives from the validated canonical root.

## Selected architecture

The opener uses two independent coordination layers:

1. A per-worktree single-flight operation prevents duplicate open attempts for
   one canonical root and remains owned through readiness and reaper
   registration.
2. A durable FIFO launch queue serializes only the short global mutation that
   trusts a root and sends its open request to the existing IntelliJ process.

After the launch request returns, the request leaves the global queue before it
waits for project indexing, the exact-root Serena service, or the native
project model. Different worktrees can therefore become ready concurrently.

## State model

Private coordination state remains beneath:

`~/.codex/state/intellij-projects`

The opener adds:

- `operations/<root-hash>/owner`: the per-worktree single-flight owner;
- `launch-queue.json`: ordered waiting entries plus the current launch owner;
  and
- `launch-queue.lock`: an advisory transaction lock used only while reading or
  atomically replacing queue state.

The root hash is SHA-256 of the resolved canonical root, truncated only to a
collision-resistant filename length. Records also contain the full canonical
root and must match it before reuse.

Owner and queue records contain only:

- a random request identifier;
- PID and process start time;
- canonical project root;
- enqueue timestamp;
- assigned monotonically increasing sequence number; and
- current phase.

State directories use mode `0700`; files use `0600`. Queue writes use a
same-directory temporary file, flush, and atomic replacement. Malformed,
wrong-owner, wrong-root, or insecure state fails closed and is not silently
deleted.

## FIFO launch queue

Enqueueing is a short transaction under `launch-queue.lock`:

1. Load and validate the complete queue state.
2. Prune only entries whose PID and recorded process start time prove that the
   owning opener no longer exists.
3. Allocate the next sequence number and append the request.
4. Atomically persist the new queue.

Requests are eligible by sequence number. A requester periodically checks the
queue under the transaction lock. The oldest valid waiter may claim the launch
owner when no validated live owner exists. Later requests remain queued; they
do not fail because the current owner is healthy.

The launch owner performs only these steps:

1. Recheck exact-root Serena and native model readiness.
2. Apply exact-root project trust.
3. Recheck readiness after trust.
4. Send the IntelliJ open request if still needed.
5. Release launch ownership and remove its queue entry in an atomic queue
   transaction.

The owner never holds the queue transaction lock while running trust, invoking
IntelliJ, probing services, sleeping, or waiting for indexing. The logical
launch owner prevents another request from entering that critical section.

Queue cleanup runs from the opener's exit and signal traps. If cleanup cannot
complete, a later waiter may reclaim the entry only after PID, process start
time, and opener command identity prove that its owner is dead. Ambiguous
identity remains protected and produces a bounded safety failure rather than
unsafe deletion.

Strict FIFO applies among live valid entries. A same-worktree follower does not
add a second launch entry while a validated single-flight owner exists.

## Per-worktree single flight

Before entering the launch queue, the opener acquires the operation for its
canonical root. The operation owner remains responsible until one of these
terminal outcomes:

- the exact-root Serena service and native model are ready and the project is
  registered with the reaper;
- trust or launch fails;
- readiness reaches its configured overall deadline; or
- a fail-closed identity or state validation rejects the operation.

A concurrent request for the same canonical root joins the existing operation.
It waits conditionally, periodically rechecking exact-root readiness and owner
liveness. When the first operation finishes, the follower returns success if
the project is ready. If the prior owner failed, one follower may become the
new operation owner and perform a fresh bounded attempt; other followers
continue to join it.

This preserves same-root deduplication after the global launch gate becomes
short-lived. It also prevents two tasks from sending duplicate open requests
during the interval between IntelliJ accepting a root and Serena becoming
ready.

## Waiting and deadlines

Queueing is a normal state, not an opener failure. There is no 30-second
contention timeout for a validated healthy launch owner.

Each opener retains one bounded overall deadline so an abandoned task cannot
wait forever. The deadline budget covers:

- waiting for a same-worktree operation;
- waiting in the global launch queue;
- exact-root trust and launch; and
- Serena and native model readiness.

The default budget must be long enough for the existing readiness allowance
plus a configurable queue allowance. Doctor and broker subprocess timeouts are
derived from the same values and include their existing termination grace, so
an outer caller cannot kill a healthy queued opener before its own documented
deadline.

The opener reports a concise final queue summary containing sequence, maximum
observed position, queue wait duration, and launch duration. It does not expose
other roots, PIDs, task identifiers, or queue contents. A deadline failure
distinguishes `queue-deadline`, `operation-deadline`, and `readiness-deadline`.
Doctor recovery preserves that classification instead of flattening all three
to `opener-failed`.

## Failure handling

The opener continues to fail closed for:

- malformed or insecure queue and operation state;
- an ambiguous live owner;
- a trust rejection;
- an IntelliJ executable or runtime identity mismatch;
- a foreign or ambiguous exact-root Serena service;
- an open command failure; and
- reaper registration ambiguity after readiness.

A live queue owner is not considered hung merely because its project is slow
to index. The launch owner does not wait for indexing, so a long-lived launch
phase instead indicates a trust or IntelliJ open call that exceeded its own
bounded command allowance. Recovery reports the phase and leaves ambiguous
state intact for diagnosis.

No opener path closes windows, restarts IntelliJ, signals IDE processes, edits
broker state, or bypasses the existing authenticated trust mechanism.

## Command and integration behavior

The public command remains:

`open-codex-project-in-intellij [--require-github] [PROJECT_DIR]`

Existing callers do not need a new activation command. `serena-project-doctor
--recover` and `serena-worktree-broker` continue to invoke the opener and use
its exit status. Their timeout calculations and diagnostic parsing are updated
to understand the overall deadline and queue-specific results.

`intellij-project-reaper` remains independent. A successfully ready project is
registered or touched exactly as today. Queue presence alone is not a reaper
lease and does not make an unopened project managed.

## Observability

Human-readable output uses state transitions that agents can classify:

- `joined existing worktree open`;
- `queued for IntelliJ launch`;
- `launch request accepted`;
- `waiting for exact-root readiness`;
- `ready`; or
- a specific terminal failure category.

Machine-readable doctor history records sanitized fields for whether the
request joined, its maximum queue position, queue wait duration, launch
duration, and terminal phase. It never records the contents of another
request's queue entry.

## Testing strategy

All tests use fixture-local state, fake trust/open/service/reaper commands, and
synthetic process owners. They do not contact installed plugins or live IDE
processes.

Required regression coverage includes:

- two different roots both launch while the first root's readiness probe is
  still blocked;
- the global launch critical sections do not overlap;
- launch requests are admitted in FIFO sequence;
- three or more queued roots drain without a 30-second contention failure;
- two requests for one root produce one trust call, one open call, and one
  reaper registration;
- a same-root follower observes the leader's success;
- a same-root follower may take over after a verified-dead leader;
- dead queue and operation owners are reclaimed using PID and start-time
  identity;
- live and ambiguous owners are preserved;
- malformed or insecure state fails closed;
- signal and normal-exit cleanup remove only the caller's records;
- queue, operation, and readiness deadlines retain distinct diagnostics;
- doctor and broker outer timeouts cannot preempt a valid configured queue
  wait; and
- existing trust, foreign-service, bootstrap, and reaper-registration tests
  remain green.

The concurrency fixtures use explicit synchronization files or pipes rather
than timing-only sleeps wherever possible. Short timeouts exist only as test
deadlock guards.

## Deployment

Source changes for the opener, doctor, broker, tests, documentation, and local
deployment manifest land as one compatible unit. Deployment copies the updated
commands into `~/.codex/bin` only after repository tests pass. A post-deploy
fixture diagnostic verifies queue state creation and cleanup without opening
IntelliJ.

Existing `opener.lock` state is migration input. If no live owner exists, the
new opener removes a valid stale legacy record before using the queued model.
A verified live legacy owner remains protected until it exits. Malformed legacy
state fails closed with a migration-specific diagnostic.

Rollback restores the prior command set and leaves the new queue files inert.
The old opener does not read them. Rollback must not remove ambiguous live
state automatically.

## Out of scope

- Multiple managed windows for one canonical worktree.
- Treating branch names as ownership identifiers.
- Changing broker service-sharing rules.
- Changing dependency bootstrap selection or permissions.
- Changing project trust allowlists or authentication.
- Closing projects or restarting IntelliJ from the opener.
- Replacing the lifecycle reaper or changing its idle thresholds.
