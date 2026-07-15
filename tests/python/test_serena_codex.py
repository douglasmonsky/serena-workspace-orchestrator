"""Tests for the Serena launcher used by Codex."""

from __future__ import annotations

import json
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import ModuleType, SimpleNamespace
from unittest import mock


BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
if not BIN_DIR.is_dir(): BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
LAUNCHER = BIN_DIR / "serena-codex"
PROJECT_CONFIG = 'project_name: "fixture"\nlanguages:\n- python\nlanguage_backend:\n'
SERENA_PACKAGE_AVAILABLE = importlib.util.find_spec("serena") is not None
LOADER = importlib.machinery.SourceFileLoader("serena_codex_launcher", str(LAUNCHER))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
launcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = launcher
LOADER.exec_module(launcher)


class SerenaCodexLauncherTests(unittest.TestCase):
    """Exercise project discovery without starting an MCP server."""

    def run_launcher(
        self,
        *args: str,
        cwd: Path,
        dry_run: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run the launcher with a predictable environment."""
        environment = os.environ.copy()
        if dry_run:
            environment["SERENA_CODEX_DRY_RUN"] = "1"
        return subprocess.run(
            [str(LAUNCHER), *args],
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def create_serena_project(root: Path) -> None:
        """Create the marker used by Serena project discovery."""
        marker = root / ".serena" / "project.yml"
        marker.parent.mkdir(parents=True)
        marker.write_text('project_name: "fixture"\n', encoding="utf-8")

    def test_semantic_probe_routes_to_runtime_owned_command(self) -> None:
        with mock.patch.object(
            launcher, "_semantic_probe_command", return_value=0, create=True
        ) as probe, mock.patch.object(
            launcher, "_exec_real_serena", side_effect=AssertionError("delegated upstream")
        ):
            status = launcher.main(["semantic-probe", "/repo", "src/app.py"])

        self.assertEqual(0, status)
        probe.assert_called_once_with(["semantic-probe", "/repo", "src/app.py"])

    def service_status(self, root: Path, matches, owned_ports):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
            launcher, "_loopback_access_denied", return_value=False
        ), mock.patch.object(
            launcher, "matching_jetbrains_clients", return_value=matches
        ), mock.patch.object(
            launcher.ide, "configured_app", return_value=Path("/IntelliJ IDEA.app")
        ), mock.patch.object(
            launcher.ide, "app_version", return_value="2026.1.4"
        ), mock.patch.object(
            launcher.ide,
            "intellij_owned_port",
            side_effect=lambda port, app: port in owned_ports,
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = launcher._jetbrains_service_status_command(
                ["jetbrains-service-status", str(root)]
            )
        return status, stdout.getvalue(), stderr.getvalue()

    def test_service_status_requires_one_intellij_owned_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            match = SimpleNamespace(_port=24227)
            status, stdout, stderr = self.service_status(root, [match], {24227})
        self.assertEqual(0, status, stderr)
        self.assertIn("READY IntelliJ-owned Serena service", stdout)

    def test_service_status_rejects_foreign_duplicate_for_same_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            matches = [SimpleNamespace(_port=24226), SimpleNamespace(_port=24227)]
            status, stdout, stderr = self.service_status(root, matches, {24227})
        self.assertEqual(1, status)
        self.assertEqual("", stdout)
        self.assertIn("AMBIGUOUS Serena services", stderr)

    def test_health_check_reports_denied_loopback_without_claiming_plugin_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(
                launcher, "_loopback_access_denied", return_value=True, create=True
            ), mock.patch.object(
                launcher, "_run_jetbrains_health_check", return_value=1
            ) as health_check, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                status = launcher.main(["project", "health-check", str(root)])

        self.assertEqual(2, status)
        health_check.assert_not_called()
        self.assertEqual("", stdout.getvalue())
        self.assertIn("loopback access is denied", stderr.getvalue())
        self.assertIn("not evidence that the plugin is missing", stderr.getvalue())

    def test_service_status_reports_denied_loopback_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(
                launcher, "_loopback_access_denied", return_value=True
            ), mock.patch.object(
                launcher, "matching_jetbrains_clients", return_value=[]
            ) as scan, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                status = launcher._jetbrains_service_status_command(
                    ["jetbrains-service-status", str(root)]
                )

        self.assertEqual(2, status)
        scan.assert_not_called()
        self.assertEqual("", stdout.getvalue())
        self.assertIn("loopback access is denied", stderr.getvalue())

    def test_semantic_probe_types_denied_loopback_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "example.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.object(
                launcher, "_loopback_access_denied", return_value=True
            ), contextlib.redirect_stdout(stdout):
                status = launcher._semantic_probe_command(
                    ["semantic-probe", str(root), target.name]
                )

        self.assertEqual(2, status)
        result = json.loads(stdout.getvalue())
        self.assertEqual("unavailable", result["status"])
        self.assertEqual("loopback-denied", result["failure_kind"])

    def test_unique_nested_project_is_injected_into_mcp_command(self) -> None:
        """A task root with one nested project recovers it after a restart."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            task_root = Path(temporary_directory)
            project_root = task_root / "work" / "agent-maintainer"
            self.create_serena_project(project_root)

            result = self.run_launcher(
                "start-mcp-server",
                "--context=codex",
                "--project-from-cwd",
                "--language-backend=JetBrains",
                cwd=task_root,
                dry_run=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            command = json.loads(result.stdout)
            self.assertIn(f"--project={project_root.resolve()}", command)
            self.assertNotIn("--project-from-cwd", command)

    def test_ambiguous_nested_projects_are_never_guessed(self) -> None:
        """Multiple nested projects preserve Serena's projectless behavior."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            task_root = Path(temporary_directory)
            self.create_serena_project(task_root / "work" / "first")
            self.create_serena_project(task_root / "work" / "second")

            result = self.run_launcher(
                "start-mcp-server",
                "--project-from-cwd",
                "--language-backend=JetBrains",
                cwd=task_root,
                dry_run=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            command = json.loads(result.stdout)
            self.assertIn("--project-from-cwd", command)
            self.assertFalse(any(item.startswith("--project=") for item in command))
            self.assertIn("multiple nested Serena projects", result.stderr)

    def test_upward_git_root_keeps_upstream_discovery(self) -> None:
        """Normal invocation inside a repository remains unchanged."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            working_directory = repository / "packages" / "example"
            working_directory.mkdir(parents=True)
            (repository / ".git").mkdir()
            self.create_serena_project(working_directory / "nested")

            result = self.run_launcher(
                "start-mcp-server",
                "--project-from-cwd",
                cwd=working_directory,
                dry_run=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            command = json.loads(result.stdout)
            self.assertIn("--project-from-cwd", command)
            self.assertFalse(any(item.startswith("--project=") for item in command))

    def test_explicit_project_keeps_upstream_conflict_handling(self) -> None:
        """The wrapper does not reinterpret an already invalid CLI combination."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            task_root = Path(temporary_directory)
            self.create_serena_project(task_root / "nested")

            result = self.run_launcher(
                "start-mcp-server",
                "--project=/explicit/project",
                "--project-from-cwd",
                cwd=task_root,
                dry_run=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            command = json.loads(result.stdout)
            self.assertIn("--project=/explicit/project", command)
            self.assertIn("--project-from-cwd", command)

    def test_resolve_project_reports_ambiguity_with_nonzero_status(self) -> None:
        """The diagnostic command makes ambiguous task roots observable."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            task_root = Path(temporary_directory)
            self.create_serena_project(task_root / "one")
            self.create_serena_project(task_root / "two")

            result = self.run_launcher("resolve-project", str(task_root), cwd=task_root)

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("multiple nested Serena projects", result.stderr)

    @unittest.skipUnless(SERENA_PACKAGE_AVAILABLE, "external serena package is unavailable")
    def test_jetbrains_health_failure_returns_nonzero_status(self) -> None:
        """A project absent from IntelliJ cannot produce a false green result."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory) / "not-open-in-intellij"
            marker = project_root / ".serena" / "project.yml"
            marker.parent.mkdir(parents=True)
            project_config = PROJECT_CONFIG.replace(
                "language_backend:\n", "language_backend: JetBrains\n", 1
            )
            marker.write_text(project_config, encoding="utf-8")

            result = self.run_launcher(
                "project",
                "health-check",
                str(project_root),
                cwd=project_root,
            )

            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("FAIL Serena JetBrains health check", result.stderr)

    def test_health_check_accepts_overview_when_variable_lookup_has_no_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            marker = project_root / ".serena" / "project.yml"
            marker.parent.mkdir(parents=True)
            marker.write_text('project_name: "fixture"\n', encoding="utf-8")
            source = project_root / "src/fixture/__init__.py"
            source.parent.mkdir(parents=True)
            source.write_text('__version__ = "0.1.0"\n', encoding="utf-8")

            backend = SimpleNamespace(value="JetBrains")
            config = SimpleNamespace(
                language_backend=backend,
                gui_log_window=True,
                web_dashboard=True,
                web_dashboard_open_on_launch=True,
                propagate_settings=lambda: None,
            )
            project = SimpleNamespace(
                project_config=SimpleNamespace(language_backend=backend),
                gather_source_files=lambda: ["src/fixture/__init__.py"],
            )
            client = mock.Mock()
            client.get_symbols_overview.return_value = {
                "symbols": [{"name_path": "__version__", "type": "Variable"}]
            }
            client.find_symbol.return_value = {"symbols": []}
            client.run_inspections.return_value = {"inspections": []}

            config_module = ModuleType("serena.config.serena_config")
            config_module.LanguageBackend = SimpleNamespace(JETBRAINS=backend)
            config_module.SerenaConfig = SimpleNamespace(
                from_config_file=lambda: config
            )
            client_module = ModuleType("serena.jetbrains.jetbrains_plugin_client")
            client_module.JetBrainsPluginClient = SimpleNamespace(
                from_project=lambda loaded: client
            )
            project_module = ModuleType("serena.project")
            project_module.Project = SimpleNamespace(
                load=lambda *args, **kwargs: project
            )

            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.dict(
                sys.modules,
                {
                    "serena": ModuleType("serena"),
                    "serena.config": ModuleType("serena.config"),
                    "serena.config.serena_config": config_module,
                    "serena.jetbrains": ModuleType("serena.jetbrains"),
                    "serena.jetbrains.jetbrains_plugin_client": client_module,
                    "serena.project": project_module,
                },
            ), mock.patch.object(
                launcher,
                "_write_health_log",
                return_value=project_root / "health.json",
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = launcher._run_jetbrains_health_check(project_root)

        self.assertEqual(0, status, stderr.getvalue())
        self.assertIn("PASS Serena JetBrains health check", stdout.getvalue())
        client.find_references.assert_not_called()

    @unittest.skipUnless(SERENA_PACKAGE_AVAILABLE, "external serena package is unavailable")
    def test_jetbrains_service_status_reports_missing_project(self) -> None:
        """Service discovery distinguishes a closed project from a dropped link."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory) / "not-open-in-intellij"
            project_root.mkdir()

            result = self.run_launcher(
                "jetbrains-service-status",
                str(project_root),
                cwd=project_root,
            )

            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("MISSING Serena JetBrains service", result.stderr)

    def test_jetbrains_mcp_process_receives_freshness_overlay_pythonpath(self) -> None:
        """The primary JetBrains server receives the compatibility layer."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_serena = root / "fake-serena"
            fake_serena.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$PYTHONPATH\"\n",
                encoding="utf-8",
            )
            fake_serena.chmod(0o755)
            environment = os.environ.copy()
            environment["SERENA_CODEX_EXECUTABLE"] = str(fake_serena)

            result = subprocess.run(
                [
                    str(LAUNCHER),
                    "start-mcp-server",
                    "--language-backend=JetBrains",
                ],
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                str(Path.home() / ".codex" / "lib" / "serena-freshness"),
                result.stdout.strip().split(":"),
            )

    def test_lsp_mcp_process_does_not_receive_freshness_overlay(self) -> None:
        """The JetBrains-only overlay must not leak into language servers."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_serena = root / "fake-serena"
            fake_serena.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$PYTHONPATH\"\n",
                encoding="utf-8",
            )
            fake_serena.chmod(0o755)
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment["SERENA_CODEX_EXECUTABLE"] = str(fake_serena)

            result = subprocess.run(
                [
                    str(LAUNCHER),
                    "start-mcp-server",
                    "--language-backend=LSP",
                ],
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("serena-freshness", result.stdout)


if __name__ == "__main__":
    unittest.main()
