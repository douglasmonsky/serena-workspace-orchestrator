# Serena Bridge Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a privacy-safe bridge doctor that distinguishes healthy Serena semantics from missing Codex MCP tool exposure, repairs Workspace Harbor-owned failures, and can resume the sole active task after one guarded Codex restart.

**Architecture:** Instrument `serena-worktree-broker` with bounded operational events, then drive a synthetic MCP `initialize`/`tools/list` exchange through the deployed broker. Keep bridge diagnosis in a new CLI and isolate Codex process, exclusive-task, heartbeat, and checkpoint validation in a focused module plus detached relauncher. Restart remains disabled until live dogfood proves that the resumed existing task receives Serena tools.

**Tech Stack:** Python 3.9+ standard library, JSON-RPC over MCP stdio, existing Workspace Harbor broker/project doctor, Codex app thread and automation tools, macOS app lifecycle commands, `unittest`, existing atomic deployer.

## Global Constraints

- Keep IntelliJ/plugin health, broker/MCP handshake health, and Codex task tool exposure as separate claims.
- Never record prompts, MCP parameters or response bodies, tool descriptions, source content, environment values, automation prompts beyond the incident token, or raw session records.
- Use only exact canonical Git roots already accepted by Workspace Harbor.
- Permit one broker repair, one lower-layer recovery, one handshake retry after meaningful repair, and at most one Codex restart per incident.
- Never restart IntelliJ for a task-host-only failure.
- Never restart Codex when another task or host may be active, evidence is unavailable or stale, or the heartbeat cannot resume the same task.
- Restart Codex gracefully only; never send KILL.
- Do not edit Codex session JSONL, task databases, or automation files directly.
- Preserve Python 3.9 compatibility; do not add a TOML or MCP dependency.
- Keep automatic Codex restart disabled until controlled live dogfood succeeds.

## File map

- Create `bin/workspace_harbor_bridge.py`: bridge journal, Codex MCP-config inspection, bounded stdio handshake, incident store, and bridge result types.
- Create `bin/serena-bridge-doctor`: `status`, `recover`, `attest-restart`, `prepare-restart`, and `resume` CLI orchestration.
- Create `bin/workspace_harbor_codex.py`: Codex process identity, exclusive-task and heartbeat attestations, restart checkpoint validation, and configuration.
- Create `bin/workspace-harbor-codex-relauncher`: detached exact-process graceful quit, app relaunch, and task deep-link helper.
- Modify `bin/serena-worktree-broker`: emit bridge-stage events and add exact-root stale-state repair.
- Modify `bin/deploy-workspace-harbor`: deploy the new commands/modules atomically.
- Create `tests/python/test_workspace_harbor_bridge.py`.
- Create `tests/python/test_serena_bridge_doctor.py`.
- Create `tests/python/test_workspace_harbor_codex.py`.
- Create `tests/python/test_workspace_harbor_codex_relauncher.py`.
- Modify `tests/python/test_serena_worktree_broker.py` and `tests/python/test_deploy_workspace_harbor.py`.
- Modify `README.md`, then update the installed global `/Users/Monsky/.codex/AGENTS.md` and `/Users/Monsky/.codex/rules/default.rules` without staging those global files.

---

### Task 1: Privacy-Safe Broker Connection Journal and Exact-Root Repair

**Files:**
- Create: `bin/workspace_harbor_bridge.py`
- Modify: `bin/serena-worktree-broker`
- Create: `tests/python/test_workspace_harbor_bridge.py`
- Modify: `tests/python/test_serena_worktree_broker.py`

**Interfaces:**
- Produces `BridgeEvent(attempt_id, timestamp, root_digest, service_key, owner_source, stage, outcome, reason, duration_ms)`.
- Produces `BridgeJournal.append(event: BridgeEvent) -> bool`; journal failure returns `False` and never raises into the MCP connection path.
- Produces `serena-worktree-broker repair-root ROOT --json` returning `unchanged`, `repaired`, or `protected` plus stable reason codes.
- Later tasks consume `BridgeJournal.recent(root: Path, limit: int = 20) -> list[dict[str, object]]`.

- [ ] **Step 1: Write failing journal redaction, rotation, and failure-isolation tests**

Create `tests/python/test_workspace_harbor_bridge.py` with an import loader matching the existing bin-module tests and these concrete tests:

