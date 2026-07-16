# Queued IntelliJ Opener Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the long-held global IntelliJ opener lock with canonical-worktree single-flight operations and a durable FIFO launch queue so concurrent recovery opens different project windows reliably without duplicating same-worktree windows.

**Architecture:** A new standard-library Python module owns validated private coordination state and exposes a small CLI consumed by the existing zsh opener. The opener holds a per-worktree logical lease through readiness, holds the FIFO launch claim only through trust and the IntelliJ open request, then releases it before indexing. Doctor and broker derive their outer subprocess limits from the same operation, queue, bootstrap, and readiness budgets and preserve queue-specific failure classifications.

**Tech Stack:** Python 3 standard library (`dataclasses`, `fcntl`, `hashlib`, `json`, `os`, `pathlib`, `tempfile`, `time`, `uuid`), zsh, `unittest`, fixture-local shell commands, existing Workspace Harbor deployer.

## Global Constraints

- The canonical Git worktree root is the IDE, Serena, operation-lock, and queue-deduplication boundary; branch names and remotes are never lock keys.
- One canonical worktree has at most one managed IntelliJ window and one brokered Serena service for the selected backend and context.
- The global FIFO claim covers only readiness rechecks, exact-root trust, and the IntelliJ open request; it is released before indexing, Serena readiness, or native-model readiness waits.
- Queue and operation state lives under `~/.codex/state/intellij-projects`, with directories mode `0700`, files mode `0600`, validated owner identity, and atomic state replacement.
- Valid live owners are queued behind, never evicted because of elapsed indexing time. Dead owners are reclaimed only after PID and process-start verification; malformed or ambiguous state fails closed.
- No implementation or test may close an IDE window, restart IntelliJ, signal an IDE process, alter broker ownership, or contact an installed plugin.
- All concurrency tests use fixture-local state and fake trust, opener, Serena, model, and reaper commands.

## File responsibility map

- Create `bin/workspace_harbor_opener_queue.py`: canonical-root coordination state, process identity validation, per-worktree single flight, FIFO launch queue, legacy-lock migration, atomic state, CLI and sanitized result packets.
- Modify `bin/open-codex-project-in-intellij`: bootstrap and readiness orchestration; calls the coordinator and limits global ownership to trust/open.
- Modify `bin/serena-project-doctor`: shared budget calculation and queue-aware recovery classification/history.
- Modify `bin/serena-worktree-broker`: shared budget calculation and queue-aware IntelliJ launch errors.
- Modify `bin/deploy-workspace-harbor`: deploy the new module atomically with the compatible command set.
- Create `tests/python/test_workspace_harbor_opener_queue.py`: pure coordinator state, FIFO, owner-liveness, permissions, deadline, cleanup, and legacy migration tests.
- Modify `tests/python/test_open_codex_project_in_intellij.py`: process-level same-root and different-root concurrency behavior.
- Modify `tests/python/test_serena_project_doctor.py`: outer timeout and queue diagnostic preservation.
- Modify `tests/python/test_serena_worktree_broker.py`: outer timeout and queue diagnostic preservation.
- Modify `tests/python/test_deploy_workspace_harbor.py`: complete deployment manifest coverage.
- Modify `README.md`: ownership boundary, queue behavior, diagnostics, and configuration.
- Modify `docs/superpowers/specs/2026-07-12-intellij-only-cutover-design.md`: supersede the former long-held global-lock sequence with a link to the approved queued design.

---

### Task 1: Durable opener coordinator

**Files:**
- Create: `bin/workspace_harbor_opener_queue.py`
- Create: `tests/python/test_workspace_harbor_opener_queue.py`

**Interfaces:**
- Consumes: canonical root strings; state directory; caller PID, process start, and request ID; absolute UNIX deadline; poll interval.
- Produces: `root_digest(root: Path) -> str`, `OwnerIdentity`, `CoordinationError`, `OpenerCoordinator.acquire_operation(root, owner, deadline)`, `release_operation(root, owner)`, `acquire_launch(root, owner, deadline)`, `release_launch(owner)`, `release_all(owner)`, `migrate_legacy_lock(legacy_lock_dir)`, and CLI JSON packets with `status`, `phase`, `request_id`, `wait_seconds`, `maximum_position`, and sanitized `error`.

