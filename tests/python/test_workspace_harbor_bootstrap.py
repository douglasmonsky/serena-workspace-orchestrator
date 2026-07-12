import importlib.machinery
import importlib.util
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
        (self.root / "outside").mkdir()
        self.write(".serena/codex-integration.yml", "bootstrap:\n  boundaries:\n    include: [missing]\n")
        self.assertEqual("needs-decision", bootstrap.plan_repository(self.root)["status"])


if __name__ == "__main__": unittest.main()