```python
class BridgeJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "bridge-state"
        self.journal = bridge.BridgeJournal(self.state, max_bytes=512, backups=2)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def event(self, stage: str) -> bridge.BridgeEvent:
        return bridge.BridgeEvent(
            attempt_id="a" * 32,
            timestamp="2026-07-15T20:00:00+00:00",
            root_digest="b" * 24,
            service_key="c" * 20,
            owner_source="codex-host",
            stage=stage,
            outcome="ok",
            reason=None,
            duration_ms=1,
        )

    def test_append_stores_only_allowlisted_fields(self) -> None:
        event = bridge.BridgeEvent(
            attempt_id="a" * 32,
            timestamp="2026-07-15T20:00:00+00:00",
            root_digest="b" * 24,
            service_key="c" * 20,
            owner_source="codex-host",
            stage="proxy-exit",
            outcome="failed",
            reason="proxy-exit-1",
            duration_ms=41,
        )
        self.assertTrue(self.journal.append(event))
        payload = json.loads(self.journal.path.read_text().splitlines()[0])
        self.assertEqual(dataclasses.asdict(event), payload)
        rendered = json.dumps(payload)
        for forbidden in ("prompt", "arguments", "environment", "source_text"):
            self.assertNotIn(forbidden, rendered)

    def test_append_failure_does_not_raise(self) -> None:
        self.journal.state_dir.write_text("not a directory", encoding="utf-8")
        self.assertFalse(self.journal.append(self.event("project-resolution")))
```

Add `test_rotation_keeps_current_plus_two_backups`, `test_recent_filters_by_root_digest`, and `test_reason_rejects_newlines_and_values_over_96_characters`. The rotation fixture appends fixed-size events until `journal.jsonl`, `.1`, and `.2` exist and asserts no `.3` exists.

- [ ] **Step 2: Run focused tests to verify RED**

Run:

```bash
python3 -m unittest -v tests.python.test_workspace_harbor_bridge.BridgeJournalTests
```

Expected: FAIL because `workspace_harbor_bridge.py`, `BridgeEvent`, and `BridgeJournal` do not exist.

- [ ] **Step 3: Implement the bounded journal**

Create the module with these exact public contracts and constants:

```python
BRIDGE_SCHEMA_VERSION = 1
DEFAULT_JOURNAL_MAX_BYTES = 524_288
DEFAULT_JOURNAL_BACKUPS = 4
MAX_REASON_LENGTH = 96

@dataclass(frozen=True)
class BridgeEvent:
    attempt_id: str
    timestamp: str
    root_digest: str | None
    service_key: str | None
    owner_source: str | None
    stage: str
    outcome: str
    reason: str | None
    duration_ms: int
    schema_version: int = BRIDGE_SCHEMA_VERSION

class BridgeJournal:
    def __init__(self, state_dir: Path, *, max_bytes: int = DEFAULT_JOURNAL_MAX_BYTES,
                 backups: int = DEFAULT_JOURNAL_BACKUPS) -> None:
        self.state_dir = state_dir
        self.path = state_dir / "journal.jsonl"
        self.lock_path = state_dir / "journal.lock"
        self.max_bytes = max_bytes
        self.backups = backups

    def append(self, event: BridgeEvent) -> bool:
        try:
            _validate_event(event)
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                self._rotate_if_needed(len(_event_line(event)))
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(_event_line(event))
            return True
        except (OSError, TypeError, ValueError):
            return False
```

Use `hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]` for root digests. Serialize with sorted compact JSON and a trailing newline. Reject non-hex attempt IDs, unknown stages/outcomes, embedded newlines, and non-allowlisted owner-source values before writing.

- [ ] **Step 4: Write failing broker-stage and repair-root tests**

Add tests that patch `BridgeJournal.append` and assert ordered stage events for a successful `_connect`: `project-resolution`, `ownership`, `service-reused|service-started`, `lease-inserted`, `proxy-started`, `proxy-exit`, `lease-cleanup`. Add separate tests for project-resolution failure, ownership conflict, proxy nonzero exit, and journal append returning false while `_connect` still returns the proxy result.

Add exact-root repair tests:

```python
def test_repair_root_removes_only_dead_exact_root_record(self) -> None:
    root = Path(self.temporary_directory.name) / "target"; root.mkdir()
    other = Path(self.temporary_directory.name) / "other"; other.mkdir()
    state = {"services": {
        "target": {"project_root": str(root), "leases": {}, "pid": 101, "port": 24320},
        "other": {"project_root": str(other), "leases": {}, "pid": 202, "port": 24321},
    }}
    with mock.patch.object(broker, "_process_is_owned", return_value=False):
        result = broker._repair_root_state(state, root)
    self.assertEqual("repaired", result["status"])
    self.assertNotIn("target", state["services"])
    self.assertIn("other", state["services"])

def test_repair_root_protects_live_lease(self) -> None:
    root = Path(self.temporary_directory.name) / "target"; root.mkdir()
    state = {"services": {"target": {
        "project_root": str(root),
        "leases": {"live": {"pid": os.getpid(), "process_started": "fixture"}},
        "pid": 101,
        "port": 24320,
    }}}
    with mock.patch.object(broker, "_prune_dead_leases"):
        result = broker._repair_root_state(state, root)
    self.assertEqual({"status": "protected", "reason": "live-lease"}, result)
```

