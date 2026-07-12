# Persistent Bootstrap and Serena Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare each Workspace Harbor worktree once from an explicit or deterministic dependency plan, cache the successful result, and resolve Serena language/tracking onboarding without repeated prompts.

**Architecture:** A focused Python module owns repository evidence, immutable plans, local decisions, fingerprints, locking, execution, and JSON-safe results; a thin CLI exposes `status`, `run`, and `decide`. The Serena doctor consumes its read-only APIs, while the IntelliJ opener invokes the idempotent runner before readiness and the broker only reports cached bootstrap status. A source-controlled deployment helper installs the reviewed command set atomically.

**Tech Stack:** Python 3.9+, PyYAML, `fcntl`, `hashlib`, `subprocess`, zsh, Git, `unittest`, existing `codex-task` compact task runner.

**Execution note:** After Task 2 exposed excessive serial handoff and review
overhead, the remaining tightly coupled slices are executed by the primary
agent with focused TDD. Subagents and delegation capsules remain optional for
independent work or one comprehensive review; they are not required per
micro-fix.

## Global Constraints

- Run only a conventional or configured Codex task, a locally approved argv-form custom command, or a versioned built-in recipe with unambiguous manifest/lock evidence.
- Never parse arbitrary prose into executable commands and never run every package manager found below a repository root.
- Tracked repository configuration describes custom commands but never authorizes their execution.
- Keep normal doctor status read-only; only `workspace-harbor-bootstrap run` and `serena-project-doctor --bootstrap` may execute setup.
- Keep dependency execution out of the Serena MCP connection path.
- Store repository decisions by canonical Git common directory and successful setup records by canonical worktree root.
- Write private state with directory mode `0700` and record/lock mode `0600`; update success records atomically only after verified success.
- Do not print or persist environment variables, credentials, private dependency URLs, or raw installer output.
- Do not stage, commit, revert, or rewrite repository files as part of onboarding decisions.
- Preserve Python 3.9 compatibility and the existing fail-closed IntelliJ/Serena lifecycle behavior.

---

### Task 1: Bootstrap Evidence, Configuration, and Immutable Plans

**Files:**
- Create: `bin/workspace_harbor_bootstrap.py`
- Create: `tests/python/test_workspace_harbor_bootstrap.py`

**Interfaces:**
- Produces immutable `BootstrapPlan(plan_id, source, ecosystem, cwd, argv, inputs, markers)`.
- Produces `resolve_root(value: str | Path | None) -> Path`, `load_policy(root: Path) -> dict[str, object]`, `plan_repository(root: Path) -> dict[str, object]`, `language_evidence(root: Path, language: str) -> str`, and `repository_identity(root: Path) -> str`.
- `plan_repository` returns JSON-safe keys `status`, `root`, `plans`, `decisions`, `policy_source`, and `recipe_version` without executing commands or changing files.

- [ ] **Step 1: Write failing recipe and precedence tests**

Create fixtures in `test_workspace_harbor_bootstrap.py` and add exact assertions:

```python
def test_root_npm_and_uv_locks_select_two_deterministic_plans(self):
    self.write("package.json", '{"name":"fixture"}\n')
    self.write("package-lock.json", "{}\n")
    self.write("pyproject.toml", "[project]\nname='fixture'\n")
    self.write("uv.lock", "version = 1\n")
    result = bootstrap.plan_repository(self.root)
    self.assertEqual("ready", result["status"])
    self.assertEqual(
        [("npm", ["npm", "ci"]), ("uv", ["uv", "sync", "--frozen"])],
        [(item["ecosystem"], item["argv"]) for item in result["plans"]],
    )

def test_conflicting_javascript_locks_require_decision_and_run_nothing(self):
    self.write("package.json", "{}\n")
    self.write("package-lock.json", "{}\n")
    self.write("pnpm-lock.yaml", "lockfileVersion: 9\n")
    result = bootstrap.plan_repository(self.root)
    self.assertEqual("needs-decision", result["status"])
    self.assertEqual([], result["plans"])
    self.assertEqual("ambiguous-javascript-manager", result["decisions"][0]["code"])

def test_nested_example_is_ignored_until_explicitly_included(self):
    self.write("examples/demo/package.json", "{}\n")
    self.write("examples/demo/package-lock.json", "{}\n")
    self.assertEqual("not-needed", bootstrap.plan_repository(self.root)["status"])
    self.write(
        ".serena/codex-integration.yml",
        "bootstrap:\n  boundaries:\n    include: [examples/demo]\n",
    )
    self.assertEqual("npm", bootstrap.plan_repository(self.root)["plans"][0]["ecosystem"])
```

