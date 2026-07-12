"""Tests for the read-only Serena project doctor."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

try:
    import yaml  # noqa: F401
except ModuleNotFoundError:
    YAML_AVAILABLE = False
else:
    YAML_AVAILABLE = True


BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
if not BIN_DIR.is_dir(): BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
DOCTOR_PATH = BIN_DIR / "serena-project-doctor"
if YAML_AVAILABLE:
    LOADER = importlib.machinery.SourceFileLoader("serena_project_doctor", str(DOCTOR_PATH))
    SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
    assert SPEC is not None
    doctor = importlib.util.module_from_spec(SPEC)
    LOADER.exec_module(doctor)


@unittest.skipUnless(YAML_AVAILABLE, "external PyYAML dependency is unavailable")
class SerenaProjectDoctorTests(unittest.TestCase):
    """Validate language discovery and read-only reporting."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        config = self.root / ".serena" / "project.yml"
        config.parent.mkdir()
        config.write_text(
            'project_name: "fixture"\nlanguages:\n- python\ninitial_prompt: ""\n',
            encoding="utf-8",
        )
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "src" / "view.tsx").write_text("export const View = 1;\n", encoding="utf-8")
        self.integration_config = Path(self.temporary_directory.name) / "global-integration.yml"
        self.repair_lock_dir = Path(self.temporary_directory.name) / "locks"
        self.history_path = Path(self.temporary_directory.name) / "history.jsonl"
        self.config_patches = mock.patch.multiple(
            doctor,
            INTEGRATION_CONFIG=self.integration_config,
            REPAIR_LOCK_DIR=self.repair_lock_dir,
            HISTORY_PATH=self.history_path,
        )
        self.config_patches.start()

    def tearDown(self) -> None:
        self.config_patches.stop()
        self.temporary_directory.cleanup()

    def test_mixed_language_gap_is_reported(self) -> None:
        with mock.patch.object(doctor, "_jetbrains_ready", return_value=False), mock.patch.object(
            doctor, "_broker_services", return_value=[]
        ), mock.patch.object(doctor, "_memory_check", return_value="ok"):
            report = doctor.audit(self.root)

        self.assertEqual(report["configured_languages"], ["python"])
        self.assertIn("typescript", report["detected_languages"])
        self.assertIn("typescript", report["missing_languages"])
        self.assertIn("missing_languages", {item["code"] for item in report["findings"]})

    def test_audit_does_not_modify_repository(self) -> None:
        before = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        with mock.patch.object(doctor, "_jetbrains_ready", return_value=False), mock.patch.object(
            doctor, "_broker_services", return_value=[]
        ), mock.patch.object(doctor, "_memory_check", return_value="ok"):
            doctor.audit(self.root)
        after = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

        self.assertEqual(before, after)

    def test_repair_adds_missing_language_and_preserves_comments(self) -> None:
        project_config = self.root / ".serena" / "project.yml"
        project_config.write_text(
            '# project comment\nproject_name: "fixture"\n\n# language comment\nlanguages:\n- python\n\ninitial_prompt: ""\n',
            encoding="utf-8",
        )

        first = doctor.repair_languages(self.root)
        second = doctor.repair_languages(self.root)

        updated = project_config.read_text(encoding="utf-8")
        self.assertTrue(first["changed"])
        self.assertEqual(first["added_languages"], ["typescript"])
        self.assertFalse(second["changed"])
        self.assertIn("# project comment", updated)
        self.assertIn("# language comment", updated)
        self.assertEqual(doctor._load_project_config(self.root)[1]["languages"], ["python", "typescript"])

    def test_project_can_opt_out_of_language_repair(self) -> None:
        opt_out = self.root / ".serena" / "codex-integration.yml"
        opt_out.write_text("auto_repair_languages: false\n", encoding="utf-8")
        before = (self.root / ".serena" / "project.yml").read_text(encoding="utf-8")

        result = doctor.repair_languages(self.root)

        self.assertFalse(result["enabled"])
        self.assertEqual(result["reason"], "opted_out")
        self.assertEqual(
            (self.root / ".serena" / "project.yml").read_text(encoding="utf-8"),
            before,
        )

    def test_repair_creates_missing_project_with_all_detected_languages(self) -> None:
        project_config = self.root / ".serena" / "project.yml"
        project_config.unlink()

        def create_config(root: Path, languages: list[str]) -> None:
            project_config.write_text(
                'project_name: "fixture"\nlanguages:\n'
                + "".join(f"- {language}\n" for language in languages)
                + 'initial_prompt: ""\n',
                encoding="utf-8",
            )

        with mock.patch.object(doctor, "_create_project_config", side_effect=create_config) as create:
            result = doctor.repair_languages(self.root)

        self.assertTrue(result["changed"])
        self.assertTrue(result["created_project_config"])
        create.assert_called_once()
        self.assertEqual(create.call_args.args[0], self.root)
        self.assertEqual(set(create.call_args.args[1]), {"python", "typescript"})
        self.assertEqual(
            set(doctor._load_project_config(self.root)[1]["languages"]),
            {"python", "typescript"},
        )

    def test_semantic_probe_refreshes_then_overviews_with_bounded_timeout(self) -> None:
        class Client:
            def __init__(self) -> None:
                self._timeout = 300
                self.calls: list[tuple[str, object]] = []

            def refresh_file(self, relative_path: str) -> None:
                self.calls.append(("refresh", (relative_path, self._timeout)))

            def get_symbols_overview(
                self,
                relative_path: str,
                depth: int,
                include_file_documentation: bool,
            ) -> dict[str, list[object]]:
                self.calls.append(("overview", (relative_path, self._timeout)))
                return {"symbols": []}

        client = Client()

        result = doctor._probe_jetbrains_client(client, "src/app.py")

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(
            client.calls,
            [
                ("refresh", ("src/app.py", 10)),
                ("overview", ("src/app.py", 10)),
            ],
        )
        self.assertEqual(client._timeout, 300)

    def test_semantic_probe_prefers_source_file_over_gradle_kotlin_dsl(self) -> None:
        (self.root / "build.gradle.kts").write_text("plugins { java }\n", encoding="utf-8")
        java = self.root / "src/main/java/example/App.java"
        java.parent.mkdir(parents=True)
        java.write_text("package example; public final class App {}\n", encoding="utf-8")

        self.assertEqual("src/main/java/example/App.java", doctor._semantic_probe_file(self.root))

    def test_semantic_timeout_is_classified_as_stalled(self) -> None:
        class Client:
            _timeout = 300

            def refresh_file(self, relative_path: str) -> None:
                raise RuntimeError("Read timed out after 10 seconds")

        result = doctor._probe_jetbrains_client(Client(), "src/app.py")

        self.assertEqual(result["status"], "stalled")
        self.assertIn("timed out", result["error"])

    def test_failed_semantic_health_is_reported_with_bounded_recovery_advice(self) -> None:
        with mock.patch.object(doctor, "_jetbrains_ready", return_value=True), mock.patch.object(
            doctor,
            "_jetbrains_semantic_health",
            return_value={"status": "plugin-error", "error": "BadLocationException"},
        ), mock.patch.object(doctor, "_broker_services", return_value=[]), mock.patch.object(
            doctor, "_memory_check", return_value="ok"
        ):
            report = doctor.audit(self.root)

        self.assertEqual(report["jetbrains_semantic_health"]["status"], "plugin-error")
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("jetbrains_semantic_unhealthy", codes)
        self.assertIn("rerun the doctor once", report["recommended_action"].lower())

    def test_history_summarizes_semantic_stability(self) -> None:
        doctor._record_history(
            {
                "root": str(self.root),
                "status": "ready",
                "jetbrains_semantic_health": {"status": "healthy", "elapsed_ms": 20},
                "findings": [],
            }
        )
        doctor._record_history(
            {
                "root": str(self.root),
                "status": "needs-attention",
                "jetbrains_semantic_health": {"status": "stalled", "elapsed_ms": 10_001},
                "findings": [{"code": "jetbrains_semantic_unhealthy"}],
            }
        )

        summary = doctor._history_summary()

        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["semantic_status_counts"], {"healthy": 1, "stalled": 1})
        self.assertEqual(summary["healthy_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