Also cover an unhealthy exact-root service with no leases being stopped only after `_stop_owned_service` validates it, stop failure returning `protected`, and a healthy empty service returning `unchanged`.

- [ ] **Step 5: Run broker tests to verify RED**

Run:

```bash
python3 -m unittest -v \
  tests.python.test_serena_worktree_broker.SerenaWorktreeBrokerTests.test_repair_root_removes_only_dead_exact_root_record \
  tests.python.test_serena_worktree_broker.SerenaWorktreeBrokerTests.test_repair_root_protects_live_lease
```

Expected: FAIL because journal instrumentation and `repair-root` do not exist.

- [ ] **Step 6: Instrument `_connect` and implement exact-root repair**

Instantiate one attempt at `_connect` entry and emit events through a helper that cannot raise:

```python
def _bridge_event(attempt_id: str, started: float, stage: str, outcome: str,
                  *, root: Path | None = None, service_key: str | None = None,
                  owner_source: str | None = None, reason: str | None = None) -> None:
    BRIDGE_JOURNAL.append(BridgeEvent(
        attempt_id=attempt_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        root_digest=root_digest(root) if root is not None else None,
        service_key=service_key,
        owner_source=owner_source,
        stage=stage,
        outcome=outcome,
        reason=reason,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
    ))
```

Catch and classify only the existing broker exceptions; re-raise them after recording. Do not place raw exception strings in `reason`. Add `_repair_root_state(root)` and the parser command:

```python
repair = subparsers.add_parser("repair-root", help="repair stale broker-owned state for one exact root")
repair.add_argument("root")
repair.add_argument("--json", action="store_true")
repair.set_defaults(handler=_repair_root)
```

Require `Path(root).resolve()` to be a directory and exact recorded project root. Implement `_repair_root_state(state, root)` as the testable pure state mutation boundary; the CLI handler acquires `_locked_state()` once and passes that mapping in. Prune dead leases, protect live leases, retain healthy services, and remove only dead or positively stopped exact-root records.

- [ ] **Step 7: Verify GREEN and commit Task 1**

Run:

```bash
python3 -m unittest -v tests.python.test_workspace_harbor_bridge tests.python.test_serena_worktree_broker
python3 -m py_compile bin/workspace_harbor_bridge.py bin/serena-worktree-broker
git diff --check
```

Expected: all focused tests PASS and no syntax or whitespace errors.

Commit:

```bash
git add -- bin/workspace_harbor_bridge.py bin/serena-worktree-broker tests/python/test_workspace_harbor_bridge.py tests/python/test_serena_worktree_broker.py
git commit -m "feat: record Serena bridge connection stages"
```

---

### Task 2: Bounded Synthetic MCP Handshake and Codex Configuration Inspection

**Files:**
- Modify: `bin/workspace_harbor_bridge.py`
- Modify: `tests/python/test_workspace_harbor_bridge.py`

**Interfaces:**
- Produces `ConfigCheck(status: str, reason: str, command: str | None)` from `check_codex_serena_config(codex_cli, expected_broker)`.
- Produces `HandshakeResult(status: str, reason: str, initialize_ms: int | None, tools_list_ms: int | None, tool_count: int, expected_tool_found: bool, proxy_exit: int | None)`.
- Produces `run_handshake(root, broker, timeout_seconds=12.0, max_output_bytes=2_097_152) -> HandshakeResult`.

- [ ] **Step 1: Write failing configuration tests**

Use a fixture executable that emits controlled `codex mcp get serena` output. Cover valid, disabled, missing, wrong broker command, wrong backend/context/add-mode args, CLI timeout, and CLI nonzero exit:

```python
def test_config_check_accepts_exact_enabled_broker(self) -> None:
    output = """serena
  enabled: true
  transport: stdio
  command: /fixture/serena-worktree-broker
  args: connect --context=codex --backend=JetBrains --add-mode=query-projects
  env: -
"""
    result = bridge.parse_codex_mcp_get(output, Path("/fixture/serena-worktree-broker"))
    self.assertEqual("healthy", result.status)

def test_config_check_rejects_wrong_backend(self) -> None:
    result = bridge.parse_codex_mcp_get(self.output.replace("JetBrains", "LSP"), self.broker)
    self.assertEqual(("invalid", "wrong-args"), (result.status, result.reason))
```

