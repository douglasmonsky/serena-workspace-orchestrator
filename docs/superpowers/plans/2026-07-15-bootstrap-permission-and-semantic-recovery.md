# Bootstrap Permission and Semantic Recovery Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make cached dependency bootstrap read-only, classify operational failures accurately, and keep exact-root IntelliJ/Serena startup available when optional dependency preparation is degraded.

**Architecture:** `workspace_harbor_bootstrap.py` gains a read-only pre-lock fast path and typed operational failure packets while retaining the existing status/exit-code surface. The IntelliJ opener captures the bootstrap JSON privately, emits only a bounded whitelist-based diagnostic, and continues semantic startup for every bootstrap outcome; trust, ownership, and lifecycle guards remain unchanged.

**Tech Stack:** Python 3 standard library and `unittest`, Zsh, existing Workspace Harbor helper scripts, Gradle verification.

## Global Constraints

- Fail closed on dependency execution and state mutation, but fail partially on optional dependency preparation.
- A cache hit performs no state mutation and requires no write permission.
- Exit `2` remains reserved for invalid input, configuration, or persisted state; operational failures remain `failed` with exit `1`.
- No setup command runs unless its plan, approval, and state checks pass.
- No fallback installer, repeated bootstrap attempt, trust broadening, plugin mutation, or manual IDE process control may be added.
- Diagnostics remain bounded and sanitized and never expose environment variables, credentials, private dependency URLs, or unbounded command output.
- Tests must not interact with an installed plugin or a live IDE.

---

## File structure

- `bin/workspace_harbor_bootstrap.py`: owns bootstrap status, execution locking, failure taxonomy, redaction, and CLI exit behavior.
- `tests/python/test_workspace_harbor_bootstrap.py`: proves read-only cache hits, single-flight locking, typed failures, execution classifications, and CLI compatibility.
- `bin/open-codex-project-in-intellij`: owns dependency-preparation ordering, bounded diagnostic presentation, and the decision to continue exact-root semantic startup.
- `tests/python/test_open_codex_project_in_intellij.py`: fixture-level opener tests with fake bootstrap, service, reaper, and open commands; no live IDE use.
- `README.md`: documents cache-hit permission behavior and the separation between dependency readiness and semantic availability.

### Task 1: Make bootstrap cache hits read-only and operational failures typed

**Files:**
- Modify: `tests/python/test_workspace_harbor_bootstrap.py:1-450`
- Modify: `bin/workspace_harbor_bootstrap.py:1-531`

**Interfaces:**
- Consumes: existing `bootstrap_status(root)`, `_worktree_lock(root)`, `_failure_result(...)`, `_sanitized_tail(...)`, and `_exit_status(result)`.
- Produces: `_operational_failure(error, *, operation, started, status=None, kind=None) -> dict[str, object]`.
- Produces: `_execute(plan) -> tuple[int, str, str | None]`, where the third item is `None`, `invalid-plan`, `command-failed`, or `process-error`.
- Preserves: `ready/pending/not-needed/disabled -> 0`, `failed -> 1`, `invalid -> 2`, and `needs-decision -> 3`.

- [ ] **Step 1: Add failing read-only and classification tests**

Add `import errno` and update every `_execute` test double to return the new
three-item result. Success doubles return `(0, "", None)`; a real command
failure returns `(9, sanitized_output, "command-failed")`.

Add these focused tests to `BootstrapPlansTests`:

