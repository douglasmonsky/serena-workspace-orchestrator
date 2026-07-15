# Stable Codex Host Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Serena MCP tools exposed across Codex MCP reloads by resolving broker subprocesses from one live Codex host to a stable owner while preserving thread-lineage ownership whenever task identity exists.

**Architecture:** Extend the broker's owner resolver with a validated Codex-host identity derived from the parent Codex process PID, start time, and executable. Before enforcing root ownership, atomically migrate only verifiable legacy `manual-pid-*` leases created by another broker under that same host. Canonical worktrees remain the service boundary; unrelated concurrent tasks must use separate worktrees when the desktop MCP transport does not provide task identity.

**Tech Stack:** Python 3 standard library, `unittest`, macOS `ps`, existing JSON state lock, existing deployment and doctor scripts, Serena 1.5.3 through `mcp-proxy`.

## Global Constraints

- Explicit `WORKSPACE_HARBOR_OWNER_ID` remains the highest-priority owner.
- Valid `CODEX_THREAD_ID` session lineage remains higher priority than host fallback.
- Host identity must include PID and process start time and must validate an exact Codex executable plus `app-server` or `exec` command token.
- Unknown, reused, stale, cross-host, mixed-owner, and malformed lease state fails closed.
- Legacy migration occurs only inside the broker's existing locked state transaction.
- Different canonical worktrees always receive separate Serena services.
- Unrelated concurrent desktop tasks must use separate worktrees because current MCP calls do not carry task identity.
- Do not kill IntelliJ, Serena, or broker processes broadly and do not edit broker state by hand.

---

### Task 1: Stable Codex-host owner resolution

**Files:**
- Modify: `bin/serena-worktree-broker:10-25,80-95,160-180,315-335`
- Test: `tests/python/test_serena_worktree_broker.py:115-235`

**Interfaces:**
- Consumes: `_process_details(pid: int) -> tuple[str, str] | None`, `OwnerResolution`.
- Produces: `CodexHostIdentity`, `_parent_pid(pid: int) -> int | None`, `_codex_host_identity(child_pid: int) -> CodexHostIdentity | None`, and owner source `codex-host`.

- [x] **Step 1: Write failing tests for stable and isolated host identities**

Add focused tests that patch process inspection rather than spawning processes:

```python
def test_codex_host_owner_is_stable_across_broker_subprocesses(self) -> None:
    process_rows = {
        101: ("broker-a", "python /Users/Monsky/.codex/bin/serena-worktree-broker connect"),
        102: ("broker-b", "python /Users/Monsky/.codex/bin/serena-worktree-broker connect"),
        50: ("host-start", "/Applications/ChatGPT.app/Contents/Resources/codex -c features.code_mode_host=true app-server"),
    }

    with mock.patch.object(broker, "_parent_pid", side_effect=lambda pid: 50), mock.patch.object(
        broker, "_process_details", side_effect=lambda pid: process_rows.get(pid)
    ):
        first = broker._codex_host_identity(101)
        second = broker._codex_host_identity(102)

    self.assertIsNotNone(first)
    self.assertEqual(first, second)
    self.assertTrue(first.owner_id.startswith("codex-host-"))

def test_codex_host_owner_separates_restarted_hosts(self) -> None:
    with mock.patch.object(broker, "_parent_pid", return_value=50), mock.patch.object(
        broker,
        "_process_details",
        side_effect=[("first-start", "/Applications/ChatGPT.app/Contents/Resources/codex app-server"),
                     ("second-start", "/Applications/ChatGPT.app/Contents/Resources/codex app-server")],
    ):
        first = broker._codex_host_identity(101)
        second = broker._codex_host_identity(102)

    self.assertNotEqual(first.owner_id, second.owner_id)

def test_unrecognized_parent_uses_process_fallback(self) -> None:
    with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
        broker, "_parent_pid", return_value=50
    ), mock.patch.object(
        broker, "_process_details", return_value=("host-start", "/usr/bin/python codex-helper.py")
    ):
        resolution = broker._owner_resolution()

    self.assertEqual(f"manual-pid-{os.getpid()}", resolution.owner_id)
    self.assertEqual("process-fallback", resolution.source)
```

- [x] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python -m unittest discover -s tests/python -p 'test_serena_worktree_broker.py' -k codex_host_owner
python -m unittest discover -s tests/python -p 'test_serena_worktree_broker.py' -k unrecognized_parent
```

Expected: FAIL because `CodexHostIdentity`, `_parent_pid`, and `_codex_host_identity` do not exist and the current resolver returns `manual-pid-*`.

- [x] **Step 3: Implement validated host resolution**

Add `shlex`, the frozen identity type, exact token validation, and opaque owner hashing:

```python
import shlex
import shutil