- [ ] **Step 2: Write failing handshake protocol tests**

Create a fixture broker script that reads three JSONL requests and emits responses. The healthy fixture returns IDs 1 and 2 with one tool named `initial_instructions`. Assert the client sends `initialize`, `notifications/initialized`, and `tools/list` in order and returns `healthy` with `tool_count == 1`.

Add named tests for initialize timeout, initialize error, early EOF, malformed JSON, response over 2 MiB, missing expected Serena tools, `tools/list` timeout, nonzero proxy exit, and process-group cleanup after timeout. Each asserts a stable reason such as `initialize-timeout`, `protocol-error`, `output-limit`, or `expected-tool-missing` rather than raw stderr.

- [ ] **Step 3: Run handshake tests to verify RED**

Run:

```bash
python3 -m unittest -v \
  tests.python.test_workspace_harbor_bridge.ConfigCheckTests \
  tests.python.test_workspace_harbor_bridge.HandshakeTests
```

Expected: FAIL because configuration and handshake contracts do not exist.

- [ ] **Step 4: Implement configuration inspection without a TOML dependency**

Resolve the Codex CLI from `WORKSPACE_HARBOR_CODEX_CLI`, the bundled ChatGPT path, then `PATH`. Run:

```python
completed = subprocess.run(
    [str(codex_cli), "mcp", "get", "serena"],
    capture_output=True,
    text=True,
    timeout=5,
    check=False,
)
```

Parse only the `enabled`, `transport`, `command`, and `args` lines. Require enabled stdio, the exact deployed broker path, and the set of exact arguments `connect`, `--context=codex`, `--backend=JetBrains`, and `--add-mode=query-projects`. Do not retain or return the `env` line.

- [ ] **Step 5: Implement the bounded JSONL MCP client**

Launch exactly:

```python
command = [
    str(broker), "connect", "--project", str(root),
    "--context=codex", "--backend=JetBrains", "--add-mode=query-projects",
]
process = subprocess.Popen(
    command,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=False,
    start_new_session=True,
)
```

Write newline-delimited JSON-RPC messages with IDs 1 and 2. Use `selectors.DefaultSelector` over stdout and stderr, cap combined captured bytes at 2 MiB, reject lines over 1 MiB, and enforce separate initialize and tools-list deadlines inside the total timeout. On timeout or limit, send TERM to the exact diagnostic process group, wait one second, then KILL only that unchanged child group if still alive. Parse only response IDs, error presence, and `result.tools[*].name`; discard descriptions and all other fields immediately.

Accept the handshake only when at least one of `initial_instructions`, `activate_project`, or `get_symbols_overview` appears. Record sanitized handshake events through `BridgeJournal`.

- [ ] **Step 6: Verify GREEN and commit Task 2**

Run:

```bash
python3 -m unittest -v tests.python.test_workspace_harbor_bridge
python3 -m py_compile bin/workspace_harbor_bridge.py
git diff --check
```

Expected: all bridge tests PASS.

Commit:

```bash
git add -- bin/workspace_harbor_bridge.py tests/python/test_workspace_harbor_bridge.py
git commit -m "feat: probe the Serena MCP bridge"
```

---

### Task 3: Bridge Doctor Status, Recovery, and Incident State Machine

**Files:**
- Create: `bin/serena-bridge-doctor`
- Modify: `bin/workspace_harbor_bridge.py`
- Create: `tests/python/test_serena_bridge_doctor.py`
- Modify: `tests/python/test_workspace_harbor_bridge.py`

**Interfaces:**
- Produces `serena-bridge-doctor status ROOT --reported-tools {present,missing,unknown} [--probe] [--json]`.
- Produces `serena-bridge-doctor recover ROOT --reported-tools missing [--json]`.
- Produces `BridgeIncident(id, root, thread_id, created_at, state, restart_attempted, heartbeat_id)` stored atomically under `$CODEX_HOME/state/serena-bridge/incidents/`.
- Later tasks consume `IncidentStore.load(id)`, `transition(id, expected, next_state)`, and `close(id, terminal_state)`.

- [ ] **Step 1: Write failing status-classification tests**

Load the CLI module with `SourceFileLoader`. Patch config, project-doctor, journal, broker status, and handshake results. Assert these exact classifications:

```python
def test_status_keeps_backend_handshake_and_exposure_separate(self) -> None:
    report = doctor.build_status(
        root=self.root,
        reported_tools="missing",
        config=self.healthy_config,
        project={"semantic_health": "healthy"},
        handshake=self.healthy_handshake,
    )
    self.assertEqual("restart-eligible", report["status"])
    self.assertEqual("backend-healthy", report["backend"])
    self.assertEqual("handshake-healthy", report["handshake"])
    self.assertEqual("task-tools-missing", report["task_exposure"])

def test_unknown_tool_report_never_claims_exposure(self) -> None:
    report = doctor.build_status(
        root=self.root,
        reported_tools="unknown",
        config=self.healthy_config,
        project={"semantic_health": "healthy"},
        handshake=self.healthy_handshake,
    )
    self.assertEqual("exposure-unverified", report["status"])
```

Also cover invalid config, missing executable, unhealthy project, failed handshake, present tools with healthy handshake, and contradictory `recover --reported-tools present` returning `invalid-state` without mutation.

- [ ] **Step 2: Write failing bounded-recovery tests**

Test one exact sequence: initial status fails at broker state, `repair-root` returns repaired, project recovery is skipped, handshake succeeds, incident is created, and status becomes `harbor-repaired` with `recheck-task-tools`. A second test starts with lower-layer semantic failure and asserts exactly one `serena-project-doctor --recover ROOT` call before one handshake retry. A third test asserts unchanged failures do not loop. A fourth asserts a healthy handshake plus reported missing tools creates one `restart-eligible` incident and reuses it on an immediate repeated call.

- [ ] **Step 3: Run doctor tests to verify RED**

Run:

```bash
python3 -m unittest -v tests.python.test_serena_bridge_doctor
```

Expected: FAIL because the doctor and incident state machine do not exist.

- [ ] **Step 4: Implement atomic incident storage**

Use schema version 1, UUID incident IDs, `0o700` directories, `0o600` files, `fcntl` locking, temporary sibling files, `fsync`, and `os.replace`. Enforce these transitions:

```python
ALLOWED_TRANSITIONS = {
    "diagnosing": {"restart-eligible", "closed-healthy", "closed-blocked"},
    "restart-eligible": {"restart-prepared", "closed-blocked"},
    "restart-prepared": {"resume-pending", "closed-blocked"},
    "resume-pending": {"closed-healthy", "closed-fresh-task-required"},
}
```

Never store raw doctor output. Store only IDs, canonical root, root digest, current thread ID, timestamps, state, restart boolean, heartbeat ID, and stable reason.

- [ ] **Step 5: Implement the CLI and recovery ladder**

Keep the CLI thin. `status` calls config inspection, `serena-project-doctor --json ROOT`, broker `owner/status`, journal recent, and optional handshake. `recover` requires `reported-tools=missing`, invokes `repair-root` only for stale broker evidence, invokes project recovery only for a lower-layer failure, then performs one handshake. Return JSON with stable keys:

```python
{
    "status": "restart-eligible",
    "reason": "handshake-healthy-task-tools-missing",
    "root": str(root),
    "backend": "backend-healthy",
    "handshake": "handshake-healthy",
    "task_exposure": "task-tools-missing",
    "incident": incident.id,
    "next_action": "prepare-guarded-restart",
}
```

Human output prints the status first and one next action. Bound all subprocesses and map exceptions to stable reasons; do not print captured stderr unless it is already a bounded Workspace Harbor status message.

- [ ] **Step 6: Verify GREEN and commit Task 3**

Run:

```bash
python3 -m unittest -v tests.python.test_serena_bridge_doctor tests.python.test_workspace_harbor_bridge
python3 -m py_compile bin/serena-bridge-doctor bin/workspace_harbor_bridge.py
git diff --check
```

Expected: all doctor and bridge tests PASS.

Commit:

```bash
git add -- bin/serena-bridge-doctor bin/workspace_harbor_bridge.py tests/python/test_serena_bridge_doctor.py tests/python/test_workspace_harbor_bridge.py
git commit -m "feat: diagnose missing Serena task tools"
```

---

### Task 4: Exclusive-Task Attestation, Heartbeat Validation, and Guarded Relaunch

**Files:**
- Create: `bin/workspace_harbor_codex.py`
- Create: `bin/workspace-harbor-codex-relauncher`
- Modify: `bin/serena-bridge-doctor`
- Create: `tests/python/test_workspace_harbor_codex.py`
- Create: `tests/python/test_workspace_harbor_codex_relauncher.py`
- Modify: `tests/python/test_serena_bridge_doctor.py`

**Interfaces:**
- Produces `RestartAttestation` containing only current thread, local host, active thread IDs, unavailable-host count, observation time, heartbeat ID/target/time/incident, and nonce.
- Produces `CodexProcessIdentity(pid, started, executable, bundle_id)` and `find_codex_app_identity() -> CodexProcessIdentity | None`.
- Produces `serena-bridge-doctor attest-restart`, `prepare-restart`, and `resume` from the approved spec.
- Produces `serena-bridge-doctor restart-policy status|enable|disable --json`; `enable` requires a successful dogfood incident.
- Produces detached `workspace-harbor-codex-relauncher CHECKPOINT_PATH`.

