"""Tests for the read-only Serena project doctor."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
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
        (self.root / "package.json").write_text("{}\n", encoding="utf-8")
        (self.root / "package-lock.json").write_text("{}\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
        (self.root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        self.integration_config = Path(self.temporary_directory.name) / "global-integration.yml"
        self.repair_lock_dir = Path(self.temporary_directory.name) / "locks"
        self.history_path = Path(self.temporary_directory.name) / "history.jsonl"
        self.bootstrap_state_dir = Path(self.temporary_directory.name) / "bootstrap-state"
        self.environment_patch = mock.patch.dict(
            "os.environ",
            {"WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR": str(self.bootstrap_state_dir)},
            clear=False,
        )
        self.environment_patch.start()
        self.config_patches = mock.patch.multiple(
            doctor,
            INTEGRATION_CONFIG=self.integration_config,
            REPAIR_LOCK_DIR=self.repair_lock_dir,
            HISTORY_PATH=self.history_path,
        )
        self.config_patches.start()

    def tearDown(self) -> None:
        self.config_patches.stop()
        self.environment_patch.stop()
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

    def test_semantic_health_runs_in_serena_runtime_subprocess(self) -> None:
        payload = {
            "status": "healthy",
            "file": "src/app.py",
            "symbols": 2,
            "elapsed_ms": 14,
        }
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(payload), stderr=""
        )

        with mock.patch.object(
            doctor, "_semantic_probe_file", return_value="src/app.py"
        ), mock.patch.object(doctor.subprocess, "run", return_value=completed) as run:
            result = doctor._jetbrains_semantic_health(self.root, True, True)

        self.assertEqual(payload, result)
        self.assertEqual(
            [str(doctor.SERENA_CODEX), "semantic-probe", str(self.root), "src/app.py"],
            run.call_args.args[0],
        )
        self.assertEqual(12, run.call_args.kwargs["timeout"])

    def test_semantic_probe_prefers_intellij_native_source_over_gradle_and_java(self) -> None:
        (self.root / "build.gradle.kts").write_text("plugins { java }\n", encoding="utf-8")
        java = self.root / "src/main/java/example/App.java"
        java.parent.mkdir(parents=True)
        java.write_text("package example; public final class App {}\n", encoding="utf-8")

        self.assertEqual("src/app.py", doctor._semantic_probe_file(self.root))

    def test_non_git_probe_finds_source_before_bounded_noise(self) -> None:
        root = Path(self.temporary_directory.name) / "non-git"
        root.mkdir()
        for index in range(501):
            (root / f"noise-{index:03d}.txt").write_text("noise\n", encoding="utf-8")
        java = root / "src/main/java/example/App.java"
        java.parent.mkdir(parents=True)
        java.write_text("package example; public final class App {}\n", encoding="utf-8")

        self.assertEqual("src/main/java/example/App.java", doctor._semantic_probe_file(root))

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
        self.assertIn("--recover", report["recommended_action"].lower())
        self.assertIn("meaningful ide or repository state change", report["recommended_action"].lower())

    def test_intellij_project_state_reports_indexing_modal_and_unknown(self) -> None:
        base = {
            "root": str(self.root.resolve()),
            "safe": False,
            "reasons": ["indexing"],
            "known": {
                "unsavedDocuments": True,
                "indexing": True,
                "run": True,
                "terminal": True,
                "debugger": True,
                "modal": True,
                "closing": True,
            },
            "counts": {
                "unsavedDocuments": 0,
                "run": 0,
                "terminal": 0,
                "debugger": 0,
            },
            "active": {"indexing": True, "modal": False, "closing": False},
        }
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(base), stderr=""
        )
        with mock.patch.object(doctor, "_run", return_value=completed):
            self.assertEqual(
                "indexing", doctor._intellij_project_state(self.root)["status"]
            )

        modal = base | {
            "reasons": ["modal-active"],
            "active": {"indexing": False, "modal": True, "closing": False},
        }
        with mock.patch.object(
            doctor,
            "_run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(modal), stderr=""
            ),
        ):
            self.assertEqual(
                "modal", doctor._intellij_project_state(self.root)["status"]
            )

        unknown_activity = base | {
            "known": base["known"] | {"indexing": False},
            "active": {"indexing": False, "modal": False, "closing": False},
            "reasons": ["indexing-unknown"],
        }
        with mock.patch.object(
            doctor,
            "_run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(unknown_activity), stderr=""
            ),
        ):
            self.assertEqual(
                "unknown", doctor._intellij_project_state(self.root)["status"]
            )

        for returncode in (1, 2):
            with self.subTest(returncode=returncode), mock.patch.object(
                doctor,
                "_run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=returncode, stdout="", stderr="unavailable"
                ),
            ):
                expected = "missing" if returncode == 1 else "unknown"
                self.assertEqual(
                    expected, doctor._intellij_project_state(self.root)["status"]
                )

    def test_recovery_opens_missing_service_and_rechecks_semantics(self) -> None:
        initial = {
            "jetbrains_semantic_health": {"status": "missing"},
            "recommended_action": "open it",
        }
        healthy = {
            "jetbrains_semantic_health": {"status": "healthy"},
            "recommended_action": "Use Serena normally.",
        }
        opener = Path(self.temporary_directory.name) / "opener"
        opener.write_text("fixture", encoding="utf-8")
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ready", stderr=""
        )
        with mock.patch.object(
            doctor, "OPENER", opener
        ), mock.patch.object(
            doctor, "audit", side_effect=[initial, healthy]
        ) as audit, mock.patch.object(
            doctor.subprocess, "run", return_value=completed
        ) as run:
            report = doctor.recover_serena(self.root)

        self.assertEqual("recovered", report["recovery"]["status"])
        self.assertEqual("healthy", report["recovery"]["final_semantic_status"])
        self.assertEqual(2, audit.call_count)
        self.assertEqual([str(opener), str(self.root)], run.call_args.args[0])

    def test_opener_timeout_exceeds_bootstrap_and_ready_timeouts(self) -> None:
        opener = Path(self.temporary_directory.name) / "opener"
        opener.write_text("fixture", encoding="utf-8")
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ready", stderr=""
        )
        with mock.patch.object(
            doctor, "OPENER", opener
        ), mock.patch.object(
            doctor, "OPENER_TIMEOUT_SECONDS", 1
        ), mock.patch.object(
            doctor.subprocess, "run", return_value=completed
        ) as run:
            doctor._open_exact_project(self.root)

        self.assertGreaterEqual(run.call_args.kwargs["timeout"], 2050)

    def test_recovery_waits_for_indexing_then_retries_once(self) -> None:
        stalled = {
            "jetbrains_semantic_health": {"status": "stalled"},
            "recommended_action": "fallback",
        }
        healthy = {
            "jetbrains_semantic_health": {"status": "healthy"},
            "recommended_action": "Use Serena normally.",
        }
        states = [
            {"status": "indexing", "active": {"indexing": True}},
            {"status": "ready", "active": {"indexing": False}},
        ]
        with mock.patch.object(
            doctor, "audit", side_effect=[stalled, healthy]
        ), mock.patch.object(
            doctor, "_intellij_project_state", side_effect=states
        ) as state, mock.patch.object(doctor.time, "sleep") as sleep:
            report = doctor.recover_serena(
                self.root, wait_seconds=10, poll_seconds=0.01
            )

        self.assertEqual("recovered", report["recovery"]["status"])
        self.assertIn("waited-for-indexing", report["recovery"]["actions"])
        self.assertEqual(2, state.call_count)
        sleep.assert_called_once_with(0.01)

    def test_indexing_timeout_recycles_the_owned_window(self) -> None:
        stalled = {
            "jetbrains_semantic_health": {"status": "stalled"},
            "recommended_action": "fallback",
        }
        healthy = {
            "jetbrains_semantic_health": {"status": "healthy"},
            "recommended_action": "Use Serena normally.",
        }
        opened = subprocess.CompletedProcess(args=[], returncode=0, stdout="ready", stderr="")
        with mock.patch.object(
            doctor, "audit", side_effect=[stalled, healthy]
        ), mock.patch.object(
            doctor, "_intellij_project_state", return_value={"status": "indexing", "active": {"indexing": True}}
        ), mock.patch.object(
            doctor, "_recycle_intellij_project", return_value={"status": "closed"}
        ) as recycle, mock.patch.object(
            doctor, "_open_exact_project", return_value=opened
        ), mock.patch.object(doctor.time, "sleep"):
            report = doctor.recover_serena(self.root, wait_seconds=0)

        self.assertEqual("recovered-after-window-recycle", report["recovery"]["status"])
        self.assertIn("indexing-timeout", report["recovery"]["actions"])
        recycle.assert_called_once_with(self.root)

    def test_recovery_surfaces_modal_without_blind_semantic_retry(self) -> None:
        stalled = {
            "jetbrains_semantic_health": {"status": "plugin-error"},
            "recommended_action": "fallback",
        }
        with mock.patch.object(
            doctor, "audit", return_value=stalled
        ) as audit, mock.patch.object(
            doctor,
            "_intellij_project_state",
            return_value={"status": "modal", "active": {"modal": True}},
        ):
            report = doctor.recover_serena(self.root)

        self.assertEqual("blocked-modal", report["recovery"]["status"])
        self.assertEqual(1, audit.call_count)
        self.assertIn("modal", report["recommended_action"].lower())
        self.assertNotIn("remainder of the task", report["recommended_action"].lower())

    def test_indexing_transition_to_modal_stops_before_recycle(self) -> None:
        stalled = {
            "jetbrains_semantic_health": {"status": "stalled"},
            "recommended_action": "fallback",
        }
        states = [
            {"status": "indexing", "active": {"indexing": True}},
            {"status": "modal", "active": {"modal": True}},
        ]
        with mock.patch.object(
            doctor, "audit", return_value=stalled
        ) as audit, mock.patch.object(
            doctor, "_intellij_project_state", side_effect=states
        ), mock.patch.object(
            doctor, "_recycle_intellij_project"
        ) as recycle, mock.patch.object(doctor.time, "sleep"):
            report = doctor.recover_serena(
                self.root, wait_seconds=1, poll_seconds=0.01
            )

        self.assertEqual("blocked-modal", report["recovery"]["status"])
        self.assertEqual(1, audit.call_count)
        recycle.assert_not_called()

    def test_window_recycle_reopen_failure_is_bounded(self) -> None:
        stalled = {
            "jetbrains_semantic_health": {"status": "stalled"},
            "recommended_action": "fallback",
        }
        opened = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="x" * 2000
        )
        with mock.patch.object(
            doctor, "audit", side_effect=[stalled, stalled.copy()]
        ), mock.patch.object(
            doctor,
            "_intellij_project_state",
            return_value={"status": "ready", "active": {}},
        ), mock.patch.object(
            doctor,
            "_recycle_intellij_project",
            return_value={"status": "closed"},
        ), mock.patch.object(
            doctor, "_open_exact_project", return_value=opened
        ):
            report = doctor.recover_serena(self.root)

        self.assertEqual(
            "window-recycle-opener-failed", report["recovery"]["status"]
        )
        self.assertLessEqual(len(report["recommended_action"]), 600)
        self.assertNotIn("reopened-exact-project", report["recovery"]["actions"])

    def test_persistent_semantic_failure_keeps_later_recovery_available(self) -> None:
        stalled = {
            "jetbrains_semantic_health": {"status": "stalled"},
            "recommended_action": "fallback",
        }
        with mock.patch.object(
            doctor, "audit", side_effect=[stalled, stalled.copy()]
        ), mock.patch.object(
            doctor,
            "_intellij_project_state",
            return_value={"status": "ready", "active": {}},
        ), mock.patch.object(
            doctor,
            "_recycle_intellij_project",
            return_value={"status": "refused-unsafe", "reasons": ["run-active"]},
        ):
            report = doctor.recover_serena(self.root)

        self.assertEqual(
            "semantic-still-unhealthy", report["recovery"]["status"]
        )
        self.assertIn("keep serena recovery active", report["recommended_action"].lower())
        self.assertIn("state change", report["recommended_action"].lower())

    def test_recovery_recycles_safe_owned_window_after_persistent_stall(self) -> None:
        stalled = {
            "jetbrains_semantic_health": {"status": "stalled"},
            "recommended_action": "fallback",
        }
        healthy = {
            "jetbrains_semantic_health": {"status": "healthy"},
            "recommended_action": "Use Serena normally.",
        }
        opened = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ready", stderr=""
        )
        with mock.patch.object(
            doctor, "audit", side_effect=[stalled, stalled.copy(), healthy]
        ), mock.patch.object(
            doctor,
            "_intellij_project_state",
            return_value={"status": "ready", "safe": True, "active": {}},
        ), mock.patch.object(
            doctor,
            "_recycle_intellij_project",
            return_value={"status": "closed"},
        ) as recycle, mock.patch.object(
            doctor, "_open_exact_project", return_value=opened
        ) as opener:
            report = doctor.recover_serena(self.root)

        self.assertEqual("recovered-after-window-recycle", report["recovery"]["status"])
        self.assertIn("recycled-exact-project-window", report["recovery"]["actions"])
        recycle.assert_called_once_with(self.root)
        opener.assert_called_once_with(self.root)

    def test_recovery_restarts_validated_hung_ide_then_reopens_exact_root(self) -> None:
        stalled = {
            "jetbrains_semantic_health": {"status": "stalled"},
            "recommended_action": "fallback",
        }
        healthy = {
            "jetbrains_semantic_health": {"status": "healthy"},
            "recommended_action": "Use Serena normally.",
        }
        opened = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ready", stderr=""
        )
        with mock.patch.object(
            doctor, "audit", side_effect=[stalled, healthy]
        ), mock.patch.object(
            doctor,
            "_intellij_project_state",
            return_value={"status": "unknown", "error": "control plane timed out"},
        ), mock.patch.object(
            doctor,
            "_restart_hung_intellij",
            return_value={"status": "stopped-term", "pid": 101},
        ) as restart, mock.patch.object(
            doctor, "_open_exact_project", return_value=opened
        ) as opener:
            report = doctor.recover_serena(self.root)

        self.assertEqual("recovered-after-ide-restart", report["recovery"]["status"])
        self.assertIn("restarted-validated-hung-intellij", report["recovery"]["actions"])
        restart.assert_called_once_with(self.root)
        opener.assert_called_once_with(self.root)

    def test_recovery_never_restarts_when_hang_guard_reports_responsive(self) -> None:
        stalled = {
            "jetbrains_semantic_health": {"status": "stalled"},
            "recommended_action": "fallback",
        }
        with mock.patch.object(
            doctor, "audit", return_value=stalled
        ), mock.patch.object(
            doctor,
            "_intellij_project_state",
            return_value={"status": "unknown", "error": "transient"},
        ), mock.patch.object(
            doctor,
            "_restart_hung_intellij",
            return_value={"status": "responsive"},
        ), mock.patch.object(doctor, "_open_exact_project") as opener:
            report = doctor.recover_serena(self.root)

        self.assertEqual("project-state-unknown", report["recovery"]["status"])
        opener.assert_not_called()

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
                "recovery": {
                    "status": "semantic-still-unhealthy",
                    "actions": ["semantic-retry", "recycled-exact-project-window"],
                },
            }
        )

        summary = doctor._history_summary()

        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["semantic_status_counts"], {"healthy": 1, "stalled": 1})
        self.assertEqual(
            summary["recovery_status_counts"],
            {"not-run": 1, "semantic-still-unhealthy": 1},
        )
        self.assertEqual(
            summary["recovery_action_counts"],
            {"recycled-exact-project-window": 1, "semantic-retry": 1},
        )
        self.assertEqual(summary["healthy_rate"], 0.5)

    def test_confirmed_typescript_repairs_but_source_only_rust_needs_decision(self) -> None:
        (self.root / "src" / "lib.rs").write_text("pub fn value() {}\n", encoding="utf-8")

        result = doctor.repair_languages(self.root)

        self.assertEqual(["typescript"], result["added_languages"])
        self.assertEqual(["rust"], result["pending_languages"])
        configured = doctor._load_project_config(self.root)[1]["languages"]
        self.assertIn("typescript", configured)
        self.assertNotIn("rust", configured)

    def test_language_enable_and_ignore_decisions_are_remembered(self) -> None:
        (self.root / "src" / "lib.rs").write_text("pub fn value() {}\n", encoding="utf-8")
        doctor.bootstrap.record_decision(self.root, "language", "rust", "ignore")
        ignored = doctor.repair_languages(self.root)
        self.assertEqual(["rust"], ignored["ignored_languages"])
        self.assertNotIn("rust", doctor._load_project_config(self.root)[1]["languages"])

        doctor.bootstrap.record_decision(self.root, "language", "rust", "enable")
        enabled = doctor.repair_languages(self.root)
        self.assertIn("rust", enabled["added_languages"])

    def test_tracking_policy_replaces_repeated_untracked_warnings(self) -> None:
        memories = self.root / ".serena/memories"; memories.mkdir()
        (memories / "core.md").write_text("stable\n", encoding="utf-8")
        with mock.patch.object(doctor, "_jetbrains_ready", return_value=False), mock.patch.object(
            doctor, "_broker_services", return_value=[]
        ), mock.patch.object(doctor, "_memory_check", return_value="ok"), mock.patch.object(
            doctor.bootstrap, "bootstrap_status", return_value={"status": "not-needed", "plans": []}
        ):
            undecided = doctor.audit(self.root)
        codes = {item["code"] for item in undecided["findings"]}
        self.assertIn("serena_tracking_policy", codes)
        self.assertNotIn("untracked_project_config", codes)
        self.assertNotIn("untracked_memories", codes)

        doctor.bootstrap.record_decision(self.root, "tracking", "serena-files", "local")
        with mock.patch.object(doctor, "_jetbrains_ready", return_value=False), mock.patch.object(
            doctor, "_broker_services", return_value=[]
        ), mock.patch.object(doctor, "_memory_check", return_value="ok"), mock.patch.object(
            doctor.bootstrap, "bootstrap_status", return_value={"status": "not-needed", "plans": []}
        ):
            local = doctor.audit(self.root)
        codes = {item["code"] for item in local["findings"]}
        self.assertIn("serena_files_local", codes)
        self.assertNotIn("serena_tracking_policy", codes)
        self.assertEqual("local", local["serena_file_policy"])

    def test_plain_audit_reads_status_but_never_runs_bootstrap(self) -> None:
        with mock.patch.object(doctor.bootstrap, "bootstrap_status", return_value={"status": "pending", "plans": []}) as status, mock.patch.object(
            doctor.bootstrap, "run_bootstrap"
        ) as run, mock.patch.object(doctor, "_jetbrains_ready", return_value=False), mock.patch.object(
            doctor, "_broker_services", return_value=[]
        ), mock.patch.object(doctor, "_memory_check", return_value="ok"):
            report = doctor.audit(self.root)
        status.assert_called_once_with(self.root)
        run.assert_not_called()
        self.assertEqual("pending", report["bootstrap"]["status"])

    def test_bootstrap_flag_repairs_runs_and_returns_bootstrap_status(self) -> None:
        run_result = {"status": "needs-decision", "decisions": [{"code": "fixture"}]}
        report = {
            "root": str(self.root),
            "status": "needs-attention",
            "configured_languages": ["python", "typescript"],
            "detected_languages": ["python", "typescript"],
            "memory_count": 0,
            "tracked_memory_count": 0,
            "initial_prompt_configured": False,
            "jetbrains_service_ready": False,
            "jetbrains_semantic_health": {"status": "missing"},
            "broker_services": [],
            "auto_repair_languages": True,
            "auto_repair_setting_source": "default",
            "findings": [],
            "recommended_action": "fixture",
        }
        with mock.patch.object(doctor, "repair_languages", return_value={"changed": False}) as repair, mock.patch.object(
            doctor.bootstrap, "run_bootstrap", return_value=run_result
        ) as run, mock.patch.object(doctor, "audit", return_value=report), mock.patch.object(
            doctor, "_record_history"
        ), mock.patch("sys.stdout", new_callable=mock.MagicMock):
            status = doctor.main(["--bootstrap", "--json", str(self.root)])
        self.assertEqual(3, status)
        repair.assert_called_once_with(self.root.resolve())
        run.assert_called_once_with(self.root.resolve())

    def test_bootstrap_flag_requires_pending_language_decision(self) -> None:
        with mock.patch.object(
            doctor,
            "repair_languages",
            return_value={"changed": False, "pending_languages": ["rust"]},
        ), mock.patch.object(
            doctor.bootstrap,
            "run_bootstrap",
            return_value={"status": "ready", "cache": "hit"},
        ), mock.patch.object(
            doctor,
            "audit",
            return_value={"status": "needs-attention"},
        ), mock.patch.object(doctor, "_record_history"), mock.patch(
            "sys.stdout", new_callable=mock.MagicMock
        ):
            status = doctor.main(["--bootstrap", "--json", str(self.root)])
        self.assertEqual(3, status)

    def test_recover_exit_status_distinguishes_recovered_from_still_unhealthy(self) -> None:
        for recovery_status, expected in (
            ("healthy", 0),
            ("recovered", 0),
            ("recovered-after-window-recycle", 0),
            ("recovered-after-ide-restart", 0),
            ("semantic-still-unhealthy", 1),
        ):
            report = {
                "root": str(self.root.resolve()),
                "status": "ready" if expected == 0 else "needs-attention",
                "jetbrains_semantic_health": {
                    "status": "healthy" if expected == 0 else "stalled"
                },
                "findings": [],
                "recovery": {"status": recovery_status},
            }
            with self.subTest(recovery_status=recovery_status), mock.patch.object(
                doctor, "recover_serena", return_value=report
            ), mock.patch.object(doctor, "_record_history"), mock.patch(
                "sys.stdout", new_callable=mock.MagicMock
            ):
                self.assertEqual(
                    expected,
                    doctor.main(["--recover", "--json", str(self.root)]),
                )


if __name__ == "__main__":
    unittest.main()