Also cover npm, pnpm, Yarn Berry, Yarn Classic, Bun, uv, Poetry, Rust, and Go argv; Gradle/Maven `ide-managed` reporting; ignored boundaries; a conventional `[tasks.bootstrap]` table detected through `codex-task list --json`; configured task precedence; argv custom-command precedence; malformed YAML/schema; symlink escape rejection; and disabled global/project policy.

- [ ] **Step 2: Run the focused test module to verify RED**

Run: `python3 -m unittest -v tests.python.test_workspace_harbor_bootstrap`

Expected: FAIL because `bin/workspace_harbor_bootstrap.py` does not exist.

- [ ] **Step 3: Implement pure evidence and plan selection**

Implement `BootstrapPlan` as a frozen dataclass with `as_dict()`. Define `RECIPE_VERSION = 1`, pruned directory names matching the doctor, and a recipe table whose commands exactly match the approved design. Read YAML mappings with strict type validation. Accept this project schema:

```yaml
bootstrap:
  enabled: true
  task: bootstrap
  command:
    argv: [tool, subcommand]
    cwd: .
    inputs: [manifest.lock]
    markers: [.environment-marker]
  boundaries:
    include: [frontend/dashboard]
    ignore: [examples]
  use_builtin_recipes: false
language_policy:
  rust: enable
serena_files: shared
```

`task` and `command` are mutually exclusive. A task/custom command suppresses built-ins unless `use_builtin_recipes: true`. Boundary paths must be relative, canonical descendants of the root, and real directories. Root is always inspected; nested boundaries are inspected only when explicitly included or represented by a root JavaScript workspace, which still produces one root package-manager plan.

Detect a conventional task by running `codex-task list --json` with a five-second timeout and checking for `bootstrap`; do not parse TOML or duplicate `codex-task` validation. Represent the task plan argv as `[CODEX_TASK, "bootstrap", "--json"]` and hash `.codex/tasks.toml` as an input.

Implement `language_evidence` with results `confirmed`, `source-only`, or `absent`. Confirm TypeScript/Svelte/Vue/Angular from source plus a JavaScript manifest/lock boundary; Python from source plus a locked Python boundary; Rust and Go from their manifest/lock pairs; Java/Kotlin from Gradle/Maven model files; and C#/PHP/Ruby/Swift from their standard project/locked boundaries. Do not weaken an explicit language ignore or bootstrap opt-out.

- [ ] **Step 4: Run recipe tests to verify GREEN**

Run:

```bash
python3 -m unittest -v tests.python.test_workspace_harbor_bootstrap
python3 -m py_compile bin/workspace_harbor_bootstrap.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add -- bin/workspace_harbor_bootstrap.py tests/python/test_workspace_harbor_bootstrap.py
git commit -m "feat: plan deterministic workspace bootstrap"
```

### Task 2: Decisions, Fingerprints, Locking, Execution, and CLI

**Files:**
- Modify: `bin/workspace_harbor_bootstrap.py`
- Create: `bin/workspace-harbor-bootstrap`
- Modify: `tests/python/test_workspace_harbor_bootstrap.py`

**Interfaces:**
- Produces `bootstrap_status(root: Path) -> dict[str, object]`, `run_bootstrap(root: Path, force: bool = False) -> dict[str, object]`, and `record_decision(root: Path, category: str, subject: str, decision: str) -> dict[str, object]`.
- Produces CLI `workspace-harbor-bootstrap status|run|decide` with exit codes `0` ready/pending/not-needed/disabled, `1` failed, `2` invalid input/config/state, and `3` needs-decision.
- State paths are injectable through `WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR` and command paths through `WORKSPACE_HARBOR_CODEX_TASK` for isolated tests.

- [ ] **Step 1: Write failing persistence and consent tests**

Add:

```python
def test_unchanged_second_run_is_cache_hit_without_second_command(self):
    self.make_npm_fixture()
    calls = []
    with mock.patch.object(bootstrap, "_execute", side_effect=lambda plan: calls.append(plan) or (0, "")):
        first = bootstrap.run_bootstrap(self.root)
        second = bootstrap.run_bootstrap(self.root)
    self.assertEqual("executed", first["cache"])
    self.assertEqual("hit", second["cache"])
    self.assertEqual(1, len(calls))

def test_changed_lockfile_invalidates_success_record(self):
    self.make_npm_fixture()
    with mock.patch.object(bootstrap, "_execute", return_value=(0, "")) as execute:
        bootstrap.run_bootstrap(self.root)
        self.write("package-lock.json", '{"changed":true}\n')
        bootstrap.run_bootstrap(self.root)
    self.assertEqual(2, execute.call_count)

def test_tracked_custom_command_requires_exact_local_approval(self):
    self.write("setup.lock", "v1\n")
    self.write(
        ".serena/codex-integration.yml",
        "bootstrap:\n  command:\n    argv: [tool, setup]\n    inputs: [setup.lock]\n",
    )
    self.assertEqual("needs-decision", bootstrap.bootstrap_status(self.root)["status"])
    bootstrap.record_decision(self.root, "command", "current", "approve")
    self.assertEqual("pending", bootstrap.bootstrap_status(self.root)["status"])
    self.write(".serena/codex-integration.yml", "bootstrap:\n  command:\n    argv: [tool, changed]\n")
    self.assertEqual("needs-decision", bootstrap.bootstrap_status(self.root)["status"])
```

Also test repository decisions shared by sibling Git worktrees; execution records separated by worktree root; language enable/ignore; tracking shared/local; corrupt state; private permissions; atomic failure behavior; protected-input mutation; missing marker; runtime/tool-version invalidation; `--force`; command timeout/missing executable; concurrent callers executing once; output truncation and redaction; no persistent raw log; and every CLI JSON/exit-code combination.

- [ ] **Step 2: Run persistence tests to verify RED**

Run: `python3 -m unittest -v tests.python.test_workspace_harbor_bootstrap`

Expected: FAIL on missing state, execution, decision, and CLI interfaces.

- [ ] **Step 3: Implement private state and exact approvals**

Derive repository keys from `git rev-parse --path-format=absolute --git-common-dir` and worktree keys from the canonical root. Store `repositories/<sha256>.json`, `worktrees/<sha256>.json`, and `locks/<sha256>.lock` under the private state directory. Use `fcntl.flock(LOCK_EX)`, strict JSON shape/version validation, same-directory mode-`0600` temporary files, `flush`, `fsync`, and `os.replace`.

Store command approval against the current plan digest, not the text `approve`. Store language decisions with their current `language_evidence` value and invalidate when that evidence changes. Store tracking policy without changing Git state.

- [ ] **Step 4: Implement fingerprints and bounded execution**

Fingerprint canonical root, recipe version, plan source/id/argv/cwd, every input path and SHA-256, integration config, executable path/version, and runtime identity. Built-in markers are `node_modules` for JavaScript and `.venv` for uv; other recipes have no required local marker. Custom markers are explicit.

Execute without a shell, with a sanitized inherited environment, captured output, and an 1,800-second default timeout. Named tasks delegate to `codex-task` and consume its compact JSON packet. Redact bearer tokens, common secret assignments, token-shaped values, and credentials embedded in URLs; return at most 8 KiB/60 lines of sanitized failure context and persist no raw command output. Reject likely secrets in configured argv.

Under the worktree lock, recompute status and fingerprint, return a cache hit without execution when valid, snapshot input hashes, run plans in stable plan-id order, validate unchanged inputs and markers, then atomically write one success record. Any plan failure or protected-input change returns `failed` and writes no success record.

- [ ] **Step 5: Implement the thin CLI and verify GREEN**

`bin/workspace-harbor-bootstrap` imports `workspace_harbor_bootstrap.main` and exits with its result. CLI parsing must implement these exact forms:

```text
workspace-harbor-bootstrap status ROOT [--json]
workspace-harbor-bootstrap run ROOT [--json] [--force]
workspace-harbor-bootstrap decide ROOT language LANGUAGE enable|ignore [--json]
workspace-harbor-bootstrap decide ROOT tracking shared|local [--json]
workspace-harbor-bootstrap decide ROOT command approve|reject [--json]
```