- [ ] **Step 1: Write failing exclusive-task and heartbeat attestation tests**

Use fixed UUIDs and times. Assert one current active task and zero unavailable hosts succeeds. Assert second active task, active child, unavailable host, unknown status, current-thread omission, duplicate IDs, mismatched heartbeat target, disabled heartbeat, incident-token mismatch, next run outside 30–180 seconds, and attestations older than 30 seconds fail with stable reasons.

The success test must use only allowlisted data:

```python
attestation = codex.build_restart_attestation(
    current_thread=CURRENT,
    host_id="local",
    active_threads=[CURRENT],
    unavailable_hosts=[],
    observed_at=NOW,
    heartbeat={
        "id": "workspace-harbor-bridge-incident",
        "target_thread_id": CURRENT,
        "enabled": True,
        "next_run": NOW + timedelta(seconds=90),
        "incident": INCIDENT,
    },
)
self.assertEqual((CURRENT,), attestation.active_threads)
self.assertNotIn("prompt", json.dumps(dataclasses.asdict(attestation)))
```

The agent obtains the source values from `codex_app__list_threads`, targeted `codex_app__read_thread` calls when status refinement is needed, and `codex_app__automation_update view`; only the sanitized fields above reach the CLI.

- [ ] **Step 2: Write failing process identity and relaunch tests**

Patch process inspection and subprocess calls. Cover exact `/Applications/ChatGPT.app/Contents/MacOS/ChatGPT`, bundle ID `com.openai.codex`, PID/start-time match, duplicate app processes, PID reuse, changed executable, app already gone, graceful quit success, graceful quit timeout, no KILL call, relaunch failure, readiness timeout, and exact `codex://threads/THREAD_ID` deep link.

Also assert the persisted restart policy defaults to false, cannot be enabled
without a `closed-healthy` dogfood incident, and can always be disabled.

Assert the relauncher uses:

```python
quit_command = [
    "/usr/bin/osascript", "-e",
    'tell application id "com.openai.codex" to quit',
]
launch_command = ["/usr/bin/open", "-b", "com.openai.codex"]
thread_command = ["/usr/bin/open", f"codex://threads/{thread_id}"]
```

and never invokes `kill`, `pkill`, `killall`, or a command containing `-KILL`.

- [ ] **Step 3: Run restart tests to verify RED**

Run:

```bash
python3 -m unittest -v \
  tests.python.test_workspace_harbor_codex \
  tests.python.test_workspace_harbor_codex_relauncher \
  tests.python.test_serena_bridge_doctor
```

Expected: FAIL because restart contracts do not exist.

- [ ] **Step 4: Implement attestation and exact Codex process identity**

Use UUID validation, timezone-aware timestamps, immutable dataclasses, and `secrets.token_hex(16)` nonces. Inspect processes with argv-form `ps -p PID -o lstart= -o command=` and walk the current process ancestry to the one exact ChatGPT executable. Require exactly one matching live app process. Never accept a command merely containing `ChatGPT` or `codex`.

`attest-restart` accepts repeated `--active-thread`, repeated `--unavailable-host`, and explicit heartbeat fields extracted from app tool results. It refuses rather than writing a proof when exclusivity or heartbeat validation fails. The proof is written atomically with mode `0o600` and expires in thirty seconds.

- [ ] **Step 5: Implement checkpoint preparation and detached relauncher**

Store local policy at `$CODEX_HOME/state/serena-bridge/config.json` with schema
version 1 and `automatic_codex_restart=false` by default. `prepare-restart`
requires an incident in `restart-eligible`, matching root/thread, fresh
attestation, heartbeat validation, exact current app identity, and either
`automatic_codex_restart=true` or an explicit `--dogfood` one-attempt marker.
The dogfood marker is stored in the incident and does not change persistent
policy. The command transitions the incident to `restart-prepared`, writes the
checkpoint, and launches:

```python
subprocess.Popen(
    [str(RELAUNCHER), str(checkpoint_path)],
    stdin=subprocess.DEVNULL,
    stdout=log_stream,
    stderr=log_stream,
    start_new_session=True,
    close_fds=True,
)
```

The relauncher waits for the invoking doctor PID to exit, revalidates app PID/start/executable, requests quit through AppleScript, waits up to fifteen seconds, and stops with `codex-restart-incomplete` if the app remains. After exit it runs `open -b com.openai.codex`, waits up to thirty seconds for the exact app identity and control readiness, opens the exact task deep link, and transitions to `resume-pending`. It never sends a signal directly.