- [ ] **Step 1: Write failing tests for canonical-root single flight and state permissions**

Create fixture helpers that import the module by path and construct identities for the current process:

```python
MODULE_PATH = ROOT / "bin/workspace_harbor_opener_queue.py"
SPEC = importlib.util.spec_from_file_location("workspace_harbor_opener_queue", MODULE_PATH)
queue = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = queue
SPEC.loader.exec_module(queue)

def current_owner(request_id: str) -> queue.OwnerIdentity:
    return queue.OwnerIdentity(
        request_id=request_id,
        pid=os.getpid(),
        process_started=queue.read_process_start(os.getpid()),
        command_token="test_workspace_harbor_opener_queue",
    )
```

Add tests asserting:

```python
def test_one_root_has_one_operation_owner(self) -> None:
    first = current_owner("first")
    second = current_owner("second")
    self.assertEqual("acquired", self.coordinator.acquire_operation(self.root, first, self.deadline)["status"])
    with self.assertRaisesRegex(queue.CoordinationError, "operation-deadline"):
        self.coordinator.acquire_operation(self.root, second, time.time() + 0.05)

def test_state_permissions_are_private(self) -> None:
    owner = current_owner("owner")
    self.coordinator.acquire_operation(self.root, owner, self.deadline)
    self.assertEqual(0o700, self.state_dir.stat().st_mode & 0o777)
    for path in self.state_dir.rglob("*"):
        self.assertIn(path.stat().st_mode & 0o777, {0o700, 0o600})
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest tests.python.test_workspace_harbor_opener_queue -v
```

Expected: import failure because `bin/workspace_harbor_opener_queue.py` does not exist.

- [ ] **Step 3: Implement validated owner records, atomic JSON, and per-root operations**

Create these exact public types and methods:

```python
@dataclass(frozen=True)
class OwnerIdentity:
    request_id: str
    pid: int
    process_started: str
    command_token: str

class CoordinationError(RuntimeError):
    def __init__(self, phase: str, message: str) -> None:
        super().__init__(message)
        self.phase = phase

def root_digest(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:32]

def read_process_start(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        check=False, capture_output=True, text=True, timeout=2,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise CoordinationError("owner-identity", f"process identity unavailable: {pid}")
    return value

```

Define `OpenerCoordinator.__init__(state_dir: Path, *, poll_seconds: float =
0.1)`, `acquire_operation(root: Path, owner: OwnerIdentity, deadline: float) ->
dict[str, object]`, and `release_operation(root: Path, owner: OwnerIdentity) ->
None` with the behavior below.

Use `operations/<root_digest>.json` plus `operations/<root_digest>.lock`.
Acquire the advisory lock only while validating and replacing the owner record.
If another verified-live owner exists, release the advisory lock, sleep for
`poll_seconds`, and retry until `deadline`. If its PID no longer exists or its
start time differs, atomically replace it. A command mismatch, malformed JSON,
insecure mode, or root mismatch raises
`CoordinationError("operation-state", "unsafe operation owner state")`.
Only the matching request ID may release its record.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the command from Step 2.

Expected: the two operation tests pass.

- [ ] **Step 5: Write failing FIFO, dead-owner, cleanup, and malformed-state tests**

Use `multiprocessing.Process` and a result queue to start three requests in a
known enqueue order. Hold the first claim with a fixture event, then assert the
observed claims are `first`, `second`, `third`. Add these direct cases:

```python
def test_dead_launch_owner_is_reclaimed(self) -> None:
    dead = self.reaped_owner("dead")
    self.write_launch_state(owner=dead, waiting=[])
    result = self.coordinator.acquire_launch(self.root, current_owner("next"), self.deadline)
    self.assertEqual("acquired", result["status"])

def test_malformed_queue_fails_closed(self) -> None:
    self.queue_path.write_text("not-json\n", encoding="utf-8")
    os.chmod(self.queue_path, 0o600)
    with self.assertRaisesRegex(queue.CoordinationError, "malformed"):
        self.coordinator.acquire_launch(self.root, current_owner("next"), self.deadline)
    self.assertEqual("not-json\n", self.queue_path.read_text(encoding="utf-8"))

def test_release_all_removes_only_matching_request(self) -> None:
    self.seed_two_roots_and_two_queue_entries()
    self.coordinator.release_all(current_owner("first"))
    self.assertEqual(["second"], self.read_request_ids())
```