Run:

```bash
python3 -m unittest -v tests.python.test_workspace_harbor_bootstrap
python3 -m py_compile bin/workspace_harbor_bootstrap.py bin/workspace-harbor-bootstrap
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add -- bin/workspace_harbor_bootstrap.py bin/workspace-harbor-bootstrap \
  tests/python/test_workspace_harbor_bootstrap.py
git commit -m "feat: cache and authorize workspace bootstrap"
```

### Task 3: Serena Doctor Language and Tracking Onboarding

**Files:**
- Modify: `bin/serena-project-doctor`
- Modify: `tests/python/test_serena_project_doctor.py`

**Interfaces:**
- Consumes `bootstrap.bootstrap_status`, `bootstrap.run_bootstrap`, `bootstrap.language_evidence`, and local decisions.
- Adds report keys `bootstrap`, `pending_language_decisions`, and `serena_file_policy`.
- Adds `serena-project-doctor --bootstrap ROOT`; ordinary audit remains byte-for-byte read-only for repository files.

- [ ] **Step 1: Write failing doctor/onboarding tests**

Add:

```python
def test_confirmed_typescript_is_repaired_but_source_only_rust_requires_decision(self):
    self.write_node_lock_boundary()
    (self.root / "src/lib.rs").write_text("pub fn value() {}\n", encoding="utf-8")
    result = doctor.repair_languages(self.root)
    self.assertEqual(["typescript"], result["added_languages"])
    self.assertEqual(["rust"], result["pending_languages"])
    self.assertNotIn("rust", doctor._load_project_config(self.root)[1]["languages"])

def test_local_tracking_policy_stops_repeating_untracked_warnings(self):
    bootstrap.record_decision(self.root, "tracking", "serena-files", "local")
    report = self.audit()
    codes = {item["code"] for item in report["findings"]}
    self.assertNotIn("untracked_project_config", codes)
    self.assertNotIn("untracked_memories", codes)
    self.assertEqual("local", report["serena_file_policy"])

def test_plain_audit_reads_bootstrap_status_without_running(self):
    with mock.patch.object(bootstrap, "run_bootstrap") as run:
        report = self.audit()
    run.assert_not_called()
    self.assertIn("bootstrap", report)
```

Also cover locally approved source-only language, ignored language, shared tracking warning, no policy producing one `serena_tracking_policy` decision instead of two recurring warnings, `--bootstrap` invoking the runner, bootstrap failed/needs-decision findings, JSON output, and strict exit behavior.

- [ ] **Step 2: Run doctor tests to verify RED**

Run: `python3 -m unittest -v tests.python.test_serena_project_doctor`

Expected: FAIL because audit and repair do not consume bootstrap evidence/decisions.

- [ ] **Step 3: Implement evidence-aware language repair and tracking policy**

Import the bootstrap module. Filter only missing languages: add confirmed or locally enabled languages; retain configured languages; return ignored and pending lists. For a missing project config, create it only with confirmed/enabled detected languages and report pending languages separately. Preserve the existing YAML comment-safe additive rewrite and opt-out behavior.

Replace the two untracked warnings with one `serena_tracking_policy` `needs-decision` finding when no policy exists. For local policy, emit informational `serena_files_local`; for shared policy, keep actionable untracked warnings until Git actually tracks the files. Never call Git mutation commands.

Include read-only bootstrap status in `audit`. `--bootstrap` performs language repair and `run_bootstrap`; it returns the bootstrap exit status only after emitting the complete report. Keep semantic-health probing independent so dependency failure does not suppress Serena diagnostics.

- [ ] **Step 4: Run doctor and bootstrap tests to verify GREEN**

Run:

```bash
python3 -m unittest -v \
  tests.python.test_workspace_harbor_bootstrap \
  tests.python.test_serena_project_doctor
python3 -m py_compile bin/serena-project-doctor
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add -- bin/serena-project-doctor tests/python/test_serena_project_doctor.py
git commit -m "feat: make Serena onboarding decision-aware"
```

### Task 4: IntelliJ Opener and Serena Broker Integration