```python
def test_ready_cache_hit_does_not_acquire_mutating_lock(self):
    self.make_go_fixture()
    state = Path(self.tmp.name) / "state"
    identity = {"path": "/tools/go", "version": "go1"}
    with patch.dict(
        os.environ,
        {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)},
        clear=False,
    ), patch.object(
        bootstrap, "_tool_identity", return_value=identity
    ), patch.object(
        bootstrap, "_execute", return_value=(0, "", None)
    ):
        first = bootstrap.run_bootstrap(self.root)
        with patch.object(
            bootstrap,
            "_worktree_lock",
            side_effect=PermissionError(errno.EPERM, "Operation not permitted"),
        ) as lock:
            cached = bootstrap.run_bootstrap(self.root)

    self.assertEqual(("ready", "executed"), (first["status"], first["cache"]))
    self.assertEqual(("ready", "hit"), (cached["status"], cached["cache"]))
    lock.assert_not_called()


def test_nonexecuting_statuses_do_not_acquire_mutating_lock(self):
    packets = (
        {"status": "disabled", "plans": []},
        {"status": "not-needed", "plans": []},
        {"status": "needs-decision", "plans": [], "decisions": [{"code": "fixture"}]},
    )
    for packet in packets:
        with self.subTest(status=packet["status"]), patch.object(
            bootstrap, "bootstrap_status", return_value=packet
        ), patch.object(
            bootstrap,
            "_worktree_lock",
            side_effect=AssertionError("read-only result attempted a lock"),
        ) as lock:
            self.assertEqual(packet, bootstrap.run_bootstrap(self.root))
            lock.assert_not_called()


def test_mutation_boundary_failures_are_operational_and_typed(self):
    self.make_go_fixture()
    state = Path(self.tmp.name) / "state"
    identity = {"path": "/tools/go", "version": "go1"}
    failures = (
        (PermissionError(errno.EPERM, "Operation not permitted"), "permission-denied"),
        (PermissionError(errno.EACCES, "Permission denied"), "permission-denied"),
        (OSError(errno.EIO, "I/O error"), "io-error"),
    )
    for error, expected_kind in failures:
        with self.subTest(kind=expected_kind, errno=error.errno), patch.dict(
            os.environ,
            {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)},
            clear=False,
        ), patch.object(
            bootstrap, "_tool_identity", return_value=identity
        ), patch.object(
            bootstrap, "_worktree_lock", side_effect=error
        ):
            result = bootstrap.run_bootstrap(self.root)

        self.assertEqual("failed", result["status"])
        self.assertEqual(expected_kind, result["failure_kind"])
        self.assertEqual("bootstrap-state", result["operation"])
        self.assertEqual(1, bootstrap.result_exit_status(result))
        self.assertNotIn("token=", json.dumps(result))


def test_run_cli_preserves_invalid_vs_operational_exit_codes(self):
    state = Path(self.tmp.name) / "state"
    self.make_go_fixture()
    with patch.dict(
        os.environ,
        {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)},
        clear=False,
    ), patch.object(
        bootstrap, "_tool_identity", return_value={"path": "/tools/go", "version": "go1"}
    ), patch.object(
        bootstrap,
        "_worktree_lock",
        side_effect=PermissionError(errno.EPERM, "Operation not permitted"),
    ):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = bootstrap.main(["run", str(self.root), "--json"])

    packet = json.loads(output.getvalue())
    self.assertEqual(1, exit_code)
    self.assertEqual("failed", packet["status"])
    self.assertEqual("permission-denied", packet["failure_kind"])

    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = bootstrap.main(["status", str(self.root / "missing"), "--json"])
    self.assertEqual(2, exit_code)
    self.assertEqual("invalid", json.loads(output.getvalue())["status"])
```

Update `test_execute_missing_timeout_and_failure_tail_are_bounded_and_redacted`
to unpack and assert the third value:

```python
code, output, failure_kind = bootstrap._execute(plan)
self.assertEqual((127, "process-error"), (code, failure_kind))
self.assertIn("unavailable", output)

code, output, failure_kind = bootstrap._execute(plan)
self.assertEqual((124, "process-error"), (code, failure_kind))
self.assertNotIn("secret-value", output)
self.assertNotIn("abc.def", output)
```

- [ ] **Step 2: Run the bootstrap tests and verify the new contract fails**

Run:

```sh
python3 -m unittest discover -s tests/python -p 'test_workspace_harbor_bootstrap.py' -v
```

Expected: FAIL because cache hits still enter `_worktree_lock`, mutation errors
still escape or become `invalid`, and `_execute` still returns two values.

- [ ] **Step 3: Implement the read-only fast path and typed failure packets**

Add `import errno`. Add this helper after `_failure_result`:

```python
def _operational_failure(
    error: BaseException,
    *,
    operation: str,
    started: float,
    status: dict[str, object] | None = None,
    kind: str | None = None,
) -> dict[str, object]:
    error_number = getattr(error, "errno", None)
    failure_kind = kind
    if failure_kind is None:
        if isinstance(error, PermissionError) or error_number in {errno.EACCES, errno.EPERM}:
            failure_kind = "permission-denied"
        elif isinstance(error, subprocess.SubprocessError):
            failure_kind = "process-error"
        else:
            failure_kind = "io-error"
    result: dict[str, object] = {
        **(status or {}),
        "status": "failed",
        "cache": "miss",
        "failure_kind": failure_kind,
        "operation": operation,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
    detail = _sanitized_tail(error)
    if detail:
        result["error"] = detail
    return result
```