Also assert queue files are `0600`, the queue state version is `1`, sequence
numbers increase under concurrent enqueue, and `maximum_position` records the
largest one-based position observed by that request.

- [ ] **Step 6: Run the expanded tests and verify RED**

Run the command from Step 2.

Expected: failures because launch queue and cleanup methods are absent.

- [ ] **Step 7: Implement FIFO launch coordination and atomic cleanup**

Add `acquire_launch(root: Path, owner: OwnerIdentity, deadline: float) ->
dict[str, object]`, `release_launch(owner: OwnerIdentity) -> None`, and
`release_all(owner: OwnerIdentity) -> None` to `OpenerCoordinator`.

Store:

```json
{
  "version": 1,
  "next_sequence": 4,
  "owner": null,
  "waiting": [
    {
      "sequence": 3,
      "request_id": "request-uuid",
      "pid": 123,
      "process_started": "Thu Jul 16 12:00:00 2026",
      "command_token": "open-codex-project-in-intellij",
      "project_root": "/canonical/root",
      "enqueued_at": 1784217600.0,
      "phase": "queued"
    }
  ]
}
```

All queue read/prune/claim/release transitions occur under an `fcntl.flock`
transaction on `launch-queue.lock`. Do not hold that lock while sleeping.
Prune only provably dead entries. Claim only when the request is the lowest
live sequence and no live launch owner exists. A deadline raises
`CoordinationError("queue-deadline", "overall opener deadline reached in launch queue")`
without deleting other requests.

- [ ] **Step 8: Write and implement legacy-lock migration tests**

Tests cover absent legacy state, a verified dead owner, a verified live owner,
and a malformed owner. Implement:

```python
def migrate_legacy_lock(self, legacy_lock_dir: Path) -> dict[str, object]:
    """Remove only a valid dead legacy opener lock; protect live/ambiguous state."""
```

Return `{"status": "absent"}`, `{"status": "removed-dead"}`, or
`{"status": "live"}`. Raise
`CoordinationError("legacy-state", "unsafe legacy opener state")` for
malformed or insecure state. Never remove a verified live owner.

- [ ] **Step 9: Add the coordinator CLI and test exact exit packets**

Provide subcommands:

```text
operation-acquire ROOT --request-id ID --owner-pid PID --owner-start START --deadline EPOCH
operation-release ROOT --request-id ID --owner-pid PID --owner-start START
launch-acquire ROOT --request-id ID --owner-pid PID --owner-start START --deadline EPOCH
launch-release --request-id ID --owner-pid PID --owner-start START
release-all --request-id ID --owner-pid PID --owner-start START
migrate-legacy PATH
self-check
```

Each command prints exactly one JSON object. Exit `0` for success, `1` for a
deadline or operational failure, and `2` for invalid arguments, malformed
state, insecure state, or owner ambiguity. Bound `error` to 500 characters and
never include another queue entry's root, PID, or request ID.

`self-check` creates a private `TemporaryDirectory`, acquires and releases one
operation and one launch claim for a synthetic root using the current process,
asserts that no owner or waiting record remains, and returns
`{"status": "healthy", "phase": "self-check"}`. Its test patches process
identity only within the fixture and asserts no configured production state
path is created.

- [ ] **Step 10: Run coordinator tests and commit**

Run:

```bash
python3 -m unittest tests.python.test_workspace_harbor_opener_queue -v
```

Expected: PASS with FIFO, single-flight, permissions, cleanup, migration, and
CLI cases green.

Commit:

```bash
git add -- bin/workspace_harbor_opener_queue.py tests/python/test_workspace_harbor_opener_queue.py
git commit -m "feat: add queued IntelliJ opener coordination"
```

---

### Task 2: Integrate single flight and the short launch claim

