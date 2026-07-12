# IntelliJ-Only Workspace Harbor Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Workspace Harbor's PyCharm-specific command chain and plugin target with a fail-closed IntelliJ IDEA-only integration.

**Architecture:** A shared Python identity module validates the configured IntelliJ application, config directory, and Serena port ownership. Product-specific trust, opener, and reaper commands consume that identity; the Serena launcher, doctor, and broker require an IntelliJ-owned exact-root service. The Java plugin is repackaged and verified against IntelliJ IDEA, then the reviewed source set is deployed atomically.

**Tech Stack:** Python 3.9+, zsh, Java 21, Gradle IntelliJ Platform Plugin 2.18.1, IntelliJ IDEA 2026.1.4, Serena JetBrains plugin 2023.3.1, `unittest`.

## Global Constraints

- IntelliJ IDEA is the only IDE managed by Workspace Harbor.
- Production defaults target `$HOME/Applications/IntelliJ IDEA.app` and fail closed when app, version, registry, executable, or port ownership is unknown.
- Never read or write a `PyCharm*` trust registry from the IntelliJ trust helper.
- Never import PyCharm reaper or runtime state into the IntelliJ namespace.
- Never close a project when unsaved, indexing, run, terminal, debugger, modal, closing, broker-lease, inventory, or ownership state is unknown.
- PyCharm itself and its settings remain installed and untouched by source implementation.
- Live plugin disabling or uninstalling requires action-time confirmation.
- Preserve Python 3.9 compatibility for LaunchAgent-executed helpers.

---

### Task 1: Shared IntelliJ Identity and Exact-Root Trust

**Files:**
- Create: `bin/workspace_harbor_ide.py`
- Create: `tests/python/test_workspace_harbor_ide.py`
- Create: `bin/intellij-project-trust`
- Create: `tests/python/test_intellij_project_trust.py`
- Delete: `bin/pycharm-project-trust`
- Delete: `tests/python/test_pycharm_project_trust.py`

**Interfaces:**
- Produces `configured_app() -> Path`, `app_version(app: Path) -> str`, `config_dir(app: Path) -> Path`, `trusted_paths_file(app: Path) -> Path`, `is_intellij_command(command: str, app: Path) -> bool`, and `intellij_owned_port(port: int, app: Path) -> bool`.
- Produces CLI `intellij-project-trust allow ROOT`, `status ROOT`, and `audit` with existing exit-code semantics.

- [ ] **Step 1: Write failing identity tests**

Add tests using a temporary `IntelliJ IDEA.app/Contents/Info.plist` and isolated account home:

```python
def test_intellij_version_derives_exact_config_directory(self):
    app = self.make_app("2026.1.4")
    with patch.object(ide, "account_home", return_value=self.home):
        self.assertEqual(
            self.home / "Library/Application Support/JetBrains/IntelliJIdea2026.1",
            ide.config_dir(app),
        )

def test_pycharm_command_and_registry_are_rejected(self):
    app = self.make_app("2026.1.4")
    self.assertFalse(ide.is_intellij_command(
        "/Applications/PyCharm.app/Contents/MacOS/pycharm", app
    ))
```

- [ ] **Step 2: Run identity tests to verify RED**

Run: `python3 -m unittest -v tests.python.test_workspace_harbor_ide`

Expected: FAIL because `bin/workspace_harbor_ide.py` does not exist.

- [ ] **Step 3: Implement the minimal identity module**

Implement strict plist/version parsing, authenticated-home defaults, and process ownership. Port ownership runs `lsof -nP -iTCP:<port> -sTCP:LISTEN -Fp` with a bounded timeout, extracts exactly one PID, reads `ps -p <pid> -o command=`, and requires the executable under the configured app bundle. Multiple, missing, malformed, or timed-out results return `False`.

- [ ] **Step 4: Run identity tests to verify GREEN**

Run: `python3 -m unittest -v tests.python.test_workspace_harbor_ide`

Expected: PASS.

- [ ] **Step 5: Port trust tests before implementation**

Copy the behavioral tests to `test_intellij_project_trust.py`, change fixtures to `IntelliJ IDEA.app` and `IntelliJIdea2026.1`, and add:

```python
def test_live_registry_rejects_pycharm_namespace(self):
    pycharm = self.home / "Library/Application Support/JetBrains/PyCharm2026.1/options/trusted-paths.xml"
    self.assertFalse(trust._is_live_registry(pycharm))

def test_default_config_uses_configured_intellij_version(self):
    self.assertEqual(self.intellij_registry, trust._config())
```

- [ ] **Step 6: Run trust tests to verify RED**

Run: `python3 -m unittest -v tests.python.test_intellij_project_trust`

Expected: FAIL because `bin/intellij-project-trust` does not exist.

- [ ] **Step 7: Implement the IntelliJ trust helper**