**Files:**
- Modify: `bin/open-codex-project-in-intellij`
- Modify: `tests/python/test_open_codex_project_in_intellij.py`
- Modify: `bin/serena-worktree-broker`
- Modify: `tests/python/test_serena_worktree_broker.py`

**Interfaces:**
- Opener consumes `WORKSPACE_HARBOR_BOOTSTRAP_COMMAND`, defaulting to `$HOME/.codex/bin/workspace-harbor-bootstrap`.
- Broker consumes the read-only `workspace-harbor-bootstrap status ROOT --json` result and never calls `run`.

- [ ] **Step 1: Write failing opener ordering and degradation tests**

Add isolated fake bootstrap commands that append to a log:

```python
def test_bootstrap_runs_before_already_open_shortcut(self):
    result = self.run_with_bootstrap(bootstrap_exit=0, serena_ready=True)
    self.assertEqual(["bootstrap run", "service status", "reaper is-open", "reaper touch"], self.log_lines())
    self.assertEqual(0, result.returncode)

def test_bootstrap_failure_reports_degraded_but_still_opens(self):
    result = self.run_with_bootstrap(bootstrap_exit=1, serena_ready=False)
    self.assertIn("dependency bootstrap is degraded", result.stderr)
    self.assertIn("open", self.log_lines())
```

Also prove `needs-decision` exit `3` is reported once but does not block exact-root trust/open; invalid/config exit `2` fails closed before open; cache-hit invocation occurs once per opener call; and no installer output is copied into ordinary opener output.

- [ ] **Step 2: Write failing broker status-only tests**

Replace broker language-only setup expectations with preparation status assertions:

```python
def test_connection_checks_bootstrap_status_but_never_runs_it(self):
    with mock.patch.object(broker.subprocess, "run", return_value=self.bootstrap_ready()) as run:
        broker._bootstrap_status(Path("/tmp/example"))
    command = run.call_args.args[0]
    self.assertIn("status", command)
    self.assertNotIn("run", command)
```

Keep additive language repair through `serena-project-doctor --repair-languages`; bootstrap `failed` or `needs-decision` is non-blocking status, while malformed status output is reported once and cannot be treated as ready.

- [ ] **Step 3: Run integration tests to verify RED**

Run:

```bash
python3 -m unittest -v \
  tests.python.test_open_codex_project_in_intellij \
  tests.python.test_serena_worktree_broker
```

Expected: FAIL on absent bootstrap invocation/status behavior.

- [ ] **Step 4: Implement opener and broker integration**

Invoke `workspace-harbor-bootstrap run "$project_dir" --json` after canonical root/GitHub validation and before the first `is_ready_in_intellij` call. Capture JSON privately. Exit `0` continues silently; `1` or `3` prints one concise degraded/decision message and continues; `2` or an unreadable result exits before trust/open. Do not retry inside the opener.

Add broker `_bootstrap_status(root)` with a five-second timeout and strict JSON mapping validation. Call it during `_connect` after additive language repair and before broker state acquisition; record/report status through existing error text only when invalid, but never invoke `run` and never block service startup for valid `failed`/`needs-decision` states.

- [ ] **Step 5: Run integration and full Python suites to verify GREEN**

Run:

```bash
python3 -m unittest -v \
  tests.python.test_open_codex_project_in_intellij \
  tests.python.test_serena_worktree_broker
python3 -m unittest discover -s tests/python -p 'test_*.py' -v
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```bash
git add -- bin/open-codex-project-in-intellij bin/serena-worktree-broker \
  tests/python/test_open_codex_project_in_intellij.py \
  tests/python/test_serena_worktree_broker.py
