# Developer Workspace Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce that Codex coding projects are created and mutated only below `/Users/Monsky/Developer/Codex` or `/Users/Monsky/.codex/src`, while preserving read-only inspection, ordinary document workflows, and deliberate migrations out of Documents.

**Architecture:** A dependency-free Python policy module classifies real-shaped Codex `PreToolUse` events. A thin executable emits the supported hook decision JSON and owns idempotent hook installation; the existing atomic Workspace Harbor deployer installs both artifacts before the global hook is enabled.

**Tech Stack:** Python 3.9+ standard library, Codex JSON hooks, `unittest`, existing Workspace Harbor atomic deployer.

## Global Constraints

- Protected legacy root: `/Users/Monsky/Documents`.
- Approved coding roots: `/Users/Monsky/Developer/Codex` and `/Users/Monsky/.codex/src`.
- Never silently rewrite a requested path.
- Permit read-only inspection and a copy or move from Documents into Developer/Codex.
- Do not block ordinary document, spreadsheet, presentation, image, or PDF tools.
- Use component-aware normalized paths; do not match lookalike sibling names.
- Emit stable denial token `documents-project-write-blocked` with no user-file content.
- Preserve the existing Serena hooks and never edit Codex's private saved-project database.
- Deployment and hook installation must be atomic, backed up, and idempotent.

---

### Task 1: Pure workspace policy classifier

**Files:**
- Create: `bin/codex_developer_workspace_guard.py`
- Create: `tests/python/test_codex_developer_workspace_guard.py`

**Interfaces:**
- Produces: `Decision(allowed: bool, reason: str)`, `classify_event(event: Mapping[str, object], home: Path | None = None) -> Decision`, and `decision_payload(decision: Decision) -> dict[str, object]`.
- Consumes: Codex hook dictionaries containing `tool_name`/`toolName`, `tool_input`/`toolInput`, and `cwd` or a tool-level `workdir`.

- [ ] **Step 1: Add failing path and command policy tests**

```python
class WorkspaceGuardPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path("/Users/Monsky")

    def classify(self, tool_name: str, tool_input: dict[str, object], cwd: str) -> guard.Decision:
        return guard.classify_event(
            {"session_id": "test", "tool_name": tool_name, "tool_input": tool_input, "cwd": cwd},
            home=self.home,
        )

    def test_denies_git_init_below_documents(self) -> None:
        decision = self.classify("Bash", {"cmd": "git init", "workdir": "/Users/Monsky/Documents/new-app"}, "/Users/Monsky")
        self.assertFalse(decision.allowed)
        self.assertEqual("documents-project-write-blocked", decision.reason)

    def test_allows_read_only_inspection_below_documents(self) -> None:
        decision = self.classify("Bash", {"cmd": "git status --short --branch", "workdir": "/Users/Monsky/Documents/old-app"}, "/Users/Monsky")
        self.assertTrue(decision.allowed)

    def test_allows_migration_into_developer(self) -> None:
        decision = self.classify("Bash", {"cmd": "mv '/Users/Monsky/Documents/old-app' '/Users/Monsky/Developer/Codex/old-app'"}, "/Users/Monsky")
        self.assertTrue(decision.allowed)

    def test_denies_source_patch_below_documents(self) -> None:
        patch = "*** Begin Patch\n*** Add File: /Users/Monsky/Documents/new-app/main.py\n+print('x')\n*** End Patch"
        self.assertFalse(self.classify("apply_patch", {"patch": patch}, "/Users/Monsky").allowed)
```

Add separate cases for `git clone`, `git worktree add`, `npm create`, `npm install`, `uv init`, `cargo new`, source redirection, relative traversal into Documents, mutations under both approved roots, Documents lookalikes, and ordinary office-tool calls.

- [ ] **Step 2: Run the focused suite and verify RED**

Run:

```bash
python3 -m unittest tests.python.test_codex_developer_workspace_guard -v
```

Expected: import failure because `bin/codex_developer_workspace_guard.py` does not exist.

- [ ] **Step 3: Implement the minimal pure classifier**

```python
@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


def classify_event(event: Mapping[str, object], home: Path | None = None) -> Decision:
    account_home = (home or Path.home()).resolve()
    protected = account_home / "Documents"
    approved = (account_home / "Developer/Codex", account_home / ".codex/src")
    tool_name = str(event.get("tool_name") or event.get("toolName") or "").strip().lower()
    tool_input = event.get("tool_input") or event.get("toolInput") or {}
    if not isinstance(tool_input, Mapping):
        return Decision(True)
    working_directory = _event_workdir(event, tool_input, account_home)
    if _is_source_write(tool_name, tool_input, working_directory, protected, approved):
        return Decision(False, "documents-project-write-blocked")
    if _is_project_mutation(tool_name, tool_input, working_directory, protected, approved):
        return Decision(False, "documents-project-write-blocked")
    return Decision(True)
```