**Files:**
- Modify: `bin/open-codex-project-in-intellij`
- Modify: `tests/python/test_open_codex_project_in_intellij.py`

**Interfaces:**
- Consumes: Task 1 coordinator CLI; existing bootstrap, trust, open, service/model probe, and reaper commands.
- Produces: one same-root operation through readiness; FIFO launch ownership only around trust/open; sanitized queue summary; exit phases `operation-deadline`, `queue-deadline`, and `readiness-deadline`.

- [ ] **Step 1: Replace the obsolete contention test with a failing parallel-readiness regression**

Change `test_concurrent_requests_for_different_projects_do_not_overlap` so the
first fake open returns after creating `first-launched`, but its Serena probe
blocks on `allow-first-ready`. Start the second opener, require
`second-launched` before setting `allow-first-ready`, then release both probes:

```python
self.assertTrue(self.wait_for(first_launched), "first root never launched")
second = subprocess.Popen(
    [str(HELPER), str(second_project)],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    env=environment,
)
self.assertTrue(
    self.wait_for(second_launched),
    "second launch was blocked by first root readiness",
)
allow_first_ready.touch()
allow_second_ready.touch()
self.assertEqual([0, 0], [first.wait(timeout=5), second.wait(timeout=5)])
self.assertFalse(overlap_log.exists(), "trust/open critical sections overlapped")
```

The fake opener retains the `mkdir open-active` overlap detector, proving
launch critical sections remain serialized even though readiness overlaps.

- [ ] **Step 2: Run the regression and verify RED**

Run:

```bash
python3 -m unittest tests.python.test_open_codex_project_in_intellij.OpenCodexProjectInIntellijTests.test_concurrent_requests_for_different_projects_do_not_overlap -v
```

Expected: FAIL because the second launch cannot occur until the first readiness
wait releases the current global lock.

- [ ] **Step 3: Route opener ownership through the coordinator**

Add environment/configuration values:

```zsh
coordinator_command="${WORKSPACE_HARBOR_OPENER_QUEUE_COMMAND:-$HOME/.codex/bin/workspace_harbor_opener_queue.py}"
operation_allowance="${INTELLIJ_OPENER_OPERATION_TIMEOUT:-300}"
queue_allowance="${INTELLIJ_OPENER_QUEUE_TIMEOUT:-300}"
request_id="${INTELLIJ_OPENER_REQUEST_ID:-$(uuidgen | tr '[:upper:]' '[:lower:]')}"
owner_started="$(ps -p $$ -o lstart= | sed 's/^ *//;s/ *$//')"
```

After canonical-root and bootstrap handling but before the first mutable open
attempt:

1. Compute one `overall_deadline` as `now + operation_allowance +
   queue_allowance + ready_timeout`.
2. Run `migrate-legacy "$opener_lock_dir"` once.
3. Run `operation-acquire` with `overall_deadline`.
4. Install a trap that invokes `release-all` for this exact owner.
5. Recheck readiness; ready roots touch the reaper and exit.
6. Run `launch-acquire` with the same `overall_deadline`.
7. Recheck readiness, trust the exact root, recheck readiness, and invoke open.
8. Run `launch-release` immediately after the open command returns, including
   the trust/open error paths.
9. Wait for exact-root readiness outside the launch claim, bounded by both
   `now + ready_timeout` and `overall_deadline`.
10. Register with the reaper, then release the operation through the exit trap.

Capture each coordinator JSON response in a bounded variable, validate its
`status` and `phase` with `python3 -c`, and print only the caller's sanitized
summary. Remove `acquire_opener_lock`, `release_opener_lock`, and the 30-second
`INTELLIJ_OPENER_LOCK_TIMEOUT` behavior.

- [ ] **Step 4: Run the parallel regression and verify GREEN**

Run the command from Step 2.

Expected: PASS; the second root launches before first-root readiness, while the
fake trust/open overlap detector remains empty.

- [ ] **Step 5: Add failing same-root join and FIFO process tests**

Add four named tests:

- `test_same_root_followers_open_and_register_once`: start two helpers for one
  root behind a blocked readiness probe and assert one trust, open, and register.