Preserve canonical-root validation, XML comment preservation, locking, backup, atomic merge, audit classification, and exit codes. Import `workspace_harbor_ide` for app/config discovery. Use `INTELLIJ_APP_PATH`, `INTELLIJ_TRUST_CONFIG_FILE`, `INTELLIJ_TRUST_ALLOWED_ROOTS`, and `INTELLIJ_TRUST_LOCK_TIMEOUT` only.

- [ ] **Step 8: Run Task 1 tests**

Run:

```bash
python3 -m unittest -v \
  tests.python.test_workspace_harbor_ide \
  tests.python.test_intellij_project_trust
python3 -m py_compile bin/workspace_harbor_ide.py bin/intellij-project-trust
```

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

```bash
git add -- bin/workspace_harbor_ide.py bin/intellij-project-trust \
  tests/python/test_workspace_harbor_ide.py tests/python/test_intellij_project_trust.py \
  bin/pycharm-project-trust tests/python/test_pycharm_project_trust.py
git commit -m "feat: add IntelliJ identity and trust management"
```

### Task 2: IntelliJ Opener and Isolated Reaper

**Files:**
- Create: `bin/open-codex-project-in-intellij`
- Create: `tests/python/test_open_codex_project_in_intellij.py`
- Create: `bin/intellij-project-reaper`
- Create: `tests/python/test_intellij_project_reaper.py`
- Delete: `bin/open-codex-project-in-pycharm`
- Delete: `tests/python/test_open_codex_project_in_pycharm.py`
- Delete: `bin/pycharm-project-reaper`
- Delete: `tests/python/test_pycharm_project_reaper.py`

**Interfaces:**
- Consumes `intellij-project-trust`, `intellij-project-reaper`, and ownership-aware `serena-codex jetbrains-service-status`.
- Produces `open-codex-project-in-intellij [--require-github] [PROJECT_DIR]`.
- Produces reaper commands `register`, `touch`, `is-open`, `status`, and `cleanup` using `~/.codex/state/intellij-projects`.

- [ ] **Step 1: Port opener tests and add product isolation assertions**

Rename fixtures and environment variables to `INTELLIJ_*`. Assert the launcher invokes the configured IntelliJ app, state lives below `intellij-projects`, readiness calls the ownership-aware launcher, and output never claims PyCharm ownership:

```python
def test_opens_configured_intellij_after_exact_root_trust(self):
    result = self.run_opener(project)
    self.assertEqual([f"trust allow {project.resolve()}", "open", "register"], self.log_lines())
    self.assertIn("opened in a new IntelliJ window", result.stdout)
```

- [ ] **Step 2: Run opener tests to verify RED**

Run: `python3 -m unittest -v tests.python.test_open_codex_project_in_intellij`

Expected: FAIL because the IntelliJ opener does not exist.

- [ ] **Step 3: Implement the IntelliJ opener**

Retain two-phase already-open checks and lock ownership validation. Use only `INTELLIJ_*` variables and `$HOME/Applications/IntelliJ IDEA.app`. Remove `pycharm` and `charm` fallbacks. Call `/usr/bin/open -a "$intellij_app_path" "$dir"` and wait for the ownership-aware service check.

- [ ] **Step 4: Run opener tests to verify GREEN**

Run: `python3 -m unittest -v tests.python.test_open_codex_project_in_intellij`

Expected: PASS.

- [ ] **Step 5: Port reaper tests and add namespace isolation**

Change command and environment names to `INTELLIJ_PROJECT_REAPER_*`. Add:

```python
def test_defaults_never_read_pycharm_state(self):
    with patch.dict(os.environ, {}, clear=True):
        config = reaper.environment()
    self.assertIn("intellij-projects", str(config["state"]))
    self.assertNotIn("pycharm-projects", str(config["state"]))
```

- [ ] **Step 6: Run reaper tests to verify RED**

Run: `python3 -m unittest -v tests.python.test_intellij_project_reaper`

Expected: FAIL because `bin/intellij-project-reaper` does not exist.

- [ ] **Step 7: Implement the isolated IntelliJ reaper**

Preserve every existing validation and fail-closed close gate. Change only product names, state/runtime defaults, and environment variables. Do not migrate old state. Keep `/usr/bin/python3` compatibility.

- [ ] **Step 8: Run Task 2 tests**

Run:

```bash
zsh -n bin/open-codex-project-in-intellij
python3 -m unittest -v \
  tests.python.test_open_codex_project_in_intellij \
  tests.python.test_intellij_project_reaper
/usr/bin/python3 -m py_compile bin/intellij-project-reaper
```

Expected: PASS.

- [ ] **Step 9: Commit Task 2**