Use `shlex.split` only for literal command segments. Resolve relative literal paths against the tool workdir; never expand variables, substitutions, or globs. Keep command families and source/project filenames in immutable constants.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
python3 -m unittest tests.python.test_codex_developer_workspace_guard -v
```

Expected: all workspace policy cases pass.

- [ ] **Step 5: Commit the classifier**

```bash
git add -- bin/codex_developer_workspace_guard.py tests/python/test_codex_developer_workspace_guard.py
git commit -m "feat: classify protected workspace writes"
```

---

### Task 2: Hook adapter and idempotent installer

**Files:**
- Create: `bin/codex-developer-workspace-guard`
- Modify: `bin/codex_developer_workspace_guard.py`
- Modify: `tests/python/test_codex_developer_workspace_guard.py`

**Interfaces:**
- Consumes: `classify_event(...)` and `decision_payload(...)` from Task 1.
- Produces: hook mode with no arguments; `install --codex-home PATH`; `install_hook(codex_home: Path) -> InstallResult`; and `remove_documents_project_tables(text: str, protected_root: Path) -> str`.

- [ ] **Step 1: Add failing CLI decision and installation tests**

```python
def test_cli_emits_supported_deny_payload(self) -> None:
    event = {"session_id": "test", "tool_name": "Bash", "tool_input": {"cmd": "git init", "workdir": "/Users/Monsky/Documents/new-app"}}
    result = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(event), text=True, capture_output=True, check=False)
    payload = json.loads(result.stdout)
    self.assertEqual("deny", payload["hookSpecificOutput"]["permissionDecision"])
    self.assertIn("documents-project-write-blocked", payload["hookSpecificOutput"]["permissionDecisionReason"])

def test_install_preserves_serena_hook_and_is_idempotent(self) -> None:
    hooks = self.codex_home / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "serena-hooks remind --client=codex"}]}]}}))
    first = self.run_guard("install", "--codex-home", str(self.codex_home))
    second = self.run_guard("install", "--codex-home", str(self.codex_home))
    self.assertEqual(0, first.returncode)
    self.assertEqual(0, second.returncode)
    installed = json.loads(hooks.read_text())
    self.assertEqual(2, len(installed["hooks"]["PreToolUse"]))
```

Also assert private `0600` output, recoverable timestamped backup, atomic replacement, malformed-config refusal without mutation, and an allow payload for a read-only event.

Add a TOML fixture containing one Documents project table, one Developer project
table, comments, and unrelated nested tables. Assert that
`install --codex-home PATH --clean-project-records` removes only the Documents
project table, creates `config.toml.backup-before-workspace-guard-20260718`,
preserves every unrelated byte, and produces TOML accepted by `tomllib.loads`.

- [ ] **Step 2: Run the focused suite and verify RED**

Run:

```bash
python3 -m unittest tests.python.test_codex_developer_workspace_guard -v
```

Expected: CLI tests fail because the executable and installer do not exist.

- [ ] **Step 3: Implement the hook adapter and installer**

```python
HOOK_ENTRY = {
    "hooks": [
        {
            "type": "command",
            "command": "{guard_path}",
        }
    ]
}


def install_hook(codex_home: Path) -> InstallResult:
    hooks_path = codex_home / "hooks.json"
    document = _load_hooks_document(hooks_path)
    entries = document.setdefault("hooks", {}).setdefault("PreToolUse", [])
    desired = {"hooks": [{"type": "command", "command": str(codex_home / "bin/codex-developer-workspace-guard")} ]}
    entries[:] = [entry for entry in entries if not _is_workspace_guard_entry(entry)]
    entries.append(desired)
    return _atomic_json_write_with_backup(hooks_path, document)
```

The executable prepends its own `bin` directory to `sys.path`, reads at most 1 MiB from stdin, rejects malformed JSON with a bounded stderr message and nonzero status, and prints exactly one JSON decision for valid events. The deny reason must tell the agent to migrate or recreate the checkout under `/Users/Monsky/Developer/Codex`.

Implement `remove_documents_project_tables` as a line-preserving TOML table
filter: recognize complete table headers with `tomllib`-compatible quoted keys,
drop a `[projects."..."]` table only when its decoded path is component-wise below
the protected root, and retain all bytes from the next table header onward. Parse
the original and candidate documents with `tomllib.loads` before atomic replacement.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
python3 -m unittest tests.python.test_codex_developer_workspace_guard -v
```

Expected: all classifier, CLI, and installer tests pass.

- [ ] **Step 5: Commit the adapter and installer**

```bash
git add -- bin/codex-developer-workspace-guard bin/codex_developer_workspace_guard.py tests/python/test_codex_developer_workspace_guard.py
git commit -m "feat: enforce developer workspace hook"
```

---

### Task 3: Atomic deployment integration

**Files:**
- Modify: `bin/deploy-workspace-harbor`
- Modify: `tests/python/test_deploy_workspace_harbor.py`

**Interfaces:**
- Consumes: Task 2's executable and Python module.
- Produces: atomic deployment of `codex-developer-workspace-guard` with mode `0755` and `codex_developer_workspace_guard.py` with mode `0644`.

- [ ] **Step 1: Add failing deployment manifest assertions**

