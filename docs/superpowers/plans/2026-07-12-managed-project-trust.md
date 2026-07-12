# Managed PyCharm Project Trust Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add exact-root, allowlisted PyCharm trust provisioning to the managed project opener without weakening global project security.

**Architecture:** A standalone Python helper validates canonical Git roots, reads and atomically updates PyCharm's native `trusted-paths.xml`, and exposes `allow`, `status`, and `audit`. The zsh opener invokes it only on the new-open path before macOS opens PyCharm. Source-controlled scripts and tests live in Workspace Harbor; reviewed copies are deployed to `~/.codex/bin` and `~/.codex/tests` after isolated verification.

**Tech Stack:** Python 3 standard library, zsh, `unittest`, Git, JetBrains XML configuration.

## Global Constraints

- Automatically trust only exact Git/worktree roots beneath `/Users/Monsky/Documents/Codex` or `/Users/Monsky/.codex/src`.
- Reject symlink escapes, nested non-root paths, missing paths, non-Git directories, arbitrary parents, and wildcard trust.
- Never disable PyCharm project security globally.
- Preserve malformed or unrelated trust state and fail closed.
- Do not remove `$USER_HOME$/Documents` during normal `allow`; migration is a separate operational gate.
- Do not interact with installed plugins or live IDE processes while implementing in this repository.

---

## File Map

- Create `bin/pycharm-project-trust`: trust validation, XML model, locking, atomic persistence, CLI.
- Create `tests/python/test_pycharm_project_trust.py`: isolated helper contract and concurrency tests.
- Create `bin/open-codex-project-in-pycharm`: source-controlled copy of the managed opener with pre-open trust integration.
- Create `tests/python/test_open_codex_project_in_pycharm.py`: source-controlled opener regression suite including trust ordering.
- Modify `README.md`: public commands, safety boundary, installation and audit behavior.
- Modify `.gitignore`: ignore Python bytecode/test caches if not already covered.
- Deploy reviewed copies to `/Users/Monsky/.codex/bin` and `/Users/Monsky/.codex/tests` only after repository tests pass.

### Task 1: Exact-root trust helper

**Files:**
- Create: `bin/pycharm-project-trust`
- Create: `tests/python/test_pycharm_project_trust.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces CLI: `pycharm-project-trust allow ROOT`, `status ROOT`, `audit`.
- Consumes environment overrides: `PYCHARM_TRUST_CONFIG_FILE`, `PYCHARM_TRUST_ALLOWED_ROOTS`, and `PYCHARM_TRUST_LOCK_TIMEOUT` for isolated tests and controlled deployment.
- Exit codes: `0` success/trusted, `1` untrusted or ineligible, `2` unsafe/unavailable state or invalid invocation.

- [ ] **Step 1: Write failing eligibility and status tests**

Create temporary Git repositories under two temporary allowed parents. Tests must assert that the exact top level is eligible, an exact entry reports trusted, and nested, missing, non-Git, outside-parent, and symlink-escape inputs are rejected without changing XML.

```python
def test_allow_accepts_only_exact_git_root(self):
    repo = self.git_repo(self.allowed / "repo")
    self.assertEqual(0, self.run_cli("allow", repo))
    self.assertEqual("trusted", self.run_cli("status", repo, capture=True))
    self.assertNotEqual(0, self.run_cli("allow", repo / "nested"))
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `python3 -m unittest -v tests.python.test_pycharm_project_trust`
Expected: FAIL because `bin/pycharm-project-trust` does not exist.

- [ ] **Step 3: Implement validation and read-only XML parsing**

Implement focused functions with these exact contracts: `canonical_git_root`
accepts a string and tuple of allowed `Path` values and returns the validated
`Path`; `load_entries` accepts the XML `Path` and returns the parsed
`ElementTree` plus a `dict[str, bool]`; `encode_path` accepts a `Path` and
optional home `Path` and returns PyCharm's string form; `trust_status` accepts a
root and entry mapping and returns `trusted` or `untrusted`.

Use `Path.resolve(strict=True)`, `git -C ROOT rev-parse --show-toplevel`, and `Path.relative_to` for the allowlist boundary. Require the Git top level to equal the requested canonical root.

- [ ] **Step 4: Write failing mutation, preservation, and audit tests**

Tests must demonstrate idempotent exact entry insertion, mode `0600`, preservation of unrelated components/entries, malformed XML fail-closed behavior, broad-entry audit reporting, and no automatic broad-entry removal.

The exact tests are named
`test_allow_preserves_unrelated_entries_and_is_idempotent`,
`test_malformed_xml_is_not_replaced`, and
`test_audit_reports_documents_as_broad_without_removing_it`. Each records the
original fixture bytes, invokes the CLI, reparses the result, and asserts both
the expected trust entry set and preservation rule.

- [ ] **Step 5: Verify the mutation tests fail for missing behavior**

Run: `python3 -m unittest -v tests.python.test_pycharm_project_trust`
Expected: mutation and audit assertions FAIL because write and audit behavior
have not been implemented.

- [ ] **Step 6: Implement locked atomic allowance and audit**

Under an adjacent exclusive `fcntl.flock`, reread the XML, add only the encoded exact entry, write a mode-`0600` temporary file in the same directory, flush and fsync, preserve a timestamped backup, then `os.replace`. Implement structured JSON audit output containing `exact`, `broad`, `outsideAllowed`, and `malformed` fields.