@dataclass(frozen=True)
class CodexHostIdentity:
    pid: int
    process_started: str
    owner_id: str


def _parent_pid(pid: int) -> int | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "ppid="],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        return int(result.stdout.strip()) if result.returncode == 0 else None
    except ValueError:
        return None


def _codex_command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _trusted_codex_executables() -> set[Path]:
    candidates = {
        Path("/Applications/ChatGPT.app/Contents/Resources/codex").resolve()
    }
    command = shutil.which("codex")
    if command:
        candidates.add(Path(command).resolve())
    return candidates


def _codex_host_identity(child_pid: int) -> CodexHostIdentity | None:
    parent_pid = _parent_pid(child_pid)
    if parent_pid is None:
        return None
    details = _process_details(parent_pid)
    if details is None:
        return None
    process_started, command = details
    tokens = _codex_command_tokens(command)
    if not tokens:
        return None
    executable_path = Path(tokens[0]).expanduser().resolve()
    if executable_path not in _trusted_codex_executables():
        return None
    if not ({"app-server", "exec"} & set(tokens[1:])):
        return None
    digest = hashlib.sha256(
        f"{parent_pid}\0{process_started}\0{executable_path}".encode("utf-8")
    ).hexdigest()[:24]
    return CodexHostIdentity(parent_pid, process_started, f"codex-host-{digest}")
```

Extend `_owner_resolution()` only after explicit and thread resolution:

```python
    host = _codex_host_identity(os.getpid())
    if host is not None:
        return OwnerResolution(None, host.owner_id, "codex-host")
    return OwnerResolution(None, f"manual-pid-{os.getpid()}", "process-fallback")
```

- [x] **Step 4: Run focused and broker tests and verify GREEN**

Run:

```bash
python -m unittest discover -s tests/python -p 'test_serena_worktree_broker.py'
```

Expected: all broker tests PASS, including existing explicit-owner and lineage precedence tests.

- [x] **Step 5: Commit stable host resolution**

```bash
git add -- bin/serena-worktree-broker tests/python/test_serena_worktree_broker.py
git commit -m "fix: stabilize Serena ownership per Codex host"
```

### Task 2: Fail-closed legacy lease migration

**Files:**
- Modify: `bin/serena-worktree-broker:330-375,583-610`
- Test: `tests/python/test_serena_worktree_broker.py:250-330`

**Interfaces:**
- Consumes: `CodexHostIdentity`, `_codex_host_identity`, `_process_details`, `_prune_dead_leases`, `_root_owners`.
- Produces: `_migrate_legacy_host_leases(state: dict[str, Any], project_root: Path, resolution: OwnerResolution) -> bool` and an `_connect` path that resolves once, migrates under lock, then enforces ownership.

- [x] **Step 1: Write failing migration tests**

Use synthetic state and patched process evidence:

```python
def test_same_host_legacy_lease_migrates_atomically(self) -> None:
    root = Path(self.temporary_directory.name) / "project"
    root.mkdir()
    lease = {
        "pid": 101,
        "process_started": "broker-start",
        "owner_id": "manual-pid-101",
    }
    state = {"services": {"service": {"project_root": str(root), "leases": {"old": lease}}}}
    resolution = broker.OwnerResolution(None, "codex-host-shared", "codex-host")
    host = broker.CodexHostIdentity(50, "host-start", "codex-host-shared")

    with mock.patch.object(
        broker,
        "_process_details",
        return_value=("broker-start", f"python {BROKER_PATH} connect"),
    ), mock.patch.object(broker, "_codex_host_identity", return_value=host):
        changed = broker._migrate_legacy_host_leases(state, root, resolution)

    self.assertTrue(changed)
    self.assertEqual("codex-host-shared", lease["owner_id"])

def test_cross_host_or_mixed_legacy_lease_does_not_partially_migrate(self) -> None:
    root = Path(self.temporary_directory.name) / "project"
    root.mkdir()
    legacy = {"pid": 101, "process_started": "broker-start", "owner_id": "manual-pid-101"}
    other = {"pid": os.getpid(), "process_started": broker._process_details(os.getpid())[0], "owner_id": "thread-other"}
    state = {"services": {"service": {"project_root": str(root), "leases": {"old": legacy, "other": other}}}}
    resolution = broker.OwnerResolution(None, "codex-host-current", "codex-host")

    current_host = broker.CodexHostIdentity(50, "host-start", "codex-host-current")
    with mock.patch.object(broker, "_prune_dead_leases"), mock.patch.object(
        broker,
        "_process_details",
        return_value=("broker-start", f"python {BROKER_PATH} connect"),
    ), mock.patch.object(broker, "_codex_host_identity", return_value=current_host):
        changed = broker._migrate_legacy_host_leases(state, root, resolution)

    self.assertFalse(changed)
    self.assertEqual("manual-pid-101", legacy["owner_id"])
    with self.assertRaisesRegex(RuntimeError, "owned by another Codex task"):
        broker._assert_root_owner(state, root, resolution.owner_id)