Replace `_execute` with a three-value result that distinguishes a launched
command's nonzero exit from launch/supervision failure:

```python
def _execute(plan: dict[str, object]) -> tuple[int, str, str | None]:
    argv_value = plan.get("argv")
    cwd_value = plan.get("cwd")
    if not isinstance(argv_value, list) or not all(isinstance(item, str) for item in argv_value):
        return 2, "invalid bootstrap argv", "invalid-plan"
    if not argv_value:
        return 0, "", None
    if not isinstance(cwd_value, str):
        return 2, "invalid bootstrap working directory", "invalid-plan"
    cwd = Path(cwd_value)
    identity = _tool_identity(argv_value[0], cwd)
    executable = identity.get("path")
    if not isinstance(executable, str):
        return 127, f"command unavailable: {Path(argv_value[0]).name}", "process-error"
    argv = [executable, *argv_value[1:]]
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=_execution_environment(),
            capture_output=True,
            text=True,
            timeout=_timeout_seconds(),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        output = "\n".join(
            part
            for part in (_sanitized_tail(error.stdout), _sanitized_tail(error.stderr))
            if part
        )
        return 124, output or "bootstrap command timed out", "process-error"
    except subprocess.SubprocessError as error:
        return 127, _sanitized_tail(error), "process-error"
    except OSError as error:
        return 127, _sanitized_tail(error), "process-error"
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode == 0:
        return 0, "", None
    return completed.returncode, _sanitized_tail(output), "command-failed"
```

Replace `run_bootstrap` with the preflight/recheck sequence below. Keep the
validated execution body inside the lock exactly as shown:

```python
def run_bootstrap(root: Path, force: bool = False) -> dict[str, object]:
    root = resolve_root(root)
    started = time.monotonic()
    status: dict[str, object] | None = None
    try:
        status = bootstrap_status(root)
        if status["status"] in {"disabled", "not-needed", "needs-decision"}:
            return status
        if status["status"] == "ready" and not force:
            return status

        with _worktree_lock(root):
            status = bootstrap_status(root)
            if status["status"] in {"disabled", "not-needed", "needs-decision"}:
                return status
            if status["status"] == "ready" and not force:
                return status
            plans = status.get("plans")
            fingerprint = status.get("fingerprint")
            if not isinstance(plans, list) or not isinstance(fingerprint, str):
                return _failure_result(status, kind="invalid-plan", started=started)
            _remove_worktree_success(root)
            executed: list[str] = []
            for plan in sorted(plans, key=lambda item: str(item.get("plan_id"))):
                if not isinstance(plan, dict):
                    return _failure_result(status, kind="invalid-plan", started=started)
                if not plan.get("argv"):
                    continue
                return_code, output, failure_kind = _execute(plan)
                if return_code != 0:
                    return _failure_result(
                        status,
                        kind=failure_kind or "command-failed",
                        started=started,
                        plan=plan,
                        exit_code=return_code,
                        context=output,
                    )
                executed.append(str(plan.get("plan_id")))
            after = plan_repository(root)
            if after.get("status") != "ready" or bootstrap_fingerprint(root, after["plans"]) != fingerprint:
                return _failure_result(status, kind="inputs-changed", started=started)
            missing_markers = [
                str(marker)
                for plan in plans
                for marker in _markers(plan)
                if not marker.exists()
            ]
            if missing_markers:
                return _failure_result(
                    status,
                    kind="missing-marker",
                    started=started,
                    context="missing environment marker: " + ", ".join(missing_markers),
                )
            write_worktree_success(root, fingerprint)
            return {
                **status,
                "status": "ready",
                "cache": "executed",
                "executed_plans": executed,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
    except subprocess.SubprocessError as error:
        return _operational_failure(
            error,
            operation="bootstrap-process",
            started=started,
            status=status,
            kind="process-error",
        )
    except OSError as error:
        return _operational_failure(
            error,
            operation="bootstrap-state",
            started=started,
            status=status,
        )
```

Finally, split CLI root validation from post-validation operation handling so
an invalid root remains exit `2`, while a later operational failure is exit
`1`:

```python
def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_root(args.root)
    except (OSError, ValueError) as error:
        result = {"status": "invalid", "error": _sanitized_tail(error)}
    else:
        started = time.monotonic()
        try:
            if args.command == "status":
                result = bootstrap_status(root)
            elif args.command == "run":
                result = run_bootstrap(root, force=args.force)
            else:
                values = list(args.values)
                if args.category == "language" and len(values) == 2:
                    subject, decision = values
                elif args.category == "tracking" and len(values) == 1:
                    subject, decision = "serena-files", values[0]
                elif args.category == "command" and len(values) == 1:
                    subject, decision = "current", values[0]
                else:
                    raise ValueError("invalid decision arguments")
                result = record_decision(root, args.category, subject, decision)
                result = {"status": "ready", **result}
        except ValueError as error:
            result = {"status": "invalid", "error": _sanitized_tail(error)}
        except subprocess.SubprocessError as error:
            result = _operational_failure(
                error,
                operation=f"bootstrap-{args.command}",
                started=started,
                kind="process-error",
            )
        except OSError as error:
            result = _operational_failure(
                error,
                operation=f"bootstrap-{args.command}",
                started=started,
            )
    _print_result(result, args.json)
    return _exit_status(result)
```

- [ ] **Step 4: Run focused and concurrency tests**

Run:

```sh
python3 -m unittest discover -s tests/python -p 'test_workspace_harbor_bootstrap.py' -v
```

Expected: PASS, including the existing two-process single-flight test.

- [ ] **Step 5: Commit bootstrap behavior**

```sh
git add -- bin/workspace_harbor_bootstrap.py tests/python/test_workspace_harbor_bootstrap.py
git commit -m "fix: make bootstrap cache hits read-only"
```

### Task 2: Keep semantic startup available after every bootstrap degradation

**Files:**
- Modify: `tests/python/test_open_codex_project_in_intellij.py:528-564`
- Modify: `bin/open-codex-project-in-intellij:56-82`

**Interfaces:**
- Consumes: `workspace-harbor-bootstrap run ROOT --json` and its established exit statuses.
- Produces: `format_bootstrap_diagnostic(EXIT_CODE)`, which reads JSON from stdin and prints one whitelisted diagnostic of at most 512 UTF-8 bytes.
- Preserves: one bootstrap invocation before the already-open shortcut and every existing trust, root, ownership, and lifecycle guard.

- [ ] **Step 1: Replace the blocking opener test with failing continuation cases**

Keep `test_bootstrap_runs_before_already_open_shortcut`. Replace
`test_bootstrap_failure_is_degraded_but_invalid_configuration_blocks_open`
with fixture cases for exits `1`, `2`, `3`, and an unknown exit:

```python
def test_every_bootstrap_degradation_keeps_exact_root_open_available(self) -> None:
    cases = (
        (
            1,
            {
                "status": "failed",
                "failure_kind": "permission-denied",
                "operation": "bootstrap-state",
                "error": "[Errno 1] Operation not permitted " + ("x" * 2000),
            },
            "permission-denied",
        ),
        (2, {"status": "invalid", "error": "invalid fixture configuration"}, "validation failed"),
        (3, {"status": "needs-decision"}, "needs a repository decision"),
        (9, None, "result=unreadable"),
        (None, None, "dependency bootstrap command unavailable"),
    )
    for bootstrap_exit, payload, expected_message in cases:
        with self.subTest(bootstrap_exit=bootstrap_exit), tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            home = root / "home"
            bin_dir = home / ".codex/bin"
            bin_dir.mkdir(parents=True)
            app = root / "IntelliJ IDEA.app"
            app.mkdir()
            ready = project / "ready"
            log = root / "log"
            bootstrap_command = root / "bootstrap"
            if bootstrap_exit is not None:
                rendered = json.dumps(payload) if payload is not None else "not-json"
                bootstrap_command.write_text(
                    "#!/bin/sh\n"
                    f"printf '%s\\n' '{rendered}'\n"
                    f"exit {bootstrap_exit}\n",
                    encoding="utf-8",
                )
                bootstrap_command.chmod(0o755)
            (bin_dir / "serena-codex").write_text(
                f"#!/bin/sh\n[ -f '{ready}' ]\n",
                encoding="utf-8",
            )
            (bin_dir / "serena-codex").chmod(0o755)
            (bin_dir / "intellij-project-reaper").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (bin_dir / "intellij-project-reaper").chmod(0o755)
            opener = root / "open"
            opener.write_text(
                f"#!/bin/sh\nprintf 'open\\n' >> '{log}'\ntouch '{ready}'\n",
                encoding="utf-8",
            )
            opener.chmod(0o755)
            result = subprocess.run(
                [str(HELPER), str(project)],
                capture_output=True,
                text=True,
                check=False,
                env=os.environ
                | {
                    "HOME": str(home),
                    "INTELLIJ_APP_PATH": str(app),
                    "INTELLIJ_OPEN_COMMAND": str(opener),
                    "WORKSPACE_HARBOR_BOOTSTRAP_COMMAND": str(bootstrap_command),
                    "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                    "INTELLIJ_SERENA_READY_TIMEOUT": "1",
                },
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("open", log.read_text(encoding="utf-8").splitlines())
            self.assertIn(expected_message, result.stderr)
            if bootstrap_exit == 1:
                self.assertLessEqual(len(result.stderr.encode("utf-8")), 1024)
```

