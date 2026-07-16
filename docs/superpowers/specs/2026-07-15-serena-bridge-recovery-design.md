# Serena Bridge Recovery Design

## Goal

Diagnose and recover the failure state where IntelliJ and Serena semantics are
healthy but a Codex task does not expose Serena MCP tools. Preserve the
existing project and IDE recovery behavior, add evidence at the broker and MCP
boundaries, and resume the same Codex task after a guarded app restart when
that restart is both safe and useful.

## Problem statement

Workspace Harbor currently observes the project through the IntelliJ plugin,
the persistent Serena service, and the worktree broker. It does not observe
the complete path from the broker's stdio proxy through MCP `initialize` and
`tools/list` to Codex's task tool inventory.

This creates an ambiguous state:

- the IntelliJ plugin can answer semantic requests;
- the persistent Serena HTTP service can be healthy;
- a direct semantic probe can pass; and
- the current Codex task can still have no Serena MCP tools.

The broker also removes a lease when its stdio proxy exits without retaining a
bounded connection-attempt record. Failures before lease insertion leave even
less evidence. A later doctor run therefore cannot distinguish project
resolution, ownership, service startup, proxy, MCP handshake, and Codex tool
exposure failures reliably.

Codex task exposure is a separate boundary. A successful synthetic MCP
handshake proves Workspace Harbor and Serena can serve tools, but only the
running Codex task can report whether its own tool inventory contains Serena.
The design keeps those claims separate.

## Chosen approach

Add a dedicated `serena-bridge-doctor` instead of extending
`serena-project-doctor` with task-host behavior. The project doctor remains the
authority for repository configuration, bootstrap, IntelliJ linkage, semantic
health, and guarded IDE recovery. The bridge doctor owns Codex configuration,
broker connection evidence, MCP handshake verification, task-exposure
classification, and guarded Codex restart coordination.

A dedicated command keeps the failure domains clear and prevents a healthy
project report from being mistaken for proof that Codex exposed MCP tools.

## Command surface

### Read-only status

```text
serena-bridge-doctor status ROOT [--reported-tools present|missing|unknown] [--probe] [--json]
```

The command validates:

- the configured Serena MCP stanza without printing environment values;
- the deployed broker, wrapper, proxy, and project-doctor executables;
- current owner resolution and broker state;
- recent privacy-safe connection attempts for the exact canonical root;
- a bounded synthetic MCP handshake when `--probe` is supplied; and
- the lower-layer project report from `serena-project-doctor`.

`--reported-tools` is explicit caller evidence. The helper cannot infer a
task's model-visible tool inventory from a healthy backend or from the absence
of past tool calls. Without caller evidence, task exposure is `unknown`.

### Guarded recovery

```text
serena-bridge-doctor recover ROOT --reported-tools missing [--json]
```

Recovery performs one bounded ladder:

1. collect status evidence;
2. repair only stale broker-owned leases or services that existing ownership
   and process-identity checks prove safe to reclaim;
3. invoke `serena-project-doctor --recover ROOT` only when the lower project or
   semantic layer requires it;
4. run one synthetic MCP handshake through the real broker; and
5. classify the remaining boundary.

It does not restart Codex directly. If all Harbor-owned layers pass while the
caller reports missing tools, it returns `restart-eligible` with a recovery
incident ID. Restart coordination is a separate explicit phase because it
requires task activity evidence and a verified resume path.

### Restart preparation

```text
serena-bridge-doctor prepare-restart ROOT \
  --incident INCIDENT \
  --current-thread THREAD_ID \
  --heartbeat-id AUTOMATION_ID \
  --exclusive-proof PROOF_FILE \
  [--json]
```

This phase validates the recovery incident, the current task identity, the
one-shot heartbeat, the exclusive-task proof, and the live Codex process
identity. It writes an atomic restart checkpoint and starts a detached,
one-shot relaunch helper. The relaunch helper waits for the invoking doctor to
exit before requesting a graceful Codex shutdown and reopening the app.

The command refuses to proceed when any evidence is missing, stale,
ambiguous, or inconsistent.

### Post-relaunch resume

```text
serena-bridge-doctor resume ROOT \
  --incident INCIDENT \
  --reported-tools present|missing \
  [--json]
```

The resumed task verifies that:

- the checkpoint belongs to its thread and root;
- the Codex process identity changed from the recorded process;
- the app relaunched within the bounded recovery window;
- the synthetic MCP handshake still succeeds; and
- the task now reports Serena tools as present or missing.

The command then closes the incident. When tools are present, it returns
`healthy`. When tools remain missing, it returns `fresh-task-required`; it
does not restart Codex a second time.

## Synthetic MCP handshake

The bridge doctor launches the deployed broker with an explicit canonical
project and drives the stdio MCP protocol directly:

1. send `initialize` with a Workspace Harbor diagnostic client identity;
2. require a successful response within a bounded timeout;
3. send `notifications/initialized`;
4. send `tools/list`;
5. require at least one expected Serena tool name; and
6. close the connection and require lease cleanup.

The diagnostic records only method names, success or sanitized failure class,
durations, and tool-name count. It never records request parameters, response
bodies, tool descriptions, source content, prompts, or tool arguments.

The handshake proves the complete Harbor-owned path through the broker,
service, and stdio proxy. It does not prove Codex registered those tools in the
current task.

## Connection journal

The broker writes a bounded, privacy-safe JSONL journal under
`~/.codex/state/serena-bridge/`. Each attempt records only:

- schema version and timestamp;
- canonical-root digest and service key;
- owner resolution category, never a raw session record;
- stages reached: project resolution, state lock, ownership, service reuse or
  start, lease insertion, proxy start, proxy exit;
- sanitized failure class and bounded duration; and
- whether cleanup completed.

The synthetic client appends `initialize` and `tools/list` outcomes to the
same incident. Journal rotation is size- and count-bounded. Writes use locking
and atomic replacement where a summary file is maintained. Journal failure is
reported but must not prevent an otherwise valid MCP connection.

## Task exposure contract

The caller must report its own tool surface as `present`, `missing`, or
`unknown`. Global agent guidance will require the agent to inspect its exposed
tool names before using bridge recovery. The doctor rejects contradictory
claims, such as `present` paired with a request to repair missing exposure.

The doctor uses these terms precisely:

- `backend-healthy`: direct Serena semantics work;
- `handshake-healthy`: the broker completes `initialize` and `tools/list`;
- `task-tools-present`: the current task reports Serena tools;
- `task-tools-missing`: the current task reports no Serena tools; and
- `task-exposure-unknown`: the caller supplied no task evidence.

No lower-layer status is promoted into a task-exposure claim.

## Exclusive-task proof

Restart eligibility is established by the Codex app's thread-management
surface, not by scanning process titles or guessing from recently modified
session files. Immediately before restart, the agent must:

1. list local Codex tasks through the app tool;
2. identify every task reported as active;
3. read the active task summaries to distinguish active turns from loaded but
   idle task state when the app surface requires that refinement; and
4. create a short-lived proof containing only the current thread ID, local
   host ID, active-task IDs, observation timestamp, and an integrity nonce.

Restart is allowed only when the current task is the sole active task. Any
unavailable host, unknown status, second active task, active child task, stale
observation, mismatched current thread, or malformed proof refuses restart.

The proof expires after thirty seconds. The relaunch helper revalidates that
the recorded Codex PID, start time, and exact executable still match before
requesting shutdown. This is intentionally fail-closed. Session JSONL and
window titles are not authoritative restart evidence.

The proof is an attestation from the trusted current Codex agent after using
the app's thread-management tool; it is not a claim that a standalone shell
process can query private app state. `prepare-restart` is unavailable to
unattended shell callers unless they provide that fresh app-derived proof.

## Resume heartbeat

Before restart, the agent creates a one-shot heartbeat automation targeted at
the current thread. This is preferred over `codex exec resume`, a deep link,
or `launchd`:

- a heartbeat resumes the same app task with existing context;
- `codex exec resume` starts a separate CLI runner and can create concurrent
  ownership or turn conflicts;
- a deep link can open a task but does not start a continuation turn; and
- an OS scheduler lacks Codex task context and duplicates app scheduling.

The heartbeat prompt contains only the incident ID, canonical root, and the
instruction to run the post-relaunch resume phase before continuing. It does
not copy the task transcript or source content.

`prepare-restart` reads only the heartbeat's allowlisted local metadata and
requires:

- the target thread to equal the current thread;
- a one-shot or short-lived heartbeat kind;
- a next run inside the recovery window;
- an enabled state; and
- a prompt containing the matching incident identifier.

If the app remains down past the original time, normal app scheduling may run
the heartbeat after relaunch. The detached helper also opens the exact task
with the documented `codex://threads/THREAD_ID` deep link after the app is
ready, but the heartbeat remains the mechanism that starts the continuation
turn.

The resumed agent deletes or disables the recovery heartbeat through the app
automation tool after closing the incident. The bridge doctor never edits
Codex automation files directly. Cleanup is idempotent so a delayed duplicate
wakeup observes a closed incident and exits without starting another recovery
cycle.

## Codex restart guard

Codex restart is the final bridge-recovery tier and is attempted at most once
per incident. It is permitted only after:

- the caller reported missing task tools;
- the synthetic MCP handshake passed;
- Harbor-owned repairs no longer apply;
- exclusive-task proof passed;
- the resume heartbeat passed validation; and
- the exact live Codex executable, PID, and process start identity matched.