git commit -m "feat: prepare worktrees before IntelliJ indexing"
```

### Task 5: Deployment, Guidance, Verification, and Live Dogfood

**Files:**
- Create: `bin/deploy-workspace-harbor`
- Create: `tests/python/test_deploy_workspace_harbor.py`
- Modify: `README.md`
- Modify: `/Users/Monsky/.codex/AGENTS.md`
- Deploy reviewed copies to: `/Users/Monsky/.codex/bin/`

**Interfaces:**
- Produces `deploy-workspace-harbor [--dry-run]` using `CODEX_HOME` and `WORKSPACE_HARBOR_SOURCE_ROOT` overrides for isolated tests.
- Installs executable commands with mode `0755` and import modules with mode `0644`, after a private timestamped backup.

- [ ] **Step 1: Write failing isolated deployment tests**

Create a temporary source tree and `CODEX_HOME`. Assert dry-run changes nothing; a real run backs up preexisting destinations, installs `workspace-harbor-bootstrap` and `workspace_harbor_bootstrap.py` with correct modes, installs the opener/doctor/broker compatible set together, and exits nonzero without partial replacement when any source file is absent.

- [ ] **Step 2: Run deployment tests to verify RED**

Run: `python3 -m unittest -v tests.python.test_deploy_workspace_harbor`

Expected: FAIL because `bin/deploy-workspace-harbor` does not exist.

- [ ] **Step 3: Implement deployment and documentation**

The deployment helper validates every source first, creates `$CODEX_HOME/backups/workspace-harbor/<UTC timestamp>/bin`, copies existing destinations into it, stages new files in a private temporary directory, verifies SHA-256 hashes, then atomically replaces destinations. It installs the bootstrap module/wrapper plus opener, trust, reaper, Serena launcher, doctor, broker, IDE module, and itself. It must not start/stop IntelliJ or mutate repository state.

Update README with recipe precedence, decision commands, state/cache behavior, opt-outs, doctor/bootstrap usage, and troubleshooting. Update global AGENTS Serena guidance to require the opener/bootstrap path, ask only for `needs-decision`, avoid hand-running all detected installers, and explain that unchanged restarts are cache hits.

- [ ] **Step 4: Run all repository verification before deployment**

Run:

```bash
python3 -m unittest discover -s tests/python -p 'test_*.py' -v
python3 -m py_compile bin/*.py bin/intellij-project-trust \
  bin/intellij-project-reaper bin/serena-project-doctor bin/serena-worktree-broker \
  bin/workspace-harbor-bootstrap
JAVA_HOME="$HOME/Applications/IntelliJ IDEA.app/Contents/jbr/Contents/Home" \
  ./gradlew test buildPlugin verifyPlugin --console=plain
git diff --check
```

Expected: PASS with no Python failures, Gradle BUILD SUCCESSFUL, and no whitespace errors.

- [ ] **Step 5: Commit source, tests, and documentation**

```bash
git add -- bin/deploy-workspace-harbor tests/python/test_deploy_workspace_harbor.py \
  README.md docs/superpowers/plans/2026-07-12-persistent-bootstrap-onboarding.md
git commit -m "docs: deploy and operate persistent bootstrap"
```

Do not include `/Users/Monsky/.codex/AGENTS.md` in the repository commit.

- [ ] **Step 6: Deploy the reviewed command set**

Run `bin/deploy-workspace-harbor --dry-run`, inspect its exact file list and backup destination, then run `bin/deploy-workspace-harbor`. Compare SHA-256 hashes for every deployed/source pair and run deployed `--help` plus an isolated `status --json` fixture.

- [ ] **Step 7: Record the active worktree tracking decision and dogfood**

For `/Users/Monsky/Documents/Codex/2026-07-11/r11-compression-detectors`, record the user's chosen Serena tracking policy only after inspecting which Serena files are appropriate to share; do not stage them automatically. Run deployed doctor/bootstrap once, verify Python and TypeScript remain configured, then run the opener twice. The second run must report a bootstrap cache hit and execute no package manager.

Create an isolated synthetic Git repository under `/Users/Monsky/Documents/Codex` containing Rust source without Cargo files. Verify doctor/bootstrap returns `needs-decision` without adding Rust or running Cargo; record `language rust enable`, verify Serena config adds Rust on the next explicit repair, then remove only the synthetic repository created for this test.

- [ ] **Step 8: Final review, publish, and report**

Inspect `git status --short --branch`, `git diff --stat` over the implementation commits, every committed diff, deployed/source hashes, state permissions, and staged/committed files for secrets or private data. Run a fresh complete Python suite and relevant live doctor checks. Push `main` only after verification because this public repository is already configured for publishing.

Report commits, exact test/build commands, deployed version/hash evidence, dogfood outcomes, the active worktree's tracking decision, rollback backup, skipped checks, and remaining risks.
