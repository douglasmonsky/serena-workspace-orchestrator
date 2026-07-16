"""Tests for the Serena task-bridge doctor and incident state machine."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
DOCTOR_PATH = BIN_DIR / "serena-bridge-doctor"
LOADER = importlib.machinery.SourceFileLoader("serena_bridge_doctor", str(DOCTOR_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
doctor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = doctor
LOADER.exec_module(doctor)


class StatusClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("/fixture/project")
        self.config = doctor.bridge.ConfigCheck("healthy", "configured", "/broker")
        self.project = {"jetbrains_semantic_health": {"status": "healthy"}}
        self.handshake = doctor.bridge.HandshakeResult(
            "healthy", "handshake-complete", 10, 15, 3, True, 0
        )

    def test_status_keeps_backend_handshake_and_exposure_separate(self) -> None:
        report = doctor.build_status(
            root=self.root,
            reported_tools="missing",
            config=self.config,
            project=self.project,
            handshake=self.handshake,
        )

        self.assertEqual("restart-eligible", report["status"])
        self.assertEqual("backend-healthy", report["backend"])
        self.assertEqual("handshake-healthy", report["handshake"])
        self.assertEqual("task-tools-missing", report["task_exposure"])

    def test_unknown_tool_report_never_claims_exposure(self) -> None:
        report = doctor.build_status(
            root=self.root,
            reported_tools="unknown",
            config=self.config,
            project=self.project,
            handshake=self.handshake,
        )

        self.assertEqual("exposure-unverified", report["status"])
        self.assertEqual("task-tools-unknown", report["task_exposure"])

    def test_present_tools_with_healthy_layers_is_healthy(self) -> None:
        report = doctor.build_status(
            root=self.root,
            reported_tools="present",
            config=self.config,
            project=self.project,
            handshake=self.handshake,
        )

        self.assertEqual("healthy", report["status"])
        self.assertEqual("none", report["next_action"])

    def test_invalid_config_precedes_lower_layer_classification(self) -> None:
        report = doctor.build_status(
            root=self.root,
            reported_tools="missing",
            config=doctor.bridge.ConfigCheck("invalid", "wrong-command", "/other"),
            project=self.project,
            handshake=self.handshake,
        )

        self.assertEqual("configuration-invalid", report["status"])
        self.assertEqual("config-wrong-command", report["reason"])

    def test_missing_codex_cli_is_configuration_failure(self) -> None:
        report = doctor.build_status(
            root=self.root,
            reported_tools="missing",
            config=doctor.bridge.ConfigCheck(
                "unavailable", "codex-cli-missing", None
            ),
            project=self.project,
            handshake=None,
        )

        self.assertEqual("configuration-invalid", report["status"])
        self.assertEqual("config-codex-cli-missing", report["reason"])

    def test_unhealthy_backend_and_failed_handshake_are_distinct(self) -> None:
        backend = doctor.build_status(
            root=self.root,
            reported_tools="missing",
            config=self.config,
            project={"jetbrains_semantic_health": {"status": "stalled"}},
            handshake=None,
        )
        handshake = doctor.build_status(
            root=self.root,
            reported_tools="missing",
            config=self.config,
            project=self.project,
            handshake=doctor.bridge.HandshakeResult(
                "failed", "initialize-timeout", None, None, 0, False, -15
            ),
        )

        self.assertEqual("backend-unhealthy", backend["status"])
        self.assertEqual("handshake-unhealthy", handshake["status"])


class IncidentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary_directory.name) / "incidents"
        self.store = doctor.bridge.IncidentStore(self.state)
        self.root = Path(self.temporary_directory.name) / "repo"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_incident_is_private_atomic_and_enforces_transitions(self) -> None:
        incident = self.store.create(
            self.root, "thread-123", "restart-eligible", "task-tools-missing"
        )

        self.assertEqual(0o600, self.store.path_for(incident.id).stat().st_mode & 0o777)
        loaded = self.store.load(incident.id)
        self.assertEqual(incident, loaded)
        transitioned = self.store.transition(
            incident.id, "restart-eligible", "restart-prepared"
        )
        self.assertEqual("restart-prepared", transitioned.state)
        with self.assertRaises(ValueError):
            self.store.transition(incident.id, "restart-prepared", "closed-healthy")

    def test_open_restart_incident_is_reused_for_same_root_and_thread(self) -> None:
        first = self.store.create_or_reuse_restart(
            self.root, "thread-123", "task-tools-missing"
        )
        second = self.store.create_or_reuse_restart(
            self.root, "thread-123", "task-tools-missing"
        )

        self.assertEqual(first.id, second.id)

    def test_incident_rejects_free_form_thread_and_reason(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create(
                self.root, "thread contains prompt text", "restart-eligible", "safe"
            )
        with self.assertRaises(ValueError):
            self.store.create(
                self.root, "thread-123", "restart-eligible", "private details"
            )


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "repo"
        self.root.mkdir()
        self.store = doctor.bridge.IncidentStore(
            Path(self.temporary_directory.name) / "incidents"
        )
        self.config = doctor.bridge.ConfigCheck("healthy", "configured", "/broker")
        self.healthy_project = {"jetbrains_semantic_health": {"status": "healthy"}}
        self.healthy_handshake = doctor.bridge.HandshakeResult(
            "healthy", "handshake-complete", 10, 12, 3, True, 0
        )
        self.failed_handshake = doctor.bridge.HandshakeResult(
            "failed", "initialize-eof", None, None, 0, False, 1
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_recover_rejects_present_tools_without_mutation(self) -> None:
        with mock.patch.object(doctor, "_configuration") as configuration:
            report = doctor.recover_bridge(
                self.root,
                reported_tools="present",
                thread_id="thread-123",
                store=self.store,
            )

        self.assertEqual("invalid-state", report["status"])
        configuration.assert_not_called()

    def test_recover_repairs_broker_once_then_requests_task_recheck(self) -> None:
        with (
            mock.patch.object(doctor, "_configuration", return_value=self.config),
            mock.patch.object(
                doctor, "_project_report", return_value=self.healthy_project
            ) as project,
            mock.patch.object(
                doctor,
                "_handshake",
                side_effect=[self.failed_handshake, self.healthy_handshake],
            ) as handshake,
            mock.patch.object(
                doctor, "_repair_broker", return_value={"status": "repaired"}
            ) as repair,
        ):
            report = doctor.recover_bridge(
                self.root,
                reported_tools="missing",
                thread_id="thread-123",
                store=self.store,
            )

        self.assertEqual("harbor-repaired", report["status"])
        self.assertEqual("recheck-task-tools", report["next_action"])
        self.assertEqual(2, handshake.call_count)
        repair.assert_called_once_with(self.root.resolve())
        project.assert_called_once_with(self.root.resolve(), recover=False)

    def test_recover_runs_one_project_cycle_before_one_handshake(self) -> None:
        with (
            mock.patch.object(doctor, "_configuration", return_value=self.config),
            mock.patch.object(
                doctor,
                "_project_report",
                side_effect=[
                    {"jetbrains_semantic_health": {"status": "stalled"}},
                    self.healthy_project,
                ],
            ) as project,
            mock.patch.object(
                doctor, "_handshake", return_value=self.healthy_handshake
            ) as handshake,
            mock.patch.object(doctor, "_repair_broker") as repair,
        ):
            report = doctor.recover_bridge(
                self.root,
                reported_tools="missing",
                thread_id="thread-123",
                store=self.store,
            )

        self.assertEqual("harbor-repaired", report["status"])
        self.assertEqual(
            [
                mock.call(self.root.resolve(), recover=False),
                mock.call(self.root.resolve(), recover=True),
            ],
            project.call_args_list,
        )
        handshake.assert_called_once_with(self.root.resolve())
        repair.assert_not_called()

    def test_recover_does_not_loop_on_unchanged_failure(self) -> None:
        with (
            mock.patch.object(doctor, "_configuration", return_value=self.config),
            mock.patch.object(
                doctor, "_project_report", return_value=self.healthy_project
            ),
            mock.patch.object(
                doctor, "_handshake", return_value=self.failed_handshake
            ) as handshake,
            mock.patch.object(
                doctor, "_repair_broker", return_value={"status": "protected"}
            ) as repair,
        ):
            report = doctor.recover_bridge(
                self.root,
                reported_tools="missing",
                thread_id="thread-123",
                store=self.store,
            )

        self.assertEqual("handshake-unhealthy", report["status"])
        handshake.assert_called_once_with(self.root.resolve())
        repair.assert_called_once_with(self.root.resolve())

    def test_healthy_missing_tools_creates_and_reuses_restart_incident(self) -> None:
        with (
            mock.patch.object(doctor, "_configuration", return_value=self.config),
            mock.patch.object(
                doctor, "_project_report", return_value=self.healthy_project
            ),
            mock.patch.object(
                doctor, "_handshake", return_value=self.healthy_handshake
            ),
        ):
            first = doctor.recover_bridge(
                self.root,
                reported_tools="missing",
                thread_id="thread-123",
                store=self.store,
            )
            second = doctor.recover_bridge(
                self.root,
                reported_tools="missing",
                thread_id="thread-123",
                store=self.store,
            )

        self.assertEqual("restart-eligible", first["status"])
        self.assertEqual(first["incident"], second["incident"])


if __name__ == "__main__":
    unittest.main()