```bash
git add -- bin/open-codex-project-in-intellij bin/intellij-project-reaper \
  tests/python/test_open_codex_project_in_intellij.py tests/python/test_intellij_project_reaper.py \
  bin/open-codex-project-in-pycharm bin/pycharm-project-reaper \
  tests/python/test_open_codex_project_in_pycharm.py tests/python/test_pycharm_project_reaper.py
git commit -m "feat: cut project lifecycle over to IntelliJ"
```

### Task 3: IntelliJ-Owned Serena Routing

**Files:**
- Modify: `bin/serena-codex`
- Modify: `tests/python/test_serena_codex.py`
- Modify: `bin/serena-project-doctor`
- Modify: `tests/python/test_serena_project_doctor.py`
- Modify: `bin/serena-worktree-broker`
- Modify: `tests/python/test_serena_worktree_broker.py`

**Interfaces:**
- Consumes `workspace_harbor_ide.intellij_owned_port` and the IntelliJ opener.
- `jetbrains-service-status ROOT` succeeds only for one IntelliJ-owned exact-root service and no foreign duplicate.

- [ ] **Step 1: Write failing launcher ownership tests**

Patch the client scan to return matching clients on controlled ports and patch ownership:

```python
def test_status_rejects_foreign_duplicate_for_same_root(self):
    clients = [client(24226, self.root), client(24227, self.root)]
    with patch.object(launcher, "matching_jetbrains_clients", return_value=clients), \
         patch.object(launcher.ide, "intellij_owned_port", side_effect=lambda port, app: port == 24227):
        self.assertEqual(1, launcher.main(["jetbrains-service-status", str(self.root)]))
```

Also cover one owned match, no match, multiple owned matches, and foreign-only matches.

- [ ] **Step 2: Run launcher tests to verify RED**

Run: `python3 -m unittest -v tests.python.test_serena_codex`

Expected: FAIL because status accepts the first matching service without ownership validation.

- [ ] **Step 3: Implement ownership-aware matching**

Scan the standard 20 plugin ports, retain clients matching the exact root, classify each port using `intellij_owned_port`, and enforce the one-owned/no-foreign invariant. Keep upstream command delegation unchanged.

- [ ] **Step 4: Update doctor and broker tests first**

Assert recovery text contains `open-codex-project-in-intellij`, broker constant `INTELLIJ_LAUNCHER` points to that command, and `_open_intellij` invokes it exactly once. Assert no functional `open-codex-project-in-pycharm` string remains.

- [ ] **Step 5: Run doctor/broker tests to verify RED**

Run:

```bash
python3 -m unittest -v \
  tests.python.test_serena_project_doctor \
  tests.python.test_serena_worktree_broker
```

Expected: FAIL on PyCharm recovery and opener wiring.

- [ ] **Step 6: Implement doctor and broker cutover**

Rename opener constants/functions and recovery messages. The doctor delegates readiness ownership to `serena-codex`; its semantic probe remains bounded and additive language repair remains unchanged.

- [ ] **Step 7: Run Task 3 tests**

Run:

```bash
python3 -m unittest -v \
  tests.python.test_serena_codex \
  tests.python.test_serena_project_doctor \
  tests.python.test_serena_worktree_broker
python3 -m py_compile bin/serena-codex bin/serena-project-doctor bin/serena-worktree-broker
```

Expected: PASS.

- [ ] **Step 8: Commit Task 3**

```bash
git add -- bin/serena-codex bin/serena-project-doctor bin/serena-worktree-broker \
  tests/python/test_serena_codex.py tests/python/test_serena_project_doctor.py \
  tests/python/test_serena_worktree_broker.py
git commit -m "feat: require IntelliJ-owned Serena services"
```

### Task 4: IntelliJ-Targeted Workspace Harbor Plugin

**Files:**
- Move: `src/main/java/com/monsky/codex/pycharm/lifecycle/*.java` to `src/main/java/com/monsky/workspaceharbor/lifecycle/`
- Move: `src/test/java/com/monsky/codex/pycharm/lifecycle/*.java` to `src/test/java/com/monsky/workspaceharbor/lifecycle/`
- Modify: `src/main/resources/META-INF/plugin.xml`
- Modify: `build.gradle.kts`
- Create: `tests/python/test_intellij_plugin_target.py`

**Interfaces:**
- Produces plugin `com.monsky.workspaceharbor` build `0.1.2` for IntelliJ build `261.*`.
- Runtime HTTP contract and fail-closed lifecycle decisions remain backward-compatible.

- [ ] **Step 1: Add failing metadata/build assertions**

Require `com.monsky.workspaceharbor.lifecycle`, the IntelliJ app default, and no `/Applications/PyCharm.app` target.

- [ ] **Step 2: Run the assertion to verify RED**

Run: `python3 -m unittest -v tests.python.test_intellij_plugin_target`

Expected: FAIL on the old Java package and PyCharm build target.

- [ ] **Step 3: Move the Java package and update metadata**