- `test_three_different_roots_launch_in_fifo_order`: enqueue three roots behind
  a gated first trust command and assert exact launch order.
- `test_queue_wait_does_not_use_legacy_thirty_second_failure`: configure a
  short former lock-timeout variable and prove it no longer terminates a valid
  queued request.
- `test_signal_cleanup_removes_only_callers_operation_and_queue_records`:
  terminate one fixture opener and assert only its request records disappear.

The same-root fixture starts two helpers while readiness is blocked and asserts
one trust line, one open line, one `register` line, two zero exits, and one
output containing `joined existing worktree open`. The FIFO fixture gates the
first trust call, starts roots in explicit order, and asserts the open log is
`first`, `second`, `third`. Use synchronization files/pipes, not assumed sleep
ordering.

- [ ] **Step 6: Run new tests and verify RED where behavior is incomplete**

Run:

```bash
python3 -m unittest tests.python.test_open_codex_project_in_intellij -v
```

Expected: at least the join wording, FIFO summary, or signal cleanup assertion
fails until the full integration is complete.

- [ ] **Step 7: Complete join, deadline, cleanup, and output behavior**

When `operation-acquire` waited behind another owner, print:

```text
open-codex-project-in-intellij: joined existing worktree open; wait=<seconds>s
```

After launch ownership is released, print:

```text
open-codex-project-in-intellij: queued launch completed; max-position=<n>; queue-wait=<seconds>s; launch=<seconds>s
```

Map coordinator phases to concise stderr and distinct exits while retaining
the existing public command syntax. Change the readiness timeout message to
contain `readiness-deadline`. Ensure `EXIT INT TERM HUP` cleanup calls only the
coordinator and never removes state directly.

- [ ] **Step 8: Run opener tests and commit**

Run:

```bash
python3 -m unittest tests.python.test_open_codex_project_in_intellij -v
```

Expected: all existing bootstrap, trust, ownership, reaper, and new concurrency
tests pass.

Commit:

```bash
git add -- bin/open-codex-project-in-intellij tests/python/test_open_codex_project_in_intellij.py
git commit -m "fix: queue concurrent IntelliJ project opens"
```

---

### Task 3: Align doctor and broker deadlines and diagnostics

**Files:**
- Modify: `bin/serena-project-doctor`
- Modify: `bin/serena-worktree-broker`
- Modify: `tests/python/test_serena_project_doctor.py`
- Modify: `tests/python/test_serena_worktree_broker.py`

**Interfaces:**
- Consumes: opener environment `INTELLIJ_OPENER_OPERATION_TIMEOUT`, `INTELLIJ_OPENER_QUEUE_TIMEOUT`, `INTELLIJ_SERENA_READY_TIMEOUT`, and bootstrap timeout.
- Produces: `minimum_intellij_opener_timeout() -> float` in each executable; queue-aware recovery status and sanitized history/action results.

- [ ] **Step 1: Write failing timeout-contract tests**

Replace assertions based on `INTELLIJ_OPENER_LOCK_TIMEOUT` with explicit values:

```python
with mock.patch.object(doctor, "OPENER_BOOTSTRAP_TIMEOUT_SECONDS", 10), \
     mock.patch.object(doctor, "OPENER_OPERATION_TIMEOUT_SECONDS", 20), \
     mock.patch.object(doctor, "OPENER_QUEUE_TIMEOUT_SECONDS", 30), \
     mock.patch.object(doctor, "OPENER_READY_TIMEOUT_SECONDS", 40):
    self.assertEqual(120, doctor.minimum_intellij_opener_timeout())
```

The expected `120` is `10 bootstrap + 10 bootstrap termination grace + 20
operation + 30 queue + 40 readiness + 10 caller grace`. Add the equivalent
broker test and assert each subprocess uses at least the computed value.

- [ ] **Step 2: Run focused timeout tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.python.test_serena_project_doctor.SerenaProjectDoctorTests.test_opener_timeout_exceeds_bootstrap_and_ready_timeouts \
  tests.python.test_serena_worktree_broker.SerenaWorktreeBrokerTests.test_intellij_launcher_timeout_exceeds_bootstrap_and_ready_timeouts -v