```python
EXECUTABLES = (
    "codex-developer-workspace-guard",
    "deploy-workspace-harbor",
    "open-codex-project-in-intellij",
    "intellij-project-trust",
    "intellij-project-reaper",
    "serena-bridge-doctor",
    "serena-codex",
    "serena-project-doctor",
    "serena-worktree-broker",
    "workspace-harbor-bootstrap",
    "workspace-harbor-codex-relauncher",
)
MODULES = (
    "codex_developer_workspace_guard.py",
    "workspace_harbor_ide.py",
    "workspace_harbor_bootstrap.py",
    "workspace_harbor_bridge.py",
    "workspace_harbor_codex.py",
    "workspace_harbor_opener_queue.py",
)

def test_missing_workspace_guard_fails_before_destination_mutation(self) -> None:
    destination = self.codex_home / "bin"
    destination.mkdir(parents=True)
    existing = destination / "serena-codex"
    existing.write_text("keep me\n", encoding="utf-8")
    (self.bin_source / "codex_developer_workspace_guard.py").unlink()
    result = self.run_deploy()
    self.assertEqual(2, result.returncode)
    self.assertEqual("keep me\n", existing.read_text(encoding="utf-8"))
```

- [ ] **Step 2: Run deployment tests and verify RED**

Run:

```bash
python3 -m unittest tests.python.test_deploy_workspace_harbor -v
```

Expected: guard artifact assertions fail because the deployer's manifests omit them.

- [ ] **Step 3: Add the guard artifacts to the deployer manifests**

Add `codex-developer-workspace-guard` to `EXECUTABLES` and `codex_developer_workspace_guard.py` to `MODULES`. Preserve the existing staging, backup, hash verification, and rollback code unchanged.

- [ ] **Step 4: Run deployment and full repository checks**

Run:

```bash
python3 -m unittest tests.python.test_deploy_workspace_harbor tests.python.test_codex_developer_workspace_guard -v
python3 -m unittest discover -s tests/python -v
JAVA_HOME=/Applications/PyCharm.app/Contents/jbr/Contents/Home ./gradlew test buildPlugin verifyPlugin --console=plain
python3 bin/deploy-workspace-harbor --dry-run
git diff --check
```

Expected: all Python tests pass, Gradle reports `BUILD SUCCESSFUL`, dry-run lists both guard artifacts with correct modes and hashes, and diff check is clean.

- [ ] **Step 5: Commit deployment integration**

```bash
git add -- bin/deploy-workspace-harbor tests/python/test_deploy_workspace_harbor.py
git commit -m "chore: deploy developer workspace guard"
```

---

### Task 4: Global rollout and live verification

**Files:**
- Deploy: `/Users/Monsky/.codex/bin/codex-developer-workspace-guard`
- Deploy: `/Users/Monsky/.codex/bin/codex_developer_workspace_guard.py`
- Modify through installer: `/Users/Monsky/.codex/hooks.json`
- Modify with backup: `/Users/Monsky/.codex/config.toml`

**Interfaces:**
- Consumes: reviewed commits from Tasks 1–3.
- Produces: active global guard configuration and zero stale `[projects]` trust records below Documents.

- [ ] **Step 1: Deploy the reviewed artifacts atomically**

Run:

```bash
python3 bin/deploy-workspace-harbor
/Users/Monsky/.codex/bin/codex-developer-workspace-guard install --codex-home /Users/Monsky/.codex --clean-project-records
```

Expected: deployment reports its backup location; installation reports either `installed` or `unchanged` and preserves the Serena hook.

- [ ] **Step 2: Remove stale Documents project trust records with a backup**

Confirm that the Task 2 installer created
`/Users/Monsky/.codex/config.toml.backup-before-workspace-guard-20260718`, removed
only `[projects."/Users/Monsky/Documents..."]` tables, and left the Codex private
saved-project store untouched.

- [ ] **Step 3: Exercise direct allow and deny fixtures**

Run:

```bash
printf '%s' '{"session_id":"live-test","tool_name":"Bash","tool_input":{"cmd":"git status --short --branch","workdir":"/Users/Monsky/Documents/Misc"}}' | /Users/Monsky/.codex/bin/codex-developer-workspace-guard
printf '%s' '{"session_id":"live-test","tool_name":"Bash","tool_input":{"cmd":"git init","workdir":"/Users/Monsky/Documents/new-project"}}' | /Users/Monsky/.codex/bin/codex-developer-workspace-guard
```

Expected: the first response allows; the second denies with `documents-project-write-blocked`.

- [ ] **Step 4: Verify live configuration invariants**

Verify that `hooks.json` is valid JSON, `config.toml` is valid TOML, the Serena hook remains present exactly once, the workspace guard hook remains present exactly once, no `[projects]` table targets Documents, source and deployed guard hashes match, and the old Documents directories remain untouched.

- [ ] **Step 5: Perform final repository review**

Run:

```bash
git status --short --branch
git diff --stat origin/main...HEAD
git diff origin/main...HEAD
git log --oneline origin/main..HEAD
```

Inspect the complete change for secrets, private paths beyond the intended account-local policy, accidental deletion, and unrelated modifications. Report that existing saved projects still require supported Codex UI removal/recreation and that fresh tasks are required to load the new hook configuration.