- [ ] **Step 6: Implement post-heartbeat resume and one-attempt termination**

`resume` requires the matching thread/root/incident and `resume-pending`, checks that the app process identity differs from the pre-restart identity, reruns the synthetic handshake, and consumes caller-reported tool exposure. Present tools close `closed-healthy`; missing tools close `closed-fresh-task-required`. A second restart request for either terminal state returns `invalid-state/restart-budget-exhausted`.

The resumed agent disables or deletes the heartbeat through `codex_app__automation_update`; the doctor returns the heartbeat ID but does not edit its file.

- [ ] **Step 7: Verify GREEN and commit Task 4**

Run:

```bash
python3 -m unittest -v \
  tests.python.test_workspace_harbor_codex \
  tests.python.test_workspace_harbor_codex_relauncher \
  tests.python.test_serena_bridge_doctor
python3 -m py_compile \
  bin/workspace_harbor_codex.py \
  bin/workspace-harbor-codex-relauncher \
  bin/serena-bridge-doctor
git diff --check
```

Expected: all restart tests PASS and no syntax or whitespace errors.

Commit:

```bash
git add -- bin/workspace_harbor_codex.py bin/workspace-harbor-codex-relauncher bin/serena-bridge-doctor tests/python/test_workspace_harbor_codex.py tests/python/test_workspace_harbor_codex_relauncher.py tests/python/test_serena_bridge_doctor.py
git commit -m "feat: guard and resume Codex bridge recovery"
```

---

### Task 5: Deployment, Documentation, and Global Operating Policy

**Files:**
- Modify: `bin/deploy-workspace-harbor`
- Modify: `tests/python/test_deploy_workspace_harbor.py`
- Modify: `README.md`
- Modify after source validation: `/Users/Monsky/.codex/AGENTS.md`
- Modify after source validation: `/Users/Monsky/.codex/rules/default.rules`

**Interfaces:**
- Deployer installs `serena-bridge-doctor`, `workspace-harbor-codex-relauncher`, `workspace_harbor_bridge.py`, and `workspace_harbor_codex.py` atomically with the existing set.
- Global policy tells agents how to report task tool exposure, invoke bridge recovery, create the app-derived restart attestation and heartbeat, and resume incidents.

- [ ] **Step 1: Write failing deployment tests**

Extend `EXECUTABLES` and `MODULES` in the fixture test and assert dry-run, install, backup, modes, rollback, and hash verification cover all four new files. Add one test that removes `workspace_harbor_codex.py` and expects status 2 before destination mutation.

- [ ] **Step 2: Run deploy tests to verify RED**

Run:

```bash
python3 -m unittest -v tests.python.test_deploy_workspace_harbor
```

Expected: FAIL because the deployer omits the new files.

- [ ] **Step 3: Update the atomic deploy set**

Add `serena-bridge-doctor` and `workspace-harbor-codex-relauncher` to `EXECUTABLES`, and both new `.py` files to `MODULES`. Keep `SERENA_RUNTIME_COMMANDS` unchanged because all new code must pass under Python 3.9. Preserve backup and rollback behavior.

- [ ] **Step 4: Document bridge diagnosis and guarded restart**

Add README command examples:

```bash
serena-bridge-doctor status "$(git rev-parse --show-toplevel)" --reported-tools unknown --probe
serena-bridge-doctor recover "$(git rev-parse --show-toplevel)" --reported-tools missing
serena-bridge-doctor resume "$(git rev-parse --show-toplevel)" --incident INCIDENT --reported-tools present
```

Document the three health boundaries, privacy-safe journal, recovery budget, sole-active-task guard, heartbeat continuation, disabled-by-default restart setting, and `fresh-task-required` terminal status. State that bridge-only failure never authorizes an IntelliJ restart.

- [ ] **Step 5: Add global guidance and narrow execpolicy rules**

Back up both global files. Add a Serena bridge paragraph that requires:

- inspect the current tool inventory before claiming Serena MCP availability;
- run bridge `recover` after backend health and task exposure disagree;
- use app task tools for the exclusive-task and heartbeat attestation;
- refuse restart when any other task is active or status is ambiguous;
- run bridge `resume` first when the heartbeat wakes the task; and
- delete/disable the recovery heartbeat after a terminal incident.

Add narrow allow rules for installed `serena-bridge-doctor status`, `recover`, `attest-restart`, `prepare-restart`, and `resume`. Do not add a direct allow rule for the relauncher; only the guarded doctor starts it.

