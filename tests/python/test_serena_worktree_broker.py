"""Tests for the global Serena worktree broker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
if not BIN_DIR.is_dir(): BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
BROKER_PATH = BIN_DIR / "serena-worktree-broker"
LOADER = importlib.machinery.SourceFileLoader("serena_worktree_broker", str(BROKER_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
broker = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(broker)


class SerenaWorktreeBrokerTests(unittest.TestCase):
    """Exercise identity, state, locking, and conservative cleanup behavior."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        state_dir = Path(self.temporary_directory.name) / "state"
        self.path_patches = mock.patch.multiple(
            broker,
            STATE_DIR=state_dir,
            STATE_FILE=state_dir / "services.json",
            LOCK_FILE=state_dir / "services.lock",
            LOG_DIR=state_dir / "logs",
        )
        self.path_patches.start()

    def tearDown(self) -> None:
        self.path_patches.stop()
        self.temporary_directory.cleanup()

    def test_service_key_uses_canonical_project_root(self) -> None:
        root = Path(self.temporary_directory.name) / "root"
        alias = Path(self.temporary_directory.name) / "alias"
        root.mkdir()
        alias.symlink_to(root, target_is_directory=True)

        direct = broker._service_key(root, "JetBrains", "codex")
        through_alias = broker._service_key(alias, "JetBrains", "codex")

        self.assertEqual(direct, through_alias)

    def test_service_key_separates_added_modes(self) -> None:
        root = Path(self.temporary_directory.name) / "root"
        root.mkdir()

        base = broker._service_key(root, "JetBrains", "codex")
        queryable = broker._service_key(
            root, "JetBrains", "codex", ("query-projects",)
        )

        self.assertNotEqual(base, queryable)

    def test_owner_identity_prefers_explicit_thread_group(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"WORKSPACE_HARBOR_OWNER_ID": "parent-thread", "CODEX_THREAD_ID": "child-thread"},
            clear=True,
        ):
            self.assertEqual("parent-thread", broker._owner_id())
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "direct-thread"}, clear=True):
            self.assertEqual("direct-thread", broker._owner_id())

    def test_root_ownership_rejects_another_thread_but_allows_other_roots(self) -> None:
        first = Path(self.temporary_directory.name) / "first"; first.mkdir()
        second = Path(self.temporary_directory.name) / "second"; second.mkdir()
        third = Path(self.temporary_directory.name) / "third"; third.mkdir()
        live = {
            "pid": os.getpid(),
            "process_started": broker._process_details(os.getpid())[0],
            "owner_id": "thread-a",
        }
        state = {
            "services": {
                "first": {"project_root": str(first), "leases": {"one": live.copy()}},
                "second": {"project_root": str(second), "leases": {"two": live.copy()}},
            }
        }
        self.assertEqual({"thread-a"}, broker._root_owners(state, first))
        broker._assert_root_owner(state, first, "thread-a")
        with self.assertRaisesRegex(RuntimeError, "owned by another Codex task"):
            broker._assert_root_owner(state, first, "thread-b")
        with self.assertRaisesRegex(RuntimeError, "owned by another Codex task"):
            broker._assert_root_owner(state, second, "thread-b")
        broker._assert_root_owner(state, third, "thread-b")

    def test_locked_state_round_trips_with_private_permissions(self) -> None:
        with broker._locked_state() as state:
            state["services"]["example"] = {"pid": 123}

        with broker._locked_state() as state:
            self.assertEqual(state["services"]["example"]["pid"], 123)

        self.assertEqual(oct(broker.STATE_DIR.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(broker.STATE_FILE.stat().st_mode & 0o777), "0o600")

    def test_dead_lease_is_removed_and_service_becomes_idle(self) -> None:
        record = {
            "leases": {
                "dead": {
                    "pid": 999999,
                    "process_started": "never",
                }
            },
            "idle_since": None,
        }

        broker._prune_dead_leases(record)

        self.assertEqual(record["leases"], {})
        self.assertIsNotNone(record["idle_since"])

    def test_leases_snapshot_prunes_and_aggregates_by_canonical_root(self) -> None:
        root = Path(self.temporary_directory.name) / "project"; root.mkdir()
        alias = Path(self.temporary_directory.name) / "alias"; alias.symlink_to(root, target_is_directory=True)
        live = {"pid": os.getpid(), "process_started": broker._process_details(os.getpid())[0]}
        with broker._locked_state() as state:
            state["services"] = {
                "one": {"project_root": str(root), "backend": "JetBrains", "leases": {"live": live}, "idle_since": None, "last_used_at": "2026-01-01T00:00:00Z"},
                "two": {"project_root": str(alias), "backend": "JetBrains", "leases": {"dead": {"pid": 999999, "process_started": "never"}}, "idle_since": None, "last_used_at": "2026-01-02T00:00:00Z"},
            }
        with mock.patch("sys.stdout") as stdout:
            broker._leases(SimpleNamespace(json=True))
        payload = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
        self.assertEqual([str(root.resolve())], list(payload))
        self.assertEqual({"project_root", "backend", "live_leases", "idle_since", "last_used_at"}, set(payload[str(root.resolve())]))
        self.assertEqual(1, payload[str(root.resolve())]["live_leases"])
        self.assertEqual("2026-01-02T00:00:00Z", payload[str(root.resolve())]["last_used_at"])
        with broker._locked_state() as state:
            self.assertEqual({}, state["services"]["two"]["leases"])

    def test_cleanup_drops_unowned_state_without_signalling_process(self) -> None:
        state = {"version": 1, "services": {"external": {"pid": os.getpid(), "leases": {}}}}
        with mock.patch.object(broker, "_process_is_owned", return_value=False), mock.patch.object(
            broker, "_stop_owned_service"
        ) as stop:
            actions = broker._cleanup_state(state, idle_seconds=0)

        self.assertEqual(state["services"], {})
        self.assertEqual(actions, ["removed stale state external"])
        stop.assert_not_called()

    def test_cleanup_stops_only_owned_idle_service(self) -> None:
        old_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        record = {"pid": 123, "leases": {}, "idle_since": old_time}
        state = {"version": 1, "services": {"owned": record}}
        with mock.patch.object(broker, "_process_is_owned", return_value=True), mock.patch.object(
            broker, "_stop_owned_service", return_value=True
        ) as stop:
            actions = broker._cleanup_state(state, idle_seconds=60)

        self.assertEqual(state["services"], {})
        self.assertEqual(actions, ["stopped idle service owned"])
        stop.assert_called_once_with(record)

    def test_port_allocator_skips_listening_port(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            occupied = listener.getsockname()[1]
            with mock.patch.multiple(broker, PORT_FIRST=occupied, PORT_LAST=occupied + 1):
                selected = broker._free_port(set())

        self.assertEqual(selected, occupied + 1)

    def test_language_auto_repair_invokes_project_doctor(self) -> None:
        completed = mock.Mock(returncode=0, stdout="{}", stderr="")
        doctor_path = Path(self.temporary_directory.name) / "doctor"
        doctor_path.write_text("fixture", encoding="utf-8")
        with mock.patch.object(broker, "PROJECT_DOCTOR", doctor_path), mock.patch.object(
            broker.subprocess, "run", return_value=completed
        ) as run:
            broker._auto_repair_project_languages(Path("/tmp/example"))

        command = run.call_args.args[0]
        self.assertIn("--repair-languages", command)
        self.assertIn("--json", command)

    def test_intellij_launcher_timeout_exceeds_helper_ready_timeout(self) -> None:
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        launcher = Path(self.temporary_directory.name) / "open-intellij"
        launcher.write_text("fixture", encoding="utf-8")
        with mock.patch.object(broker, "INTELLIJ_LAUNCHER", launcher), mock.patch.object(
            broker.subprocess, "run", return_value=completed
        ) as run:
            broker._open_intellij(Path("/tmp/example"))

        self.assertGreaterEqual(run.call_args.kwargs["timeout"], 150)

    def test_language_auto_repair_failure_blocks_service_startup(self) -> None:
        completed = mock.Mock(returncode=1, stdout="", stderr="invalid config")
        doctor_path = Path(self.temporary_directory.name) / "doctor"
        doctor_path.write_text("fixture", encoding="utf-8")
        with mock.patch.object(broker, "PROJECT_DOCTOR", doctor_path), mock.patch.object(
            broker.subprocess, "run", return_value=completed
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid config"):
                broker._auto_repair_project_languages(Path("/tmp/example"))

    def test_jetbrains_connection_auto_repairs_detected_languages(self) -> None:
        """The primary backend keeps project language coverage current too."""
        project_root = Path(self.temporary_directory.name) / "project"
        project_root.mkdir()
        missing_proxy = Path(self.temporary_directory.name) / "missing-mcp-proxy"
        args = SimpleNamespace(
            project=str(project_root),
            backend="JetBrains",
            context="codex",
            add_mode=(),
        )
        with mock.patch.object(
            broker, "_resolve_project", return_value=project_root
        ), mock.patch.object(
            broker, "_auto_repair_project_languages"
        ) as repair, mock.patch.object(
            broker, "_bootstrap_status", return_value={"status": "pending"}
        ) as bootstrap_status, mock.patch.object(
            broker, "MCP_PROXY", missing_proxy
        ):
            with self.assertRaisesRegex(RuntimeError, "mcp-proxy not found"):
                broker._connect(args)

        repair.assert_called_once_with(project_root)
        bootstrap_status.assert_called_once_with(project_root)

    def test_bootstrap_probe_is_status_only_and_accepts_decision_status(self) -> None:
        helper = Path(self.temporary_directory.name) / "bootstrap"; helper.write_text("fixture"); helper.chmod(0o755)
        completed = mock.Mock(returncode=3, stdout=json.dumps({"status": "needs-decision"}), stderr="")
        with mock.patch.object(broker, "BOOTSTRAP", helper), mock.patch.object(
            broker.subprocess, "run", return_value=completed
        ) as run:
            result = broker._bootstrap_status(Path("/tmp/example"))
        self.assertEqual("needs-decision", result["status"])
        command = run.call_args.args[0]
        self.assertIn("status", command)
        self.assertNotIn("run", command)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 5)

    def test_bootstrap_probe_rejects_invalid_or_malformed_results(self) -> None:
        helper = Path(self.temporary_directory.name) / "bootstrap"; helper.write_text("fixture"); helper.chmod(0o755)
        cases = [
            mock.Mock(returncode=2, stdout=json.dumps({"status": "invalid"}), stderr="bad config"),
            mock.Mock(returncode=0, stdout="not-json", stderr=""),
            mock.Mock(returncode=0, stdout=json.dumps({"status": "surprise"}), stderr=""),
        ]
        for completed in cases:
            with self.subTest(stdout=completed.stdout), mock.patch.object(broker, "BOOTSTRAP", helper), mock.patch.object(
                broker.subprocess, "run", return_value=completed
            ):
                with self.assertRaisesRegex(RuntimeError, "bootstrap"):
                    broker._bootstrap_status(Path("/tmp/example"))


if __name__ == "__main__":
    unittest.main()