```

Add the remaining fail-closed evidence table:

```python
def test_invalid_legacy_lease_evidence_fails_closed(self) -> None:
    root = Path(self.temporary_directory.name) / "project"
    root.mkdir()
    expected_host = broker.CodexHostIdentity(50, "host-start", "codex-host-current")
    other_host = broker.CodexHostIdentity(60, "other-start", "codex-host-other")
    cases = [
        (
            "owner-pid-mismatch",
            "manual-pid-102",
            ("broker-start", f"python {BROKER_PATH} connect"),
            expected_host,
        ),
        (
            "process-start-mismatch",
            "manual-pid-101",
            ("reused-start", f"python {BROKER_PATH} connect"),
            expected_host,
        ),
        (
            "unrecognized-command",
            "manual-pid-101",
            ("broker-start", "python /tmp/not-workspace-harbor connect"),
            expected_host,
        ),
        (
            "different-codex-host",
            "manual-pid-101",
            ("broker-start", f"python {BROKER_PATH} connect"),
            other_host,
        ),
    ]
    for label, owner, details, legacy_host in cases:
        with self.subTest(label=label):
            lease = {"pid": 101, "process_started": "broker-start", "owner_id": owner}
            state = {"services": {"service": {"project_root": str(root), "leases": {"old": lease}}}}
            resolution = broker.OwnerResolution(None, "codex-host-current", "codex-host")
            with mock.patch.object(broker, "_prune_dead_leases"), mock.patch.object(
                broker, "_process_details", return_value=details
            ), mock.patch.object(
                broker, "_codex_host_identity", return_value=legacy_host
            ):
                changed = broker._migrate_legacy_host_leases(state, root, resolution)
            self.assertFalse(changed)
            self.assertEqual(owner, lease["owner_id"])
```

- [x] **Step 2: Run migration tests and verify RED**

Run:

```bash
python -m unittest discover -s tests/python -p 'test_serena_worktree_broker.py' -k 'legacy_lease'
```

Expected: FAIL because `_migrate_legacy_host_leases` does not exist.

- [x] **Step 3: Implement all-or-nothing migration**

Add exact broker-command and legacy-owner validation. Validate every candidate before mutating any lease:

```python
LEGACY_PROCESS_OWNER_PATTERN = re.compile(r"^manual-pid-([1-9][0-9]*)$")


def _is_deployed_broker_command(command: str) -> bool:
    tokens = _codex_command_tokens(command)
    expected = Path(__file__).resolve()
    paths = []
    for token in tokens[:3]:
        try:
            paths.append(Path(token).expanduser().resolve())
        except OSError:
            continue
    return expected in paths and "connect" in tokens


def _migrate_legacy_host_leases(
    state: dict[str, Any], project_root: Path, resolution: OwnerResolution
) -> bool:
    if resolution.source != "codex-host":
        return False
    canonical_root = str(project_root.resolve())
    candidates: list[dict[str, Any]] = []
    for record in state.get("services", {}).values():
        if not isinstance(record, dict):
            continue
        try:
            record_root = str(Path(record.get("project_root", "")).resolve())
        except (OSError, TypeError, ValueError):
            return False
        if record_root != canonical_root:
            continue
        _prune_dead_leases(record)
        for lease in record.get("leases", {}).values():
            if not isinstance(lease, dict):
                return False
            owner = lease.get("owner_id")
            if owner == resolution.owner_id:
                continue
            match = LEGACY_PROCESS_OWNER_PATTERN.fullmatch(str(owner))
            if match is None or int(match.group(1)) != lease.get("pid"):
                return False
            details = _process_details(int(lease["pid"]))
            if details is None or details[0] != lease.get("process_started"):
                return False
            if not _is_deployed_broker_command(details[1]):
                return False
            host = _codex_host_identity(int(lease["pid"]))
            if host is None or host.owner_id != resolution.owner_id:
                return False
            candidates.append(lease)
    for lease in candidates:
        lease["owner_id"] = resolution.owner_id
    return bool(candidates)