The relaunch helper requests graceful termination only. It never sends KILL.
If Codex does not exit within the bounded grace period, the helper records
`codex-restart-incomplete` and stops. It does not manipulate session files or
terminate child tasks independently.

Automatic restart remains disabled until a controlled dogfood proves that a
graceful app restart can rehydrate Serena tools in the resumed existing task.
If dogfood disproves that hypothesis, the implementation retains diagnosis,
heartbeat-free manual recovery, and `fresh-task-required`, but does not enable
the restart tier.

## Status model

The bridge doctor returns one primary status:

- `healthy`: handshake and reported task exposure are healthy;
- `exposure-unverified`: Harbor is healthy but task exposure is unknown;
- `harbor-repaired`: a Harbor-owned failure was repaired; caller should
  recheck its tool surface;
- `lower-layer-blocked`: project, IDE, broker, or semantic recovery remains
  unhealthy;
- `restart-eligible`: Harbor is healthy, task tools are missing, and restart
  coordination may begin;
- `codex-restart-refused-active-tasks`: another or ambiguous active task
  prevents restart;
- `codex-restart-refused-resume`: the heartbeat or checkpoint cannot guarantee
  continuation;
- `codex-restart-incomplete`: graceful termination or relaunch did not finish;
- `resume-pending`: the app relaunched and the heartbeat has not completed;
- `fresh-task-required`: one guarded restart did not restore task tools; and
- `invalid-state`: configuration, identity, or incident evidence is malformed
  or contradictory.

Human output leads with the status and one next action. JSON output includes
stable reason codes and no raw logs.

## Recovery budget

One bridge incident permits:

- one broker-state repair pass;
- one lower-layer project recovery pass when required;
- one synthetic handshake retry after meaningful repair; and
- at most one guarded Codex restart.

Ordinary file edits, commits, bootstrap completion, or repeated missing-tool
observations do not reset the incident. A successful task tool check closes
it. A failed post-restart check terminates it as `fresh-task-required`.

## Configuration and deployment

The deployer installs the bridge doctor and relaunch helper alongside existing
Workspace Harbor commands. Global agent guidance gains a short bridge section:

1. inspect whether Serena tools are exposed;
2. use `serena-bridge-doctor recover` for missing exposure;
3. never reopen or restart IntelliJ when the backend and handshake are healthy;
4. create an exclusive-task proof and one-shot heartbeat only when the doctor
   returns `restart-eligible`; and
5. on heartbeat resume, run the incident resume command before continuing.

No repository-local `AGENTS.md` needs to duplicate the global policy.

Automatic Codex restart is controlled by a local Workspace Harbor setting and
ships disabled until live dogfood passes. Enabling it does not weaken the
exclusive-task, heartbeat, identity, or single-attempt guards.

## Validation

Unit and integration tests cover:

- missing, disabled, malformed, and valid Serena MCP configuration;
- missing or non-executable broker, wrapper, proxy, and doctor commands;
- broker failures before and after lease insertion producing bounded journal
  records;
- journal write failure not breaking a valid connection;
- journal rotation and absence of prompts, parameters, response bodies,
  environment values, or source content;
- successful `initialize` and `tools/list` through a fixture broker;
- initialize timeout, protocol error, early EOF, malformed JSON, missing tools,
  tools-list timeout, proxy failure, and lease-cleanup failure;
- strict separation of backend, handshake, and task-exposure states;
- one repair retry and no unchanged-state loops;
- exclusive proof with one current task succeeding;
- second active task, active child, unavailable host, unknown status, stale
  proof, mismatched thread, and malformed proof refusing restart;
- missing, disabled, wrong-thread, stale, or mismatched heartbeat refusing
  restart;
- exact Codex process identity validation and PID-reuse refusal;
- graceful restart timeout stopping without KILL;
- closed incidents ignoring delayed duplicate heartbeat runs;
- one post-relaunch success and one `fresh-task-required` terminal failure; and
- deployed-helper and global-guidance verification.

Live dogfood uses a disposable task and repository fixture. It first proves a
synthetic handshake without restarting anything. Restart dogfood runs only
when the app task inventory proves the dogfood task is the sole active task,
creates and validates its heartbeat, restarts Codex once, resumes the same
thread, and checks whether Serena tools appear. Any ambiguity aborts the live
restart test.

## Out of scope

- Restarting Codex when another task may be active.
- Forced Codex termination.
- Restarting IntelliJ for a task-host-only failure.
- Editing Codex session JSONL or task databases.
- Treating broker or backend health as proof of task tool exposure.
- Starting a separate `codex exec resume` runner to simulate app continuation.
- Reimplementing Codex MCP registration or scheduling.
- Automatically creating a fresh task after restart failure.