Validate each intended command with `codex execpolicy check` and prove `workspace-harbor-codex-relauncher` does not match.

- [ ] **Step 6: Verify GREEN, commit Task 5, and deploy**

Run:

```bash
python3 -m unittest -v tests.python.test_deploy_workspace_harbor
bin/deploy-workspace-harbor --dry-run
git diff --check
```

Expected: deploy tests PASS and dry-run lists fourteen installed files.

Commit repository files only:

```bash
git add -- bin/deploy-workspace-harbor tests/python/test_deploy_workspace_harbor.py README.md
git commit -m "docs: operate Serena bridge recovery"
```

Then deploy the reviewed set and install the reviewed global guidance/rules. Do not stage `/Users/Monsky/.codex/AGENTS.md` or `/Users/Monsky/.codex/rules/default.rules`.

---

### Task 6: Full Verification and Controlled Dogfood

**Files:**
- Modify only if dogfood exposes a tested defect: files from Tasks 1–5 and their matching tests.
- Inspect: deployed helpers, global guidance/rules, broker state, bridge journal, and one disposable project fixture.

**Interfaces:**
- Produces evidence for whether `automatic_codex_restart=true` is safe to enable.
- Produces the terminal decision `restart-rehydrates-existing-task` or `fresh-task-required`.

- [ ] **Step 1: Run complete repository verification**

Run:

```bash
python3 -m unittest discover -s tests/python -p 'test_*.py' -v
python3 -m py_compile bin/*
JAVA_HOME="$HOME/Applications/IntelliJ IDEA.app/Contents/jbr/Contents/Home" \
  ./gradlew test buildPlugin verifyPlugin --console=plain
git diff --check
```

Expected: all Python tests PASS, all scripts compile, and Gradle reports `BUILD SUCCESSFUL`.

- [ ] **Step 2: Verify installed/source parity and privacy**

Compare deployed hashes against the deploy dry-run packet, allowing only the existing Serena runtime shebang rewrite. Inspect bridge journal records and incident/checkpoint files for allowlisted keys only. Search committed and installed files for copied thread previews, prompts, tokens, credentials, and absolute private source paths; expect no matches beyond documented command paths.

- [ ] **Step 3: Dogfood status and recovery without restarting**

On a disposable exact-root fixture with IntelliJ/Serena healthy:

1. run `status --reported-tools unknown --probe` and require `exposure-unverified`, healthy backend, and healthy handshake;
2. run `recover --reported-tools missing` and require one `restart-eligible` incident;
3. inspect the journal and require project-resolution through tools-list stages with no response bodies;
4. repeat recovery and require the same incident with no extra lower-layer recovery; and
5. inject one stale exact-root broker fixture and prove `repair-root` leaves unrelated services unchanged.

- [ ] **Step 4: Gate live restart on the app task inventory**

Call `codex_app__list_threads` and targeted `codex_app__read_thread`. If any task besides the dogfood task is active, any host is unavailable, or status is ambiguous, record `codex-restart-refused-active-tasks` and stop the live restart test without treating it as a product failure.

When the dogfood task is the sole active task, create a one-shot heartbeat targeted to it for 90 seconds ahead. View it through `codex_app__automation_update`, extract only the attestation fields, and invoke `attest-restart` followed by `prepare-restart --dogfood`. Persistent automatic restart must remain false during this first attempt.

- [ ] **Step 5: Resume after relaunch and decide enablement**

The heartbeat must wake the same task and first run:

```bash
serena-bridge-doctor resume ROOT --incident INCIDENT --reported-tools present
```

If Serena tools are exposed and resume returns `healthy`, disable/delete the heartbeat, run `serena-bridge-doctor restart-policy enable --incident INCIDENT --json`, and record `restart-rehydrates-existing-task`. If tools remain missing, run resume with `--reported-tools missing`, require `fresh-task-required`, disable/delete the heartbeat, and verify the restart policy remains false. Never attempt a second restart.

- [ ] **Step 6: Correct any dogfood defect with one TDD cycle**

For each defect, add one failing regression test to the matching test module, run it to verify RED, make the smallest production fix, rerun the focused test to verify GREEN, then rerun Step 1. Do not patch around a failed safety guard merely to make dogfood proceed.

- [ ] **Step 7: Final review and publish-ready handoff**

Inspect `git status --short --branch`, `git diff --stat`, all commits since `b490624`, deployed/source hashes, global file backups, and staged files. Confirm no secret, prompt, automation content, raw session record, or private source data entered Git. Report commit hashes, test totals, Gradle result, non-restart dogfood result, restart dogfood result or safe refusal reason, restart enablement state, and any remaining Codex app-server compatibility risk.