```

Resolve once in `_connect` and migrate only while holding `_locked_state()`:

```python
    resolution = _owner_resolution()
    owner_id = resolution.owner_id
    ...
    with _locked_state() as state:
        _cleanup_state(state, DEFAULT_IDLE_SECONDS, stop_idle=False)
        _migrate_legacy_host_leases(state, project_root, resolution)
        _assert_root_owner(state, project_root, owner_id)
```

- [x] **Step 4: Run broker and full Python tests and verify GREEN**

Run:

```bash
python -m unittest discover -s tests/python -p 'test_serena_worktree_broker.py'
python -m unittest discover -s tests/python -p 'test_*.py'
```

Expected: broker tests PASS; full suite PASS with only the repository's documented external-Serena skips.

- [x] **Step 5: Commit migration**

```bash
git add -- bin/serena-worktree-broker tests/python/test_serena_worktree_broker.py
git commit -m "fix: migrate same-host Serena broker leases"
```

### Task 3: Documentation, deployment, and live MCP exposure dogfood

**Files:**
- Modify: `README.md:65-88`
- Modify after source validation: `/Users/Monsky/.codex/AGENTS.md` ownership paragraph
- Modify: `docs/superpowers/plans/2026-07-14-stable-codex-host-ownership.md` checkboxes only

**Interfaces:**
- Consumes: deployed `serena-worktree-broker`, `serena-worktree-broker owner --json`, `serena-worktree-broker status --json`, `bin/deploy-workspace-harbor`.
- Produces: documented host fallback, deployed broker, and post-restart evidence that a desktop Codex MCP client exposes Serena without a manual owner override.

- [x] **Step 1: Update source and global ownership guidance**

Replace the absolute “one logical task per worktree” wording with the two supported cases:

```markdown
When Codex supplies `CODEX_THREAD_ID`, Workspace Harbor resolves validated
session lineage to one root task and rejects another task on that worktree.
The desktop MCP launcher currently supplies only its long-lived Codex host
identity. Broker reloads under that host reuse the same service; unrelated
concurrent desktop tasks must use separate canonical worktrees.
```

Keep the explicit override, fail-closed lineage, separate-root, and privacy wording.

- [x] **Step 2: Run documentation and complete source gates**

Run:

```bash
git diff --check
python -m unittest discover -s tests/python -p 'test_*.py'
./gradlew test buildPlugin verifyPlugin
```

Expected: `git diff --check` exits 0, Python tests PASS with documented skips, and Gradle reports `BUILD SUCCESSFUL`.

- [x] **Step 3: Commit documentation**

```bash
git add -- README.md docs/superpowers/plans/2026-07-14-stable-codex-host-ownership.md
git commit -m "docs: explain Codex-host Serena ownership"
```

- [ ] **Step 4: Deploy the validated broker**

Run:

```bash
bin/deploy-workspace-harbor
shasum -a 256 bin/serena-worktree-broker /Users/Monsky/.codex/bin/serena-worktree-broker
```

Expected: deployment creates a timestamped backup and both broker hashes match.

- [ ] **Step 5: Verify live migration and Serena MCP exposure after desktop restart**

Do not use standalone `codex exec` as a substitute for the desktop host. It has
a different validated parent process and must remain isolated. After deploying,
restart Codex desktop once so its MCP launcher loads the new broker, then open a
normal task on an already-configured repository without
`WORKSPACE_HARBOR_OWNER_ID`. Confirm Serena is listed and call
`initial_instructions` once. Capture status before and after:

```bash
/Users/Monsky/.codex/bin/serena-worktree-broker status --json
/Users/Monsky/.codex/bin/serena-worktree-broker status --json
```

Expected: the desktop task exposes Serena, `initial_instructions` succeeds, and
the canonical root retains one healthy persistent service rather than spawning
a duplicate. A legacy lease changes owner only during a same-host reload that
proves the full process lineage; after a full desktop restart, older leases are
dead and are pruned instead of migrated.

- [ ] **Step 6: Run final repository and installation review**

Run:

```bash
git status --short --branch
git diff --stat HEAD~3..HEAD
git diff --check HEAD~3..HEAD
git log -4 --oneline
/Users/Monsky/.codex/bin/serena-worktree-broker owner --json
/Users/Monsky/.codex/bin/serena-worktree-broker status --json
/Users/Monsky/.codex/bin/codex-agent-audit
```

Expected: source worktree clean except any intentional plan checkbox update, three focused implementation commits present, agent audit has zero errors, and broker output contains no prompts or session content.