- [ ] **Step 2: Run the opener test and verify invalid/unknown bootstrap results still block**

Run:

```sh
python3 -m unittest discover -s tests/python -p 'test_open_codex_project_in_intellij.py' -v
```

Expected: FAIL for exit `2`, exit `9`, and the unavailable helper because the
current `prepare_project_dependencies` returns blocking status `2`.

- [ ] **Step 3: Add bounded diagnostic formatting and non-blocking preparation**

Add this formatter immediately before `prepare_project_dependencies`:

```zsh
format_bootstrap_diagnostic() {
  local result_code="$1"

  if ! command -v python3 >/dev/null 2>&1; then
    print -r -- "exit=$result_code; diagnostic-parser=unavailable"
    return 0
  fi
  python3 -c '
import json
import sys

exit_code = sys.argv[1]
try:
    result = json.load(sys.stdin)
except (json.JSONDecodeError, OSError, TypeError, ValueError):
    print(f"exit={exit_code}; result=unreadable")
    raise SystemExit(0)
if not isinstance(result, dict):
    print(f"exit={exit_code}; result=unreadable")
    raise SystemExit(0)
parts = [f"exit={exit_code}"]
for key in ("status", "failure_kind", "operation"):
    value = result.get(key)
    if isinstance(value, str):
        parts.append(f"{key}={value}")
detail = result.get("error") or result.get("failure_context")
if isinstance(detail, str) and detail:
    parts.append("detail=" + " ".join(detail.split()))
encoded = "; ".join(parts).encode("utf-8")
if len(encoded) > 512:
    encoded = encoded[:509].decode("utf-8", errors="ignore").encode("utf-8") + b"..."
print(encoded.decode("utf-8", errors="ignore"))
' "$result_code"
}
```

Replace `prepare_project_dependencies` with:

```zsh
prepare_project_dependencies() {
  local dir="$1" result_code result_payload diagnostic category

  if [[ ! -x "$bootstrap_command" ]]; then
    print -u2 "open-codex-project-in-intellij: dependency bootstrap command unavailable; continuing with IntelliJ and Serena"
    return 0
  fi
  if result_payload="$("$bootstrap_command" run "$dir" --json 2>&1)"; then
    return 0
  else
    result_code=$?
  fi
  diagnostic="$(print -rn -- "$result_payload" | format_bootstrap_diagnostic "$result_code")"
  case "$result_code" in
    1) category="is degraded" ;;
    2) category="validation failed" ;;
    3) category="needs a repository decision" ;;
    *) category="returned an unexpected result" ;;
  esac
  print -u2 "open-codex-project-in-intellij: dependency bootstrap $category ($diagnostic); continuing with IntelliJ and Serena available for diagnosis"
  return 0
}
```

Do not change the later trust, opener lock, reaper registration, project-model,
or Serena readiness logic.

- [ ] **Step 4: Run opener and bootstrap tests together**

Run:

```sh
python3 -m unittest discover -s tests/python -p 'test_open_codex_project_in_intellij.py' -v
python3 -m unittest discover -s tests/python -p 'test_workspace_harbor_bootstrap.py' -v
```

Expected: both commands PASS. Opener fixtures prove one warning and continued
exact-root opening; bootstrap tests prove all printed details remain sanitized.