Use IntelliJ/Serena semantic move when the service is healthy; otherwise perform a precise file move and update package declarations, tests, and `plugin.xml`. Do not change lifecycle behavior.

- [ ] **Step 4: Retarget Gradle to IntelliJ**

Resolve the app with:

```kotlin
val intellijAppPath = providers.environmentVariable("INTELLIJ_APP_PATH")
    .orElse("${System.getProperty("user.home")}/Applications/IntelliJ IDEA.app")
```

Use it for compiler JBR, `intellijPlatform.local`, and verifier `ides.local`. Bump plugin version to `0.1.2`.

- [ ] **Step 5: Run focused and full plugin gates**

Run:

```bash
python3 -m unittest -v tests.python.test_intellij_plugin_target
INTELLIJ_APP_PATH="$HOME/Applications/IntelliJ IDEA.app" \
JAVA_HOME="$HOME/Applications/IntelliJ IDEA.app/Contents/jbr/Contents/Home" \
./gradlew test buildPlugin verifyPlugin --console=plain
```

Expected: PASS and a distributable Workspace Harbor `0.1.2` archive.

- [ ] **Step 6: Commit Task 4**

```bash
git add -- build.gradle.kts src/main src/test tests/python/test_intellij_plugin_target.py
git commit -m "refactor: target Workspace Harbor at IntelliJ"
```

### Task 5: Documentation, Deployment, and Live Cutover

**Files:**
- Modify: `README.md`
- Modify: global `/Users/Monsky/.codex/AGENTS.md`
- Deploy reviewed commands to: `/Users/Monsky/.codex/bin/`
- Deploy plugin to IntelliJ through the normal plugin installation flow
- Remove obsolete deployed PyCharm helper commands after backup

**Interfaces:**
- Documents exact IntelliJ commands, recovery policy, language baseline, and rollback location.

- [ ] **Step 1: Update documentation and scan for functional PyCharm dependencies**

Document IntelliJ-only operation and commands. Historical spec files may retain PyCharm wording; active source, tests, README, and global guidance may not direct agents to PyCharm.

Run:

```bash
rg -n "open-codex-project-in-pycharm|pycharm-project-trust|pycharm-project-reaper|/Applications/PyCharm.app" \
  README.md AGENTS.md bin src tests build.gradle.kts
```

Expected: no matches.

- [ ] **Step 2: Run the complete source gate**

Run:

```bash
python3 -m unittest discover -s tests/python -p 'test_*.py' -v
INTELLIJ_APP_PATH="$HOME/Applications/IntelliJ IDEA.app" \
JAVA_HOME="$HOME/Applications/IntelliJ IDEA.app/Contents/jbr/Contents/Home" \
./gradlew test buildPlugin verifyPlugin --console=plain
git diff --check
```

Expected: PASS.

- [ ] **Step 3: Back up and deploy commands atomically**

Create a timestamped private backup directory below `~/.codex/backups/workspace-harbor/`, copy the current deployed helpers there, then install the six reviewed commands together with mode `0755`. Preserve the Serena uv interpreter shebang required by the deployed doctor. Remove obsolete deployed PyCharm commands only after all new command hashes match source.

- [ ] **Step 4: Install Workspace Harbor `0.1.2` in IntelliJ**

Use the built archive from `build/distributions/`. Restart IntelliJ only after action-time confirmation. Verify plugin runtime inventory and the exact-root authenticated status endpoint.

- [ ] **Step 5: Run live IntelliJ dogfood gates**

Run:

```bash
open-codex-project-in-intellij "$(git rev-parse --show-toplevel)"
serena-codex jetbrains-service-status "$(git rev-parse --show-toplevel)"
serena-project-doctor "$(git rev-parse --show-toplevel)"
intellij-project-reaper status --json
```

Then run IntelliJ-backed Serena overview, find-symbol, references, and inspections on one Java and one Python file after native Gradle import. Confirm exactly one IntelliJ process and one matching Serena service.

- [ ] **Step 6: Complete PyCharm live migration with confirmation**

At action time, request confirmation to disable or uninstall Serena and Workspace Harbor from PyCharm. Never uninstall PyCharm itself or delete its settings. If PyCharm is running, close it only when the user has authorized closure and no unsaved prompt appears.

- [ ] **Step 7: Commit documentation and deployment manifest**

```bash
git add -- README.md docs/superpowers/plans/2026-07-12-intellij-only-cutover.md
git commit -m "docs: explain IntelliJ-only Workspace Harbor setup"
```

- [ ] **Step 8: Final review**

Check `git status --short --branch`, `git diff --stat HEAD~5..HEAD`, committed diffs, deployed/source hashes, plugin archive contents, and staged/committed files for secrets or private data. Report commits, checks, skipped live actions, rollback path, and remaining risks.