```

Expected: FAIL because both programs still use the removed lock timeout and do
not expose the calculation function.

- [ ] **Step 3: Implement shared-value timeout calculations**

In both programs define:

```python
OPENER_OPERATION_TIMEOUT_SECONDS = float(
    os.environ.get("INTELLIJ_OPENER_OPERATION_TIMEOUT", "300")
)
OPENER_QUEUE_TIMEOUT_SECONDS = float(
    os.environ.get("INTELLIJ_OPENER_QUEUE_TIMEOUT", "300")
)

def minimum_intellij_opener_timeout() -> float:
    return (
        OPENER_BOOTSTRAP_TIMEOUT_SECONDS
        + 10
        + OPENER_OPERATION_TIMEOUT_SECONDS
        + OPENER_QUEUE_TIMEOUT_SECONDS
        + OPENER_READY_TIMEOUT_SECONDS
        + 10
    )
```

Use `max(configured_timeout, minimum_intellij_opener_timeout())` at the opener
subprocess boundary. Remove `OPENER_LOCK_TIMEOUT_SECONDS` and the old constant
expression.

- [ ] **Step 4: Run timeout tests and verify GREEN**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 5: Write failing doctor and broker classification tests**

Parameterize opener failures containing sanitized final packets:

```python
cases = (
    ("operation-deadline", "operation-deadline"),
    ("queue-deadline", "queue-deadline"),
    ("readiness-deadline", "readiness-deadline"),
)
```

For doctor, assert `report["recovery"]["status"]` equals the expected phase and
`action_result` contains only `phase`, `maximum_queue_position`,
`queue_wait_seconds`, and `launch_seconds`. For broker, assert the raised error
contains the phase but excludes fixture roots, PIDs, and other request IDs.

- [ ] **Step 6: Run classification tests and verify RED**

Run:

```bash
python3 -m unittest tests.python.test_serena_project_doctor tests.python.test_serena_worktree_broker -v
```

Expected: the new cases fail because current code flattens nonzero opener exits
to `opener-failed` or `failed to open IntelliJ project`.

- [ ] **Step 7: Parse bounded opener summaries and preserve terminal phase**

Add a private parser in each executable:

```python
ALLOWED_OPENER_PHASES = {
    "operation-deadline", "queue-deadline", "readiness-deadline",
    "trust-failed", "launch-failed", "state-invalid",
}