- [ ] **Step 5: Commit opener recovery behavior**

```sh
git add -- bin/open-codex-project-in-intellij tests/python/test_open_codex_project_in_intellij.py
git commit -m "fix: keep semantics available after bootstrap errors"
```

### Task 3: Document, verify, and deploy the compatible helper set

**Files:**
- Modify: `README.md:108-145`
- Verify only: `bin/deploy-workspace-harbor`
- Verify deployed copies: `~/.codex/bin/workspace-harbor-bootstrap`, `~/.codex/bin/workspace_harbor_bootstrap.py`, and `~/.codex/bin/open-codex-project-in-intellij`

**Interfaces:**
- Consumes: the Task 1 bootstrap result contract and Task 2 opener continuation contract.
- Produces: operator guidance that distinguishes dependency degradation from semantic availability.
- Produces: an atomically deployed, hash-verified helper set with the deployer's existing backup/rollback behavior.

- [ ] **Step 1: Update the README behavior contract**

Add this paragraph after the first paragraph in `Persistent dependency
bootstrap`:

```markdown
A cache hit is fully read-only: it does not acquire the mutable execution lock
or require write access to Harbor's private state directory. Pending or forced
setup still requires that lock and fails closed if state cannot be mutated.
Operational permission or I/O failures are reported as degraded `failed`
results, distinct from invalid repository configuration. Dependency
degradation never authorizes a fallback installer and does not prevent the
separately guarded IntelliJ opener from making Serena semantics available for
diagnosis.
```

- [ ] **Step 2: Run focused and full verification**

Run, in order:

```sh
python3 -m unittest discover -s tests/python -p 'test_workspace_harbor_bootstrap.py' -v
python3 -m unittest discover -s tests/python -p 'test_open_codex_project_in_intellij.py' -v
python3 -m unittest discover -s tests/python -p 'test_*.py' -v
./gradlew test buildPlugin verifyPlugin --console=plain
git diff --check
git status --short --branch
```

Expected: every test and Gradle verification task passes; `git diff --check`
is silent; status contains only the intended README change before its commit.

- [ ] **Step 3: Commit documentation**

```sh
git add -- README.md
git commit -m "docs: explain bootstrap degradation recovery"
```

- [ ] **Step 4: Review the final branch before deployment**

Run:

```sh
git status --short --branch
git diff main...HEAD --stat
git diff main...HEAD --check
git log --oneline main..HEAD
```

Inspect `git diff main...HEAD` once for unintended files, secrets, weakened
guards, unbounded output, or unrelated refactors. Expected: the design, plan,
two helper implementations, two focused test files, and README only.

- [ ] **Step 5: Dry-run and atomically deploy**

Run:

```sh
./bin/deploy-workspace-harbor --dry-run
./bin/deploy-workspace-harbor
```

Expected: the dry run lists the reviewed source hashes; deployment succeeds,
backs up replaced helpers, verifies hashes/modes, and reports the backup path.
It does not load or mutate an installed plugin and does not open or signal a
live IDE.

- [ ] **Step 6: Verify deployed identity and the original restricted cache-hit scenario**

Run each identity check separately:

```sh
cmp -s bin/workspace_harbor_bootstrap.py "$HOME/.codex/bin/workspace_harbor_bootstrap.py"
cmp -s bin/workspace-harbor-bootstrap "$HOME/.codex/bin/workspace-harbor-bootstrap"
cmp -s bin/open-codex-project-in-intellij "$HOME/.codex/bin/open-codex-project-in-intellij"
```

Then reproduce the exact safe half of the reported scenario from the managed
Codex sandbox, without invoking the opener or a live IDE:

```sh
"$HOME/.codex/bin/workspace-harbor-bootstrap" run "/Users/Monsky/Developer/Codex/legacy/2026-07-11/agent-maintainer-strict-burndown" --json
```

Expected: exit `0` with `status: ready` and `cache: hit`, despite the sandbox's
inability to write `$CODEX_HOME/state/workspace-harbor/bootstrap`. No package
manager runs and no state file changes.

- [ ] **Step 7: Report completion**

Report the three implementation commit hashes, focused/full checks, Gradle
result, deployment backup path, deployed identity checks, and the real
restricted cache-hit result. Explicitly note that live IDE recovery was not
invoked because repository guidance prohibits interaction with installed
plugins or live IDE processes.
