"""Tests for the detached, signal-free Codex relauncher."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
RELAUNCHER_PATH = BIN_DIR / "workspace-harbor-codex-relauncher"
LOADER = importlib.machinery.SourceFileLoader(
    "workspace_harbor_codex_relauncher", str(RELAUNCHER_PATH)
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
relauncher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = relauncher
LOADER.exec_module(relauncher)


CURRENT = "11111111-1111-4111-8111-111111111111"
INCIDENT = "a" * 32
EXECUTABLE = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"


class RelaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary_directory.name)
        self.store = relauncher.bridge.IncidentStore(self.state / "incidents")
        root = self.state / "repo"
        root.mkdir()
        incident = self.store.create(
            root, CURRENT, "restart-eligible", "task-tools-missing"
        )
        self.incident = self.store.transition(
            incident.id,
            "restart-eligible",
            "restart-prepared",
            restart_attempted=True,
            heartbeat_id="workspace-harbor-bridge-incident",
        )
        self.old = relauncher.codex.CodexProcessIdentity(
            123, "old-start", EXECUTABLE, "com.openai.codex"
        )
        self.new = relauncher.codex.CodexProcessIdentity(
            456, "new-start", EXECUTABLE, "com.openai.codex"
        )
        self.checkpoint = relauncher.codex.RelaunchCheckpoint(
            incident_id=self.incident.id,
            incident_store=str(self.store.state_dir),
            root=self.incident.root,
            thread_id=CURRENT,
            heartbeat_id="workspace-harbor-bridge-incident",
            attestation_nonce="b" * 32,
            doctor_pid=999,
            app_identity=self.old,
            created_at=datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc).isoformat(),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def completed(command, returncode=0):
        return subprocess.CompletedProcess(command, returncode, "", "")

    def test_graceful_relaunch_uses_exact_commands_and_transitions(self) -> None:
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            return self.completed(command)

        with (
            mock.patch.object(relauncher.codex, "process_exists", return_value=False),
            mock.patch.object(
                relauncher.codex, "identity_matches", side_effect=[True, False]
            ),
            mock.patch.object(
                relauncher.codex,
                "find_codex_app_identity",
                side_effect=[None, self.new],
            ),
            mock.patch.object(relauncher.subprocess, "run", side_effect=run),
            mock.patch.object(relauncher.time, "sleep"),
        ):
            result = relauncher.perform_relaunch(
                self.checkpoint, self.store, wait_iterations=2
            )

        self.assertEqual("resume-pending", result["status"])
        self.assertEqual(
            [
                [
                    "/usr/bin/osascript",
                    "-e",
                    'tell application id "com.openai.codex" to quit',
                ],
                ["/usr/bin/open", "-b", "com.openai.codex"],
                ["/usr/bin/open", f"codex://threads/{CURRENT}"],
            ],
            commands,
        )
        rendered = " ".join(part for command in commands for part in command)
        for forbidden in ("kill", "pkill", "killall", "-KILL"):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual("resume-pending", self.store.load(self.incident.id).state)

    def test_app_already_gone_skips_quit_and_relaunches(self) -> None:
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            return self.completed(command)

        with (
            mock.patch.object(relauncher.codex, "process_exists", return_value=False),
            mock.patch.object(relauncher.codex, "identity_matches", return_value=False),
            mock.patch.object(
                relauncher.codex,
                "find_codex_app_identity",
                side_effect=[None, self.new],
            ),
            mock.patch.object(relauncher.subprocess, "run", side_effect=run),
        ):
            result = relauncher.perform_relaunch(
                self.checkpoint, self.store, wait_iterations=1
            )

        self.assertEqual("resume-pending", result["status"])
        self.assertNotIn("/usr/bin/osascript", [command[0] for command in commands])

    def test_quit_timeout_closes_blocked_without_forcing(self) -> None:
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            return self.completed(command)

        with (
            mock.patch.object(relauncher.codex, "process_exists", return_value=False),
            mock.patch.object(relauncher.codex, "identity_matches", return_value=True),
            mock.patch.object(relauncher.subprocess, "run", side_effect=run),
            mock.patch.object(relauncher.time, "sleep"),
        ):
            result = relauncher.perform_relaunch(
                self.checkpoint, self.store, wait_iterations=2
            )

        self.assertEqual("codex-restart-incomplete", result["reason"])
        self.assertEqual("closed-blocked", self.store.load(self.incident.id).state)
        self.assertEqual(1, len(commands))

    def test_launch_failure_closes_blocked(self) -> None:
        with (
            mock.patch.object(relauncher.codex, "process_exists", return_value=False),
            mock.patch.object(relauncher.codex, "identity_matches", return_value=False),
            mock.patch.object(
                relauncher.codex, "find_codex_app_identity", return_value=None
            ),
            mock.patch.object(
                relauncher.subprocess,
                "run",
                return_value=self.completed([], 1),
            ),
        ):
            result = relauncher.perform_relaunch(
                self.checkpoint, self.store, wait_iterations=1
            )

        self.assertEqual("codex-relaunch-failed", result["reason"])

    def test_readiness_timeout_closes_blocked(self) -> None:
        with (
            mock.patch.object(relauncher.codex, "process_exists", return_value=False),
            mock.patch.object(relauncher.codex, "identity_matches", return_value=False),
            mock.patch.object(
                relauncher.codex, "find_codex_app_identity", return_value=None
            ),
            mock.patch.object(
                relauncher.subprocess,
                "run",
                return_value=self.completed([], 0),
            ),
            mock.patch.object(relauncher.time, "sleep"),
        ):
            result = relauncher.perform_relaunch(
                self.checkpoint, self.store, wait_iterations=1
            )

        self.assertEqual("codex-readiness-timeout", result["reason"])


if __name__ == "__main__":
    unittest.main()