def _opener_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Return only the final validated opener packet and whitelisted metrics."""
```

Have the opener emit its final machine packet on stdout prefixed by
`WORKSPACE_HARBOR_RESULT ` so human text cannot be mistaken for JSON. Parse the
last prefixed line, validate types and numeric bounds, and fall back to the
existing bounded text diagnostic when absent. Doctor uses the validated phase
as recovery status and records whitelisted metrics in `action_result`. Broker
includes only the validated phase and caller metrics in its exception.

- [ ] **Step 8: Run doctor and broker tests and commit**

Run the command from Step 6.

Expected: all doctor and broker tests pass.

Commit:

```bash
git add -- bin/serena-project-doctor bin/serena-worktree-broker \
  tests/python/test_serena_project_doctor.py tests/python/test_serena_worktree_broker.py
git commit -m "fix: preserve queued opener recovery outcomes"
```

---

### Task 4: Deploy, document, and verify the compatible command set

**Files:**
- Modify: `bin/deploy-workspace-harbor`
- Modify: `tests/python/test_deploy_workspace_harbor.py`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-12-intellij-only-cutover-design.md`

**Interfaces:**
- Consumes: `bin/workspace_harbor_opener_queue.py` and the updated command set from Tasks 1–3.
- Produces: atomic local deployment including the new module; documented queue configuration and diagnostics.

- [ ] **Step 1: Write a failing deployment-manifest test**

Add `workspace_harbor_opener_queue.py` to the test fixture's expected modules
and explicitly assert dry-run output and installed mode:

```python
self.assertIn("workspace_harbor_opener_queue.py", result.stdout)
self.assertEqual(
    0o644,
    (destination / "workspace_harbor_opener_queue.py").stat().st_mode & 0o777,
)
```

- [ ] **Step 2: Run the deploy test and verify RED**

Run:

```bash
python3 -m unittest tests.python.test_deploy_workspace_harbor -v
```

Expected: FAIL because the deployer's `MODULES` tuple omits the coordinator.

- [ ] **Step 3: Add the coordinator to atomic deployment**

Add exactly:

```python
MODULES = (
    "workspace_harbor_ide.py",
    "workspace_harbor_bootstrap.py",
    "workspace_harbor_bridge.py",
    "workspace_harbor_codex.py",
    "workspace_harbor_opener_queue.py",
)
```

Keep the module installed as `0644`; the zsh opener invokes it with `python3`,
so it is not part of `EXECUTABLES`.

- [ ] **Step 4: Run deploy tests and verify GREEN**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 5: Update operator documentation**

In `README.md`, replace the single global-opener-lock description with:

```markdown
The canonical worktree is the single-flight boundary. Requests for one root
join one open operation. Different roots enter a durable FIFO launch queue for
the brief exact-root trust and IntelliJ open request, then wait for indexing
and Serena readiness concurrently. Queueing is normal recovery state; only a
validated deadline or unsafe owner/state condition is a failure.
```

Document defaults and overrides:

```text
INTELLIJ_OPENER_OPERATION_TIMEOUT=300
INTELLIJ_OPENER_QUEUE_TIMEOUT=300
INTELLIJ_SERENA_READY_TIMEOUT=120
```

Document `joined existing worktree open`, `queued launch completed`,
`operation-deadline`, `queue-deadline`, and `readiness-deadline` without
exposing private queue records. State explicitly that queue presence is not a
reaper lease and an unopened queued root is not registered as managed.

In the older IntelliJ cutover spec, mark its Managed opener steps 3–7 as
superseded by `2026-07-16-queued-intellij-opener-design.md`; do not rewrite the
historical document as if the original design never existed.

- [ ] **Step 6: Run the focused and full repository validation**

Run:

```bash
python3 -m unittest \
  tests.python.test_workspace_harbor_opener_queue \
  tests.python.test_open_codex_project_in_intellij \
  tests.python.test_serena_project_doctor \
  tests.python.test_serena_worktree_broker \
  tests.python.test_deploy_workspace_harbor -v
python3 -m unittest discover -s tests/python -p 'test_*.py'
```

Expected: all tests pass; no test contacts a live IDE or installed plugin.

- [ ] **Step 7: Commit documentation and deployment changes**

```bash
git add -- bin/deploy-workspace-harbor tests/python/test_deploy_workspace_harbor.py \
  README.md docs/superpowers/specs/2026-07-12-intellij-only-cutover-design.md
git commit -m "docs: operate queued IntelliJ recovery"
```

- [ ] **Step 8: Review final repository state before deployment**

Run:

```bash
git status --short --branch
git diff bf108fc..HEAD --stat
git diff bf108fc..HEAD --check
git log -5 --oneline --decorate
```

Expected: only the planned opener, coordinator, doctor, broker, tests,
deployment, and documentation files changed; the worktree is clean; diff check
reports no errors.

- [ ] **Step 9: Deploy atomically and run the non-live post-deploy check**

Run:

```bash
python3 bin/deploy-workspace-harbor --dry-run
python3 bin/deploy-workspace-harbor
python3 ~/.codex/bin/workspace_harbor_opener_queue.py self-check
```

Expected: dry run lists the complete compatible set; deployment succeeds with
a timestamped backup; coordinator self-check reports healthy and leaves no
fixture owner or queue entry. Do not invoke the IntelliJ opener during this
post-deploy check.

- [ ] **Step 10: Final installed-source parity and status check**

Run:

```bash
cmp bin/workspace_harbor_opener_queue.py ~/.codex/bin/workspace_harbor_opener_queue.py
cmp bin/open-codex-project-in-intellij ~/.codex/bin/open-codex-project-in-intellij
cmp bin/serena-project-doctor ~/.codex/bin/serena-project-doctor
cmp bin/serena-worktree-broker ~/.codex/bin/serena-worktree-broker
git status --short --branch
```

Expected: all `cmp` commands exit `0`; Git remains clean.