- [ ] **Step 7: Add and pass concurrency tests**

Run two helper processes against the same temporary registry and assert both exact entries remain and the resulting XML parses. Run the entire helper suite and require `OK`.

- [ ] **Step 8: Commit Task 1**

```bash
git add -- .gitignore bin/pycharm-project-trust tests/python/test_pycharm_project_trust.py
git commit -m "feat: add exact-root PyCharm trust helper"
```

### Task 2: Managed opener integration

**Files:**
- Create: `bin/open-codex-project-in-pycharm`
- Create: `tests/python/test_open_codex_project_in_pycharm.py`

**Interfaces:**
- Consumes executable `PYCHARM_PROJECT_TRUST_COMMAND`, defaulting to `$HOME/.codex/bin/pycharm-project-trust`.
- Preserves all current opener options, reaper behavior, serialization, and readiness semantics.

- [ ] **Step 1: Import the current opener and regression suite unchanged**

Use the deployed files as the initial source-controlled baseline and run:

```bash
python3 -m unittest -v tests.python.test_open_codex_project_in_pycharm
```

Expected: existing opener tests PASS before trust integration.

- [ ] **Step 2: Write failing trust ordering tests**

Add assertions for:

Name the tests `test_new_open_trusts_exact_root_before_open`,
`test_trust_failure_prevents_new_open_and_registration`, and
`test_already_open_root_does_not_require_trust_write`. Use the existing fake
command log to assert the exact ordered command list for each case.

The fake trust command records `allow ROOT`; the fake open command records its invocation. Assert trust occurs first and a trust exit code `2` prevents open and register.

- [ ] **Step 3: Run the new opener tests and verify RED**

Run the three named tests with `python3 -m unittest -v`.
Expected: FAIL because the opener never calls the trust helper.

- [ ] **Step 4: Implement minimal pre-open trust integration**

Add:

```zsh
project_trust_command="${PYCHARM_PROJECT_TRUST_COMMAND:-$HOME/.codex/bin/pycharm-project-trust}"
ensure_project_trusted() {
  local dir="$1"
  [[ -x "$project_trust_command" ]] || return 1
  "$project_trust_command" allow "$dir"
}
```

Call `ensure_project_trusted "$project_dir"` after acquiring the opener lock and the second already-open check, immediately before `open_new_pycharm_window`. Print one concise error and exit without opening when it fails.

- [ ] **Step 5: Run targeted and full opener suites**

Run: `python3 -m unittest -v tests.python.test_open_codex_project_in_pycharm`
Expected: all existing and trust-specific tests PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add -- bin/open-codex-project-in-pycharm tests/python/test_open_codex_project_in_pycharm.py
git commit -m "feat: trust managed projects before opening"
```

### Task 3: Documentation, deployment, and isolated verification

**Files:**
- Modify: `README.md`
- Deploy copy: `/Users/Monsky/.codex/bin/pycharm-project-trust`
- Deploy copy: `/Users/Monsky/.codex/bin/open-codex-project-in-pycharm`
- Deploy copy: `/Users/Monsky/.codex/tests/test_pycharm_project_trust.py`
- Deploy copy: `/Users/Monsky/.codex/tests/test_open_codex_project_in_pycharm.py`

**Interfaces:**
- Documents `allow`, `status`, `audit`, approved parents, and the separate broad-trust migration gate.

- [x] **Step 1: Update README with exact commands and safety rules**

Document that managed new opens auto-trust exact Git roots within the two approved parents, while already-open roots remain usable if trust state is temporarily unavailable. Include `pycharm-project-trust audit` and state that broad entries are reported but not removed.

- [x] **Step 2: Run repository verification before deployment**

```bash
python3 -m unittest discover -s tests/python -p 'test_*.py'
python3 -m py_compile bin/pycharm-project-trust
zsh -n bin/open-codex-project-in-pycharm
JAVA_HOME=/Applications/PyCharm.app/Contents/jbr/Contents/Home ./gradlew test buildPlugin verifyPlugin
git diff --check
```

Expected: all commands exit `0`.

- [x] **Step 3: Deploy reviewed copies without changing live trust state**

Back up the two existing deployed files, then copy the source-controlled files to `~/.codex/bin` with mode `0755` and copy tests to `~/.codex/tests`. Do not invoke `allow`, edit the real XML, restart PyCharm, or remove broad trust.

- [x] **Step 4: Verify deployed files against isolated state**

Point `PYCHARM_TRUST_CONFIG_FILE` and `PYCHARM_TRUST_ALLOWED_ROOTS` at temporary fixtures. Run deployed helper `allow`, `status`, and `audit`; run the global unittest discovery suite; compare source and deployed SHA-256 hashes.

- [x] **Step 5: Commit documentation**

```bash
git add -- README.md docs/superpowers/plans/2026-07-12-managed-project-trust.md
git commit -m "docs: explain managed project trust"
```

- [x] **Step 6: Record the operational gate**

Report that live no-prompt testing and removal of `$USER_HOME$/Documents` remain pending because repository guidance prohibits live IDE interaction. Provide the exact later audit command, but do not change live trust state.
