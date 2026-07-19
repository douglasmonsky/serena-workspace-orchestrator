import importlib.machinery
import importlib.util
from contextlib import redirect_stdout
import errno
import io
import json
import os
import subprocess
import tempfile
import unittest
import sys
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "bin/workspace_harbor_bootstrap.py"
loader = importlib.machinery.SourceFileLoader("bootstrap", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
bootstrap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bootstrap
loader.exec_module(bootstrap)


class BootstrapPlansTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()

    def tearDown(self): self.tmp.cleanup()

    def write(self, name, text):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def make_go_fixture(self):
        self.write("go.mod", "module fixture\n")
        self.write("go.sum", "checksum\n")
        self.write("main.go", "package fixture\n")

    def test_root_npm_and_uv_locks_select_two_deterministic_plans(self):
        self.write("package.json", '{"name":"fixture"}\n'); self.write("package-lock.json", "{}\n")
        self.write("pyproject.toml", "[project]\nname='fixture'\n"); self.write("uv.lock", "version = 1\n")
        result = bootstrap.plan_repository(self.root)
        self.assertEqual("ready", result["status"])
        self.assertEqual([("npm", ["npm", "ci"]), ("uv", ["uv", "sync", "--frozen"])], [(p["ecosystem"], p["argv"]) for p in result["plans"]])

    def test_conflicting_javascript_locks_require_decision_and_run_nothing(self):
        self.write("package.json", "{}\n"); self.write("package-lock.json", "{}\n"); self.write("pnpm-lock.yaml", "lockfileVersion: 9\n")
        result = bootstrap.plan_repository(self.root)
        self.assertEqual("needs-decision", result["status"]); self.assertEqual([], result["plans"])
        self.assertEqual("ambiguous-javascript-manager", result["decisions"][0]["code"])

    def test_nested_example_is_ignored_until_explicitly_included(self):
        self.write("examples/demo/package.json", "{}\n"); self.write("examples/demo/package-lock.json", "{}\n")
        self.assertEqual("not-needed", bootstrap.plan_repository(self.root)["status"])
        self.write(".serena/codex-integration.yml", "bootstrap:\n  boundaries:\n    include: [examples/demo]\n")
        self.assertEqual("npm", bootstrap.plan_repository(self.root)["plans"][0]["ecosystem"])

    def test_recipe_commands_and_gradle_reporting(self):
        cases = [("package.json", "{}", "pnpm-lock.yaml", "", ["pnpm", "install", "--frozen-lockfile"]), ("package.json", "{}", "yarn.lock", "", ["yarn", "install", "--frozen-lockfile"]), ("package.json", '{"packageManager":"yarn@4"}', "yarn.lock", "", ["yarn", "install", "--immutable"]), ("package.json", "{}", "bun.lock", "", ["bun", "install", "--frozen-lockfile"]), ("pyproject.toml", "[tool.poetry]", "poetry.lock", "", ["poetry", "install", "--sync", "--no-interaction"]), ("Cargo.toml", "", "Cargo.lock", "fn main() {}", ["cargo", "fetch", "--locked"]), ("go.mod", "module x", "go.sum", "package x", ["go", "mod", "download"])]
        for manifest, contents, lock, source, argv in cases:
            with self.subTest(lock=lock), tempfile.TemporaryDirectory() as temp:
                self.root = Path(temp); self.write(manifest, contents); self.write(lock, "")
                if source: self.write("main.rs" if manifest == "Cargo.toml" else "main.go", source)
                self.assertEqual(argv, bootstrap.plan_repository(self.root)["plans"][0]["argv"])
        self.root = Path(self.tmp.name) / "gradle"; self.root.mkdir()
        self.write("build.gradle.kts", ""); result = bootstrap.plan_repository(self.root)
        self.assertIn("ide-managed", [p["ecosystem"] for p in result["plans"]])

    def test_command_precedence_disabled_policy_and_immutable_plan(self):
        self.write("package.json", "{}"); self.write("package-lock.json", "{}")
        self.write(".serena/codex-integration.yml", "bootstrap:\n  command:\n    argv: [tool, setup]\n    cwd: .\n    inputs: [package-lock.json]\n")
        plan = bootstrap.plan_repository(self.root)["plans"][0]
        self.assertEqual(["tool", "setup"], plan["argv"]); self.assertEqual("command", plan["source"])
        self.assertTrue(hasattr(bootstrap.BootstrapPlan("x", "x", "x", ".", (), (), ()), "as_dict"))
        self.write(".serena/codex-integration.yml", "bootstrap:\n  enabled: false\n")
        self.assertEqual("disabled", bootstrap.plan_repository(self.root)["status"])

    def test_bad_configuration_and_symlink_escape_need_decision(self):
        self.write(".serena/codex-integration.yml", "bootstrap:\n  task: bootstrap\n  command: {argv: [x]}\n")
        self.assertEqual("needs-decision", bootstrap.plan_repository(self.root)["status"])

    def test_custom_command_beats_conventional_task_and_builtin_opt_out_is_honored(self):
        self.write("package.json", "{}"); self.write("package-lock.json", "{}")
        self.write(".serena/codex-integration.yml", "bootstrap:\n  command: {argv: [safe, setup]}\n  use_builtin_recipes: false\n")
        with patch.object(bootstrap, "_task_plan", return_value=bootstrap.BootstrapPlan("task", "task", "task", ".", ("task",), (), ())):
            result = bootstrap.plan_repository(self.root)
        self.assertEqual(["safe", "setup"], result["plans"][0]["argv"])
        self.write(".serena/codex-integration.yml", "bootstrap:\n  use_builtin_recipes: false\n")
        self.assertEqual("not-needed", bootstrap.plan_repository(self.root)["status"])

    def test_configured_missing_task_and_invalid_command_inputs_fail_closed(self):
        self.write(".serena/codex-integration.yml", "bootstrap:\n  task: absent\n")
        with patch.object(bootstrap, "_task_plan", return_value=None):
            self.assertEqual("missing-configured-task", bootstrap.plan_repository(self.root)["decisions"][0]["code"])
        self.write(".serena/codex-integration.yml", "bootstrap:\n  command: {argv: [safe], inputs: [/escape]}\n")
        self.assertEqual("needs-decision", bootstrap.plan_repository(self.root)["status"])

    def test_ignore_excludes_explicit_nested_boundary(self):
        self.write("examples/demo/package.json", "{}"); self.write("examples/demo/package-lock.json", "{}")
        self.write(".serena/codex-integration.yml", "bootstrap:\n  boundaries:\n    include: [examples/demo]\n    ignore: [examples/demo]\n")
        self.assertEqual("not-needed", bootstrap.plan_repository(self.root)["status"])

    def test_task_discovery_requires_success_and_carries_taskfile_input(self):
        self.write(".codex/tasks.toml", "[tasks.bootstrap]\ncommand = 'true'\n")
        done = type("Done", (), {"returncode": 0, "stdout": '{"tasks":["bootstrap"]}'})()
        with patch.object(bootstrap.subprocess, "run", return_value=done):
            plan = bootstrap._task_plan(self.root, "bootstrap")
        self.assertIsNotNone(plan); self.assertIn(str(self.root / ".codex/tasks.toml"), plan.inputs)
        failed = type("Done", (), {"returncode": 1, "stdout": '{"tasks":["bootstrap"]}'})()
        with patch.object(bootstrap.subprocess, "run", return_value=failed): self.assertIsNone(bootstrap._task_plan(self.root, "bootstrap"))

    def test_invalid_package_json_is_a_decision_not_an_exception(self):
        self.write("package.json", "[]"); self.write("package-lock.json", "{}")
        result = bootstrap.plan_repository(self.root)
        self.assertEqual("needs-decision", result["status"])
        self.assertEqual("invalid-package-json", result["decisions"][0]["code"])

    def test_policy_deep_merges_nested_global_and_project_mappings(self):
        global_config = self.root / "global.yml"
        global_config.write_text("bootstrap:\n  boundaries:\n    include: [frontend]\n  enabled: true\n")
        self.write("frontend/package.json", "{}"); self.write("frontend/package-lock.json", "{}")
        self.write(".serena/codex-integration.yml", "bootstrap:\n  boundaries:\n    ignore: [frontend]\n")
        with patch.object(bootstrap, "CODEX_HOME", global_config.parent), patch.object(bootstrap, "_mapping", side_effect=lambda path: bootstrap.yaml.safe_load(global_config.read_text()) if path == global_config.parent / "serena-integration.yml" else bootstrap.yaml.safe_load(path.read_text()) if path.is_file() else {}):
            policy = bootstrap.load_policy(self.root)
        self.assertEqual(["frontend"], policy["bootstrap"]["boundaries"]["include"])
        self.assertEqual(["frontend"], policy["bootstrap"]["boundaries"]["ignore"])

    def test_language_evidence_reports_only_confirmed_source_only_or_absent(self):
        self.write("Cargo.toml", "[package]"); self.write("Cargo.lock", ""); self.write("src/main.rs", "fn main() {}")
        self.assertEqual("confirmed", bootstrap.language_evidence(self.root, "rust"))
        self.assertEqual("absent", bootstrap.language_evidence(self.root, "typescript"))
        self.write("web/app.ts", "export {}")
        self.assertEqual("source-only", bootstrap.language_evidence(self.root, "typescript"))
        self.write("package.json", "{}"); self.write("package-lock.json", "{}")
        self.assertEqual("confirmed", bootstrap.language_evidence(self.root, "typescript"))

    def test_maven_is_ide_managed_and_global_bootstrap_can_disable(self):
        self.write("pom.xml", "<project/>")
        self.assertEqual("ide-managed", bootstrap.plan_repository(self.root)["plans"][0]["ecosystem"])
        home = Path(self.tmp.name) / "codex"; (home).mkdir()
        (home / "serena-integration.yml").write_text("bootstrap:\n  enabled: false\n")
        with patch.object(bootstrap, "CODEX_HOME", home):
            self.assertEqual("disabled", bootstrap.plan_repository(self.root)["status"])

    def test_symlink_escape_boundary_is_rejected(self):
        outside = Path(self.tmp.name) / "outside"; outside.mkdir()
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        self.write(".serena/codex-integration.yml", "bootstrap:\n  boundaries:\n    include: [escape]\n")
        result = bootstrap.plan_repository(self.root)
        self.assertEqual("needs-decision", result["status"])
        self.assertEqual("invalid-boundary", result["decisions"][0]["code"])

    def test_decisions_are_private_and_language_evidence_bound(self):
        state = Path(self.tmp.name) / "state"
        self.write("src/main.rs", "fn main() {}")
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False):
            bootstrap.record_decision(self.root, "language", "rust", "enable")
            stored = bootstrap.repository_decisions(self.root)["language:rust"]
        self.assertEqual("source-only", stored["evidence"])
        record = next((state / "repositories").glob("*.json"))
        self.assertEqual(0o600, record.stat().st_mode & 0o777)
        self.assertEqual(0o700, (state / "repositories").stat().st_mode & 0o777)

    def test_cpp_language_decision_is_supported_and_evidence_bound(self):
        state = Path(self.tmp.name) / "state"
        self.write("native/main.c", "int main(void) { return 0; }\n")
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False):
            self.assertEqual("source-only", bootstrap.language_evidence(self.root, "cpp"))
            bootstrap.record_decision(self.root, "language", "cpp", "enable")
            self.assertEqual("enable", bootstrap.language_decision(self.root, "cpp"))

    def test_tracking_and_exact_command_decisions_and_corrupt_state(self):
        state = Path(self.tmp.name) / "state"; self.write("setup.lock", "v1")
        self.write(".serena/codex-integration.yml", "bootstrap:\n  command: {argv: [tool, setup], inputs: [setup.lock]}\n")
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False):
            bootstrap.record_decision(self.root, "tracking", "serena-files", "shared")
            bootstrap.record_decision(self.root, "command", "current", "approve")
            decisions = bootstrap.repository_decisions(self.root)
            self.assertEqual("shared", decisions["tracking:serena-files"]["decision"])
            self.assertEqual("approve", decisions["command:current"]["decision"])
            next((state / "repositories").glob("*.json")).write_text("bad")
            with self.assertRaises(ValueError): bootstrap.repository_decisions(self.root)

    def test_sibling_worktrees_share_repository_decision_key(self):
        sibling = Path(self.tmp.name) / "sibling"; sibling.mkdir()
        common = Path(self.tmp.name) / "common"; common.mkdir()
        state = Path(self.tmp.name) / "state"
        def git_common(command, **_): return type("Done", (), {"returncode": 0, "stdout": str(common) + "\n"})()
        with patch.object(bootstrap.subprocess, "run", side_effect=git_common), patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False):
            bootstrap.record_decision(self.root, "tracking", "serena-files", "local")
            self.assertEqual("local", bootstrap.repository_decisions(sibling)["tracking:serena-files"]["decision"])

    def test_language_decision_expires_when_evidence_changes_and_state_records_are_strict(self):
        state = Path(self.tmp.name) / "state"; self.write("src/main.rs", "fn main() {}")
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False):
            bootstrap.record_decision(self.root, "language", "rust", "ignore")
            self.assertEqual("ignore", bootstrap.language_decision(self.root, "rust"))
            self.write("Cargo.toml", ""); self.write("Cargo.lock", "")
            self.assertIsNone(bootstrap.language_decision(self.root, "rust"))
            record = next((state / "repositories").glob("*.json")); record.write_text('{"version":1,"decisions":{"language:rust":{"decision":[],"evidence":{},"digest":[]}}}')
            with self.assertRaises(ValueError): bootstrap.repository_decisions(self.root)

    def test_decision_subjects_are_constrained_on_write_and_read(self):
        state = Path(self.tmp.name) / "state"
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False):
            for category, subject, decision in (("tracking", "current", "local"), ("command", "other", "approve"), ("language", "madeup", "enable")):
                with self.subTest(category=category), self.assertRaises(ValueError): bootstrap.record_decision(self.root, category, subject, decision)
            path = state / "repositories" / (bootstrap.repository_identity(self.root) + ".json")
            path.parent.mkdir(parents=True); path.write_text('{"version":1,"decisions":{"tracking:current":{"decision":"local","evidence":"x","digest":"x"}}}')
            with self.assertRaises(ValueError): bootstrap.repository_decisions(self.root)

    def test_status_fingerprint_cache_invalidates_lock_tool_runtime_and_marker(self):
        state = Path(self.tmp.name) / "state"; self.write("package.json", "{}"); self.write("package-lock.json", "one")
        (self.root / "node_modules").mkdir()
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False), patch.object(bootstrap, "_version", return_value="v1"):
            first = bootstrap.bootstrap_status(self.root); self.assertEqual("pending", first["status"])
            bootstrap.write_worktree_success(self.root, first["fingerprint"])
            self.assertEqual("ready", bootstrap.bootstrap_status(self.root)["status"])
            self.write("package-lock.json", "two")
            self.assertEqual("pending", bootstrap.bootstrap_status(self.root)["status"])
            self.write("package-lock.json", "one"); bootstrap.write_worktree_success(self.root, bootstrap.bootstrap_status(self.root)["fingerprint"])
            (self.root / "node_modules").rmdir()
            self.assertEqual("pending", bootstrap.bootstrap_status(self.root)["status"])

    def test_status_requires_custom_approval_and_worktree_records_are_separate_and_strict(self):
        state = Path(self.tmp.name) / "state"; sibling = Path(self.tmp.name) / "sibling"; sibling.mkdir()
        for directory in (self.root, sibling):
            (directory / "setup.lock").write_text("x"); (directory / ".serena").mkdir(); (directory / ".serena/codex-integration.yml").write_text("bootstrap:\n  command: {argv: [tool, setup], inputs: [setup.lock]}\n")
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False), patch.object(bootstrap, "_version", return_value="v1"):
            self.assertEqual("needs-decision", bootstrap.bootstrap_status(self.root)["status"])
            bootstrap.record_decision(self.root, "command", "current", "approve")
            first = bootstrap.bootstrap_status(self.root); self.assertEqual("pending", first["status"])
            bootstrap.write_worktree_success(self.root, first["fingerprint"])
            bootstrap.record_decision(sibling, "command", "current", "approve")
            self.assertEqual("pending", bootstrap.bootstrap_status(sibling)["status"])
            bootstrap._worktree_path(self.root).write_text("bad")
            with self.assertRaises(ValueError): bootstrap.bootstrap_status(self.root)

    def test_global_policy_and_resolved_tool_path_invalidate_cache(self):
        state = Path(self.tmp.name) / "state"; codex = Path(self.tmp.name) / "codex"; codex.mkdir()
        self.write("package.json", "{}"); self.write("package-lock.json", ""); (self.root / "node_modules").mkdir()
        identities = [{"path": "/tools/npm-a", "version": "v1"}, {"path": "/tools/npm-b", "version": "v1"}]
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False), patch.object(bootstrap, "CODEX_HOME", codex), patch.object(bootstrap, "_tool_identity", side_effect=lambda *_: identities[0]):
            first = bootstrap.bootstrap_status(self.root); bootstrap.write_worktree_success(self.root, first["fingerprint"])
            self.assertEqual("ready", bootstrap.bootstrap_status(self.root)["status"])
            (codex / "serena-integration.yml").write_text("bootstrap: {}\n")
            self.assertEqual("pending", bootstrap.bootstrap_status(self.root)["status"])
            current = bootstrap.bootstrap_status(self.root); bootstrap.write_worktree_success(self.root, current["fingerprint"])
            identities[0] = identities[1]
            self.assertEqual("pending", bootstrap.bootstrap_status(self.root)["status"])

    def test_missing_executable_never_reports_a_cache_hit(self):
        state = Path(self.tmp.name) / "state"; self.write("package.json", "{}"); self.write("package-lock.json", ""); (self.root / "node_modules").mkdir()
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False), patch.object(bootstrap, "_tool_identity", return_value={"path": None, "version": "unavailable"}):
            result = bootstrap.bootstrap_status(self.root); bootstrap.write_worktree_success(self.root, result["fingerprint"])
            self.assertEqual("pending", bootstrap.bootstrap_status(self.root)["status"])

    def test_tool_identity_resolves_relative_cwd_and_relative_path_entries(self):
        frontend = self.root / "frontend"; frontend.mkdir()
        setup = frontend / "setup"; setup.write_text("#!/bin/sh\n"); setup.chmod(0o755)
        self.assertEqual(str(setup.resolve()), bootstrap._tool_identity("./setup", frontend)["path"])
        tools = frontend / "tools"; tools.mkdir(); tool = tools / "runner"; tool.write_text("#!/bin/sh\n"); tool.chmod(0o755)
        with patch.dict(os.environ, {"PATH": "tools"}, clear=False):
            self.assertEqual(str(tool.resolve()), bootstrap._tool_identity("runner", frontend)["path"])

    def test_non_executable_tool_is_unavailable_and_revokes_cache_hit(self):
        state = Path(self.tmp.name) / "state"; tool = self.root / "setup"; tool.write_text("#!/bin/sh\n"); tool.chmod(0o755)
        self.write("setup.lock", "x"); self.write(".serena/codex-integration.yml", "bootstrap:\n  command: {argv: [./setup], inputs: [setup.lock]}\n")
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False):
            bootstrap.record_decision(self.root, "command", "current", "approve")
            first = bootstrap.bootstrap_status(self.root); bootstrap.write_worktree_success(self.root, first["fingerprint"])
            self.assertEqual("ready", bootstrap.bootstrap_status(self.root)["status"])
            tool.chmod(0o644)
            self.assertIsNone(bootstrap._tool_identity("./setup", self.root)["path"])
            self.assertEqual("pending", bootstrap.bootstrap_status(self.root)["status"])

    def test_command_approval_digest_covers_declared_inputs_and_markers_and_lock_is_separate(self):
        state = Path(self.tmp.name) / "state"; self.write("setup.lock", "one")
        self.write(".serena/codex-integration.yml", "bootstrap:\n  command: {argv: [tool, setup], inputs: [setup.lock], markers: [.ready]}\n")
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False):
            bootstrap.record_decision(self.root, "command", "current", "approve")
            first = bootstrap.repository_decisions(self.root)["command:current"]["evidence"]
            self.write("setup.lock", "changed-content")
            self.assertEqual(first, bootstrap._decision_subject(self.root, "command", "current"))
            self.write(".serena/codex-integration.yml", "bootstrap:\n  command: {argv: [tool, setup], inputs: [other.lock], markers: [.ready]}\n")
            self.assertNotEqual(first, bootstrap._decision_subject(self.root, "command", "current"))
        self.assertTrue(list((state / "locks").glob("*.lock")))
        (self.root / "outside").mkdir()
        self.write(".serena/codex-integration.yml", "bootstrap:\n  boundaries:\n    include: [missing]\n")
        self.assertEqual("needs-decision", bootstrap.plan_repository(self.root)["status"])

    def test_run_executes_once_then_uses_cache_and_lock_change_reruns(self):
        self.make_go_fixture()
        state = Path(self.tmp.name) / "state"
        calls = []
        identity = {"path": "/tools/go", "version": "go1"}
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False), \
             patch.object(bootstrap, "_tool_identity", return_value=identity), \
             patch.object(bootstrap, "_execute", side_effect=lambda plan: calls.append(plan["plan_id"]) or (0, "", None)):
            first = bootstrap.run_bootstrap(self.root)
            second = bootstrap.run_bootstrap(self.root)
            self.write("go.sum", "changed\n")
            third = bootstrap.run_bootstrap(self.root)
        self.assertEqual(("ready", "executed"), (first["status"], first["cache"]))
        self.assertEqual(("ready", "hit"), (second["status"], second["cache"]))
        self.assertEqual(("ready", "executed"), (third["status"], third["cache"]))
        self.assertEqual(2, len(calls))

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
            {
                "status": "needs-decision",
                "plans": [],
                "decisions": [{"code": "fixture"}],
            },
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

    def test_success_record_failure_is_typed_and_never_claims_cache(self):
        self.make_go_fixture()
        state = Path(self.tmp.name) / "state"
        identity = {"path": "/tools/go", "version": "go1"}
        error = PermissionError(errno.EPERM, "Operation not permitted")
        with patch.dict(
            os.environ,
            {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)},
            clear=False,
        ), patch.object(
            bootstrap, "_tool_identity", return_value=identity
        ), patch.object(
            bootstrap, "_execute", return_value=(0, "", None)
        ) as execute, patch.object(
            bootstrap, "write_worktree_success", side_effect=error
        ):
            first = bootstrap.run_bootstrap(self.root)
            second = bootstrap.run_bootstrap(self.root)

        for result in (first, second):
            self.assertEqual("failed", result["status"])
            self.assertEqual("permission-denied", result["failure_kind"])
            self.assertEqual("bootstrap-state", result["operation"])
        self.assertEqual(2, execute.call_count)
        self.assertFalse(bootstrap._worktree_path(self.root).exists())

    def test_force_reruns_and_failure_removes_success_record_with_redaction(self):
        self.make_go_fixture()
        state = Path(self.tmp.name) / "state"
        identity = {"path": "/tools/go", "version": "go1"}
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False), \
             patch.object(bootstrap, "_tool_identity", return_value=identity), \
             patch.object(bootstrap, "_execute", return_value=(0, "", None)) as execute:
            bootstrap.run_bootstrap(self.root)
            forced = bootstrap.run_bootstrap(self.root, force=True)
            self.assertEqual("executed", forced["cache"])
            self.assertEqual(2, execute.call_count)
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False), \
             patch.object(bootstrap, "_tool_identity", return_value=identity), \
             patch.object(bootstrap, "_execute", return_value=(9, "api_key=supersecret\nAuthorization: Bearer abc.def", "command-failed")):
            failed = bootstrap.run_bootstrap(self.root, force=True)
        self.assertEqual("failed", failed["status"])
        self.assertNotIn("supersecret", json.dumps(failed))
        self.assertNotIn("abc.def", json.dumps(failed))
        self.assertFalse(bootstrap._worktree_path(self.root).exists())

    def test_input_mutation_and_missing_marker_never_write_success(self):
        self.make_go_fixture()
        state = Path(self.tmp.name) / "state"
        identity = {"path": "/tools/go", "version": "go1"}

        def mutate(_plan):
            self.write("go.sum", "mutated\n")
            return 0, "", None

        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False), \
             patch.object(bootstrap, "_tool_identity", return_value=identity), \
             patch.object(bootstrap, "_execute", side_effect=mutate):
            changed = bootstrap.run_bootstrap(self.root)
        self.assertEqual("inputs-changed", changed["failure_kind"])
        self.assertFalse(bootstrap._worktree_path(self.root).exists())

        other = Path(self.tmp.name) / "npm"; other.mkdir(); self.root = other
        self.write("package.json", "{}\n"); self.write("package-lock.json", "{}\n")
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False), \
             patch.object(bootstrap, "_tool_identity", return_value={"path": "/tools/npm", "version": "10"}), \
             patch.object(bootstrap, "_execute", return_value=(0, "", None)):
            missing = bootstrap.run_bootstrap(self.root)
        self.assertEqual("missing-marker", missing["failure_kind"])
        self.assertFalse(bootstrap._worktree_path(self.root).exists())

    def test_cli_exit_codes_json_and_tracking_decision(self):
        state = Path(self.tmp.name) / "state"
        with patch.dict(os.environ, {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)}, clear=False):
            output = io.StringIO()
            with redirect_stdout(output):
                status = bootstrap.main(["decide", str(self.root), "tracking", "local", "--json"])
            self.assertEqual(0, status)
            self.assertEqual("local", json.loads(output.getvalue())["decision"])

            self.write("package.json", "{}\n"); self.write("package-lock.json", "{}\n"); self.write("pnpm-lock.yaml", "lockfileVersion: 9\n")
            output = io.StringIO()
            with redirect_stdout(output):
                status = bootstrap.main(["status", str(self.root), "--json"])
            self.assertEqual(3, status)
            self.assertEqual("needs-decision", json.loads(output.getvalue())["status"])

            output = io.StringIO()
            with redirect_stdout(output):
                status = bootstrap.main(["status", str(self.root / "missing"), "--json"])
            self.assertEqual(2, status)
            self.assertEqual("invalid", json.loads(output.getvalue())["status"])

    def test_run_cli_preserves_invalid_vs_operational_exit_codes(self):
        state = Path(self.tmp.name) / "state"
        self.make_go_fixture()
        with patch.dict(
            os.environ,
            {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state)},
            clear=False,
        ), patch.object(
            bootstrap,
            "_tool_identity",
            return_value={"path": "/tools/go", "version": "go1"},
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

    def test_concurrent_cli_runs_execute_installer_once(self):
        self.make_go_fixture()
        home = Path(self.tmp.name) / "codex"; (home / "bin").mkdir(parents=True)
        state = Path(self.tmp.name) / "state"; tools = Path(self.tmp.name) / "tools"; tools.mkdir()
        log = Path(self.tmp.name) / "runs.log"
        fake_go = tools / "go"
        fake_go.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = --version ]; then echo go1; exit 0; fi\n"
            f"printf 'run\\n' >> '{log}'\n"
            "sleep 0.3\n",
            encoding="utf-8",
        )
        fake_go.chmod(0o755)
        wrapper = ROOT / "bin/workspace-harbor-bootstrap"
        environment = os.environ | {
            "CODEX_HOME": str(home),
            "WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(state),
            "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
        }
        commands = [[sys.executable, str(wrapper), "run", str(self.root), "--json"] for _ in range(8)]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment) for command in commands]
        results = [process.communicate(timeout=10) + (process.returncode,) for process in processes]
        self.assertEqual([0] * 8, [result[2] for result in results], results)
        self.assertEqual(1, len(log.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(["ready"] * 8, [json.loads(result[0])["status"] for result in results])

    def test_custom_argv_rejects_likely_inline_secrets(self):
        secret_values = [
            "--api-key=supersecret",
            "Authorization: Bearer abc.def",
            "https://user:password@example.test/package",
            "sk-abcdefghijklmnopqrstuvwxyz123456",
        ]
        for value in secret_values:
            with self.subTest(value=value):
                self.write(
                    ".serena/codex-integration.yml",
                    "bootstrap:\n  command:\n    argv: [tool, " + json.dumps(value) + "]\n",
                )
                result = bootstrap.plan_repository(self.root)
                self.assertEqual("needs-decision", result["status"])
                self.assertEqual("invalid-policy", result["decisions"][0]["code"])

    def test_execute_missing_timeout_and_failure_tail_are_bounded_and_redacted(self):
        plan = {
            "plan_id": "fixture",
            "source": "command",
            "ecosystem": "custom",
            "cwd": str(self.root),
            "argv": ["tool", "setup"],
            "inputs": [],
            "markers": [],
        }
        with patch.object(bootstrap, "_tool_identity", return_value={"path": None, "version": "unavailable"}):
            code, output, failure_kind = bootstrap._execute(plan)
        self.assertEqual((127, "process-error"), (code, failure_kind))
        self.assertIn("unavailable", output)

        timeout = subprocess.TimeoutExpired(
            ["/tools/tool", "setup"],
            1,
            output="api_key=secret-value\n",
            stderr="Authorization: Bearer abc.def\n",
        )
        with patch.object(bootstrap, "_tool_identity", return_value={"path": "/tools/tool", "version": "1"}), \
             patch.object(bootstrap.subprocess, "run", side_effect=timeout):
            code, output, failure_kind = bootstrap._execute(plan)
        self.assertEqual((124, "process-error"), (code, failure_kind))
        self.assertNotIn("secret-value", output)
        self.assertNotIn("abc.def", output)

        noisy = "\n".join(f"line-{index} token=secret-{index}" for index in range(1000))
        tail = bootstrap._sanitized_tail(noisy)
        self.assertLessEqual(len(tail.encode("utf-8")), 8192)
        self.assertLessEqual(len(tail.splitlines()), 60)
        self.assertNotIn("secret-999", tail)


if __name__ == "__main__": unittest.main()
