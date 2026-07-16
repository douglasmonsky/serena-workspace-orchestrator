"""Tests for guarded Codex restart attestations and local state."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
MODULE_PATH = BIN_DIR / "workspace_harbor_codex.py"
SPEC = importlib.util.spec_from_file_location("workspace_harbor_codex", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
codex = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = codex
SPEC.loader.exec_module(codex)


CURRENT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
INCIDENT = "a" * 32
NOW = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
EXECUTABLE = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"


class RestartAttestationTests(unittest.TestCase):
    def heartbeat(self, **overrides):
        value = {
            "id": "workspace-harbor-bridge-incident",
            "target_thread_id": CURRENT,
            "enabled": True,
            "next_run": NOW + timedelta(seconds=90),
            "incident": INCIDENT,
        }
        value.update(overrides)
        return value

    def build(self, **overrides):
        values = {
            "current_thread": CURRENT,
            "host_id": "local",
            "active_threads": [CURRENT],
            "unavailable_hosts": [],
            "observed_at": NOW,
            "heartbeat": self.heartbeat(),
            "incident": INCIDENT,
            "active_children": [],
            "unknown_threads": [],
            "nonce": "b" * 32,
        }
        values.update(overrides)
        return codex.build_restart_attestation(**values)

    def test_one_current_task_and_valid_heartbeat_succeeds(self) -> None:
        attestation = self.build()

        self.assertEqual((CURRENT,), attestation.active_threads)
        self.assertEqual(0, attestation.unavailable_host_count)
        rendered = json.dumps(asdict(attestation))
        for forbidden in ("prompt", "preview", "title", "message"):
            self.assertNotIn(forbidden, rendered)

    def test_exclusivity_and_heartbeat_failures_use_stable_reasons(self) -> None:
        cases = {
            "second-active-task": {"active_threads": [CURRENT, OTHER]},
            "active-child": {"active_children": [OTHER]},
            "unavailable-host": {"unavailable_hosts": ["remote"]},
            "unknown-task-status": {"unknown_threads": [OTHER]},
            "current-task-missing": {"active_threads": [OTHER]},
            "duplicate-task-id": {"active_threads": [CURRENT, CURRENT]},
            "heartbeat-target-mismatch": {
                "heartbeat": self.heartbeat(target_thread_id=OTHER)
            },
            "heartbeat-disabled": {"heartbeat": self.heartbeat(enabled=False)},
            "incident-mismatch": {"heartbeat": self.heartbeat(incident="c" * 32)},
            "heartbeat-too-soon": {
                "heartbeat": self.heartbeat(next_run=NOW + timedelta(seconds=10))
            },
            "heartbeat-too-late": {
                "heartbeat": self.heartbeat(next_run=NOW + timedelta(seconds=181))
            },
        }
        for reason, overrides in cases.items():
            with self.subTest(reason=reason), self.assertRaises(codex.AttestationError) as caught:
                self.build(**overrides)
            self.assertEqual(reason, caught.exception.reason)

    def test_attestation_expires_after_thirty_seconds(self) -> None:
        attestation = self.build()

        with self.assertRaises(codex.AttestationError) as caught:
            codex.validate_restart_attestation(
                attestation,
                current_thread=CURRENT,
                incident=INCIDENT,
                now=NOW + timedelta(seconds=31),
            )

        self.assertEqual("attestation-expired", caught.exception.reason)

    def test_private_attestation_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "attestation.json"
            codex.write_private_dataclass(path, self.build())
            loaded = codex.load_restart_attestation(path)

            self.assertEqual(self.build(), loaded)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)


class ProcessIdentityTests(unittest.TestCase):
    def test_find_requires_one_exact_app_process_and_bundle(self) -> None:
        expected = codex.CodexProcessIdentity(
            pid=123,
            started="Wed Jul 15 20:00:00 2026",
            executable=EXECUTABLE,
            bundle_id="com.openai.codex",
        )
        with (
            mock.patch.object(codex, "_candidate_pids", return_value=[123]),
            mock.patch.object(
                codex,
                "_read_process",
                return_value=(expected.started, EXECUTABLE),
            ),
            mock.patch.object(
                codex, "_bundle_identifier", return_value="com.openai.codex"
            ),
        ):
            result = codex.find_codex_app_identity()

        self.assertEqual(expected, result)

    def test_find_rejects_duplicate_or_lookalike_processes(self) -> None:
        with mock.patch.object(codex, "_candidate_pids", return_value=[123, 456]):
            self.assertIsNone(codex.find_codex_app_identity())
        with (
            mock.patch.object(codex, "_candidate_pids", return_value=[123]),
            mock.patch.object(
                codex,
                "_read_process",
                return_value=("started", "/tmp/ChatGPT"),
            ),
            mock.patch.object(
                codex, "_bundle_identifier", return_value="com.openai.codex"
            ),
        ):
            self.assertIsNone(codex.find_codex_app_identity())

    def test_identity_match_rejects_pid_reuse_or_changed_executable(self) -> None:
        identity = codex.CodexProcessIdentity(
            123, "original", EXECUTABLE, "com.openai.codex"
        )
        with mock.patch.object(
            codex, "_read_process", return_value=("changed", EXECUTABLE)
        ), mock.patch.object(
            codex, "_bundle_identifier", return_value="com.openai.codex"
        ):
            self.assertFalse(codex.identity_matches(identity))
        with mock.patch.object(
            codex, "_read_process", return_value=("original", "/tmp/ChatGPT")
        ), mock.patch.object(
            codex, "_bundle_identifier", return_value="com.openai.codex"
        ):
            self.assertFalse(codex.identity_matches(identity))


class RestartPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary_directory.name)
        self.policy = codex.RestartPolicyStore(self.state / "config.json")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_policy_defaults_disabled_and_can_always_disable(self) -> None:
        self.assertFalse(self.policy.load()["automatic_codex_restart"])
        self.policy.write(True)
        self.assertTrue(self.policy.load()["automatic_codex_restart"])
        self.policy.write(False)
        self.assertFalse(self.policy.load()["automatic_codex_restart"])


if __name__ == "__main__":
    unittest.main()
