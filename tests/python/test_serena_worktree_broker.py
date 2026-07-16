"""Tests for the global Serena worktree broker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
if not BIN_DIR.is_dir(): BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
BROKER_PATH = BIN_DIR / "serena-worktree-broker"
ROOT_ID = "11111111-1111-4111-8111-111111111111"
PARENT_ID = "22222222-2222-4222-8222-222222222222"
CHILD_ID = "33333333-3333-4333-8333-333333333333"
LOADER = importlib.machinery.SourceFileLoader("serena_worktree_broker", str(BROKER_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
broker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = broker
LOADER.exec_module(broker)


class SerenaWorktreeBrokerTests(unittest.TestCase):
    """Exercise identity, state, locking, and conservative cleanup behavior."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        state_dir = Path(self.temporary_directory.name) / "state"
        self.session_dir = Path(self.temporary_directory.name) / "sessions"
        self.path_patches = mock.patch.multiple(
            broker,
            STATE_DIR=state_dir,
            STATE_FILE=state_dir / "services.json",
            LOCK_FILE=state_dir / "services.lock",
            LOG_DIR=state_dir / "logs",
        )
        self.path_patches.start()
        self.session_patch = mock.patch.object(
            broker, "SESSION_DIR", self.session_dir, create=True
        )
        self.session_patch.start()

    def tearDown(self) -> None:
        self.session_patch.stop()
        self.path_patches.stop()
        self.temporary_directory.cleanup()

    def write_session(
        self,
        thread_id: str,
        parent_id: str | None = None,
        *,
        nested_parent_id: str | None = None,
        day: str = "14",
        thread_source: str | None = None,
    ) -> Path:
        directory = self.session_dir / "2026" / "07" / day
        directory.mkdir(parents=True, exist_ok=True)
        nested_parent = parent_id if nested_parent_id is None else nested_parent_id
        payload = {
            "id": thread_id,
            "parent_thread_id": parent_id,
            "source": (
                {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": nested_parent,
                            "depth": 1,
                        }
                    }
                }
                if parent_id is not None
                else {}
            ),
            "thread_source": thread_source or ("subagent" if parent_id else "cli"),
        }
        path = directory / f"rollout-2026-07-{day}T00-00-00-{thread_id}.jsonl"
        path.write_text(
            json.dumps({"type": "session_meta", "payload": payload}) + "\n",
            encoding="utf-8",
        )
        return path

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

    def test_nested_subagent_resolves_to_root_parent(self) -> None:
        self.write_session(ROOT_ID)
        self.write_session(PARENT_ID, ROOT_ID)
        self.write_session(CHILD_ID, PARENT_ID)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": CHILD_ID}, clear=True):
            result = broker._owner_resolution()

        self.assertEqual(ROOT_ID, result.owner_id)
        self.assertEqual(CHILD_ID, result.thread_id)
        self.assertEqual("subagent-lineage", result.source)
        self.assertIsNone(result.reason)

    def test_root_thread_and_explicit_override_resolution(self) -> None:
        self.write_session(ROOT_ID)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": ROOT_ID}, clear=True):
            root = broker._owner_resolution()
        self.assertEqual(
            (ROOT_ID, ROOT_ID, "root-thread", None),
            (root.thread_id, root.owner_id, root.source, root.reason),
        )

        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": CHILD_ID, "WORKSPACE_HARBOR_OWNER_ID": "chosen-owner"},
            clear=True,
        ):
            explicit = broker._owner_resolution()
        self.assertEqual("chosen-owner", explicit.owner_id)
        self.assertEqual("explicit", explicit.source)

    def test_parent_pid_parses_ps_output_and_rejects_invalid_rows(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="  50\n")
        with mock.patch.object(broker.subprocess, "run", return_value=completed):
            self.assertEqual(50, broker._parent_pid(101))

        for result in (
            SimpleNamespace(returncode=1, stdout="50\n"),
            SimpleNamespace(returncode=0, stdout="not-a-pid\n"),
        ):
            with self.subTest(result=result), mock.patch.object(
                broker.subprocess, "run", return_value=result
            ):
                self.assertIsNone(broker._parent_pid(101))

    def test_codex_host_owner_is_stable_across_broker_subprocesses(self) -> None:
        host_command = (
            "/Applications/ChatGPT.app/Contents/Resources/codex "
            "-c features.code_mode_host=true app-server"
        )
        with mock.patch.object(broker, "_parent_pid", return_value=50), mock.patch.object(
            broker, "_process_details", return_value=("host-start", host_command)
        ):
            first = broker._codex_host_identity(101)
            second = broker._codex_host_identity(102)

        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        assert first is not None
        self.assertTrue(first.owner_id.startswith("codex-host-"))

    def test_codex_host_owner_separates_restarted_hosts(self) -> None:
        host_command = "/Applications/ChatGPT.app/Contents/Resources/codex app-server"
        with mock.patch.object(broker, "_parent_pid", return_value=50), mock.patch.object(
            broker,
            "_process_details",
            side_effect=[
                ("first-start", host_command),
                ("second-start", host_command),
            ],
        ):
            first = broker._codex_host_identity(101)
            second = broker._codex_host_identity(102)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertNotEqual(first.owner_id, second.owner_id)

    def test_owner_resolution_uses_validated_codex_host(self) -> None:
        host = broker.CodexHostIdentity(50, "host-start", "codex-host-shared")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            broker, "_codex_host_identity", return_value=host
        ):
            resolution = broker._owner_resolution()

        self.assertEqual("codex-host-shared", resolution.owner_id)
        self.assertEqual("codex-host", resolution.source)
        self.assertIsNone(resolution.thread_id)

    def test_unrecognized_parent_uses_process_fallback(self) -> None:
        cases = (
            "/usr/bin/python codex-helper.py",
            "/tmp/codex app-server",
            "/Applications/ChatGPT.app/Contents/Resources/codex unrelated-command",
        )
        for command in cases:
            with self.subTest(command=command), mock.patch.dict(
                os.environ, {}, clear=True
            ), mock.patch.object(broker, "_parent_pid", return_value=50), mock.patch.object(
                broker, "_process_details", return_value=("host-start", command)
            ):
                resolution = broker._owner_resolution()

            self.assertEqual(f"manual-pid-{os.getpid()}", resolution.owner_id)
            self.assertEqual("process-fallback", resolution.source)

    def test_invalid_lineage_falls_back_to_child(self) -> None:
        self.write_session(
            CHILD_ID,
            PARENT_ID,
            nested_parent_id=ROOT_ID,
        )

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": CHILD_ID}, clear=True):
            result = broker._owner_resolution()

        self.assertEqual(CHILD_ID, result.owner_id)
        self.assertEqual("inconsistent-parent", result.reason)

    def test_missing_malformed_oversized_and_ambiguous_metadata_fail_closed(self) -> None:
        cases: list[tuple[str, str]] = []
        cases.append((CHILD_ID, "missing-session"))

        malformed_id = "44444444-4444-4444-8444-444444444444"
        malformed = self.session_dir / "2026/07/14" / f"rollout-x-{malformed_id}.jsonl"
        malformed.parent.mkdir(parents=True, exist_ok=True)
        malformed.write_text("not-json\n", encoding="utf-8")
        cases.append((malformed_id, "invalid-session-meta"))

        oversized_id = "55555555-5555-4555-8555-555555555555"
        oversized = self.session_dir / "2026/07/14" / f"rollout-x-{oversized_id}.jsonl"
        oversized.write_text("x" * 65_537 + "\n", encoding="utf-8")
        cases.append((oversized_id, "oversized-session-meta"))

        ambiguous_id = "66666666-6666-4666-8666-666666666666"
        self.write_session(ambiguous_id, day="13")
        self.write_session(ambiguous_id, day="14")
        cases.append((ambiguous_id, "ambiguous-session"))

        for thread_id, reason in cases:
            with self.subTest(reason=reason), mock.patch.dict(
                os.environ, {"CODEX_THREAD_ID": thread_id}, clear=True
            ):
                result = broker._owner_resolution()
            self.assertEqual(thread_id, result.owner_id)
            self.assertEqual(reason, result.reason)

    def test_invalid_thread_cycles_and_over_depth_lineage_fail_closed(self) -> None:
        invalid = "not-a-thread-id"
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": invalid}, clear=True):
            invalid_result = broker._owner_resolution()
        self.assertEqual(invalid, invalid_result.owner_id)
        self.assertEqual("invalid-thread-id", invalid_result.reason)

        self.write_session(CHILD_ID, CHILD_ID)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": CHILD_ID}, clear=True):
            self_link = broker._owner_resolution()
        self.assertEqual(CHILD_ID, self_link.owner_id)
        self.assertEqual("lineage-cycle", self_link.reason)

        cycle_child = "77777777-7777-4777-8777-777777777777"
        cycle_parent = "88888888-8888-4888-8888-888888888888"
        self.write_session(cycle_child, cycle_parent)
        self.write_session(cycle_parent, cycle_child)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": cycle_child}, clear=True):
            cycle = broker._owner_resolution()
        self.assertEqual(cycle_child, cycle.owner_id)
        self.assertEqual("lineage-cycle", cycle.reason)

        chain = [f"{index:08x}-0000-4000-8000-{index:012x}" for index in range(10)]
        self.write_session(chain[-1])
        for index in range(9):
            self.write_session(chain[index], chain[index + 1])
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": chain[0]}, clear=True):
            over_depth = broker._owner_resolution()
        self.assertEqual(chain[0], over_depth.owner_id)
        self.assertEqual("lineage-too-deep", over_depth.reason)

    def test_owner_json_reports_resolution_without_session_content(self) -> None:
        resolution = broker.OwnerResolution(CHILD_ID, ROOT_ID, "subagent-lineage")
        with mock.patch.object(
            broker, "_owner_resolution", return_value=resolution
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            status = broker.main(["owner", "--json"])

        self.assertEqual(0, status)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            {
                "owner_id": ROOT_ID,
                "source": "subagent-lineage",
                "thread_id": CHILD_ID,
            },
            payload,
        )
        self.assertNotIn("payload", stdout.getvalue())

    def test_owner_text_includes_only_concise_failure_reason(self) -> None:
        resolution = broker.OwnerResolution(
            CHILD_ID, CHILD_ID, "root-thread", "inconsistent-parent"
        )
        with mock.patch.object(
            broker, "_owner_resolution", return_value=resolution
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            status = broker.main(["owner"])

        self.assertEqual(0, status)
        self.assertEqual(
            f"thread={CHILD_ID} owner={CHILD_ID} source=root-thread "
            "reason=inconsistent-parent\n",
            stdout.getvalue(),
        )

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

    def test_same_host_legacy_lease_migrates_atomically(self) -> None:
        root = Path(self.temporary_directory.name) / "project"; root.mkdir()
        state = {
            "services": {
                "example": {
                    "project_root": str(root),
                    "leases": {
                        "legacy": {
                            "pid": 101,
                            "process_started": "broker-start",
                            "owner_id": "manual-pid-101",
                        }
                    },
                }
            }
        }
        resolution = broker.OwnerResolution(
            None, "codex-host-shared", "codex-host"
        )
        host = broker.CodexHostIdentity(50, "host-start", "codex-host-shared")
        command = f"python3 {BROKER_PATH} connect --project {root}"

        with mock.patch.object(broker, "_prune_dead_leases"), mock.patch.object(
            broker, "_process_details", return_value=("broker-start", command)
        ), mock.patch.object(
            broker, "_codex_host_identity", return_value=host
        ):
            migrated = broker._migrate_legacy_host_leases(
                state, root, resolution
            )

        self.assertTrue(migrated)
        self.assertEqual(
            "codex-host-shared",
            state["services"]["example"]["leases"]["legacy"]["owner_id"],
        )

    def test_mixed_owner_state_does_not_partially_migrate(self) -> None:
        root = Path(self.temporary_directory.name) / "project"; root.mkdir()
        state = {
            "services": {
                "example": {
                    "project_root": str(root),
                    "leases": {
                        "legacy": {
                            "pid": 101,
                            "process_started": "broker-start",
                            "owner_id": "manual-pid-101",
                        },
                        "other": {
                            "pid": 202,
                            "process_started": "other-start",
                            "owner_id": "thread-other",
                        },
                    },
                }
            }
        }
        resolution = broker.OwnerResolution(
            None, "codex-host-shared", "codex-host"
        )
        host = broker.CodexHostIdentity(50, "host-start", "codex-host-shared")
        command = f"python3 {BROKER_PATH} connect --project {root}"

        with mock.patch.object(broker, "_prune_dead_leases"), mock.patch.object(
            broker, "_process_details", return_value=("broker-start", command)
        ), mock.patch.object(
            broker, "_codex_host_identity", return_value=host
        ):
            migrated = broker._migrate_legacy_host_leases(
                state, root, resolution
            )

        self.assertFalse(migrated)
        self.assertEqual(
            "manual-pid-101",
            state["services"]["example"]["leases"]["legacy"]["owner_id"],
        )
        with mock.patch.object(broker, "_prune_dead_leases"):
            with self.assertRaisesRegex(RuntimeError, "owned by another Codex task"):
                broker._assert_root_owner(state, root, resolution.owner_id)

    def test_invalid_legacy_lease_evidence_fails_closed(self) -> None:
        root = Path(self.temporary_directory.name) / "project"; root.mkdir()
        resolution = broker.OwnerResolution(
            None, "codex-host-shared", "codex-host"
        )
        matching_host = broker.CodexHostIdentity(
            50, "host-start", "codex-host-shared"
        )
        different_host = broker.CodexHostIdentity(
            60, "other-start", "codex-host-other"
        )
        valid_command = f"python3 {BROKER_PATH} connect --project {root}"
        cases = (
            ("manual-pid-102", "broker-start", valid_command, matching_host),
            ("manual-pid-101", "reused-start", valid_command, matching_host),
            ("manual-pid-101", "broker-start", "python3 unrelated.py", matching_host),
            ("manual-pid-101", "broker-start", valid_command, different_host),
        )

        for owner_id, live_start, command, host in cases:
            with self.subTest(owner_id=owner_id, command=command, host=host):
                state = {
                    "services": {
                        "example": {
                            "project_root": str(root),
                            "leases": {
                                "legacy": {
                                    "pid": 101,
                                    "process_started": "broker-start",
                                    "owner_id": owner_id,
                                }
                            },
                        }
                    }
                }
                with mock.patch.object(
                    broker, "_prune_dead_leases"
                ), mock.patch.object(
                    broker, "_process_details", return_value=(live_start, command)
                ), mock.patch.object(
                    broker, "_codex_host_identity", return_value=host
                ):
                    migrated = broker._migrate_legacy_host_leases(
                        state, root, resolution
                    )

                self.assertFalse(migrated)
                self.assertEqual(
                    owner_id,
                    state["services"]["example"]["leases"]["legacy"]["owner_id"],
                )

    def test_connect_migrates_before_asserting_root_ownership(self) -> None:
        root = Path(self.temporary_directory.name) / "project"; root.mkdir()
        proxy = Path(self.temporary_directory.name) / "mcp-proxy"
        proxy.write_text("fixture", encoding="utf-8")
        args = SimpleNamespace(
            project=str(root), backend="JetBrains", context="codex", add_mode=()
        )
        resolution = broker.OwnerResolution(
            None, "codex-host-shared", "codex-host"
        )
        events: list[str] = []

        with mock.patch.object(
            broker, "_resolve_project", return_value=root
        ), mock.patch.object(
            broker, "_auto_repair_project_languages"
        ), mock.patch.object(
            broker, "_bootstrap_status", return_value={"status": "ready"}
        ), mock.patch.object(
            broker, "MCP_PROXY", proxy
        ), mock.patch.object(
            broker, "_owner_resolution", return_value=resolution
        ), mock.patch.object(
            broker, "_cleanup_state"
        ), mock.patch.object(
            broker,
            "_migrate_legacy_host_leases",
            side_effect=lambda *_: events.append("migrate"),
        ), mock.patch.object(
            broker,
            "_assert_root_owner",
            side_effect=lambda *_: events.append("assert"),
        ), mock.patch.object(
            broker, "_start_service", side_effect=RuntimeError("stop after ownership")
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after ownership"):
                broker._connect(args)

        self.assertEqual(["migrate", "assert"], events)

    def test_connect_records_ordered_bridge_stages(self) -> None:
        root = Path(self.temporary_directory.name) / "project"; root.mkdir()
        proxy = Path(self.temporary_directory.name) / "mcp-proxy"; proxy.write_text("fixture")
        args = SimpleNamespace(
            project=str(root), backend="JetBrains", context="codex", add_mode=()
        )
        resolution = broker.OwnerResolution(None, "codex-host-shared", "codex-host")
        record = {
            "leases": {}, "port": 24320, "project_root": str(root),
            "pid": 201, "process_started": "service-start",
        }
        service_key = broker._service_key(root, "JetBrains", "codex", ())
        state = {"version": 1, "services": {service_key: record}}
        locked = mock.MagicMock()
        locked.return_value.__enter__.return_value = state
        locked.return_value.__exit__.return_value = False
        completed = mock.Mock(returncode=0)

        with mock.patch.object(broker, "_resolve_project", return_value=root), mock.patch.object(
            broker, "_auto_repair_project_languages"
        ), mock.patch.object(broker, "_bootstrap_status"), mock.patch.object(
            broker, "MCP_PROXY", proxy
        ), mock.patch.object(broker, "_owner_resolution", return_value=resolution), mock.patch.object(
            broker, "_process_details", return_value=("broker-start", "broker")
        ), mock.patch.object(broker, "_locked_state", locked), mock.patch.object(
            broker, "_cleanup_state"
        ), mock.patch.object(broker, "_migrate_legacy_host_leases"), mock.patch.object(
            broker, "_assert_root_owner"
        ), mock.patch.object(broker, "_start_service", return_value=(service_key, record)), mock.patch.object(
            broker.subprocess, "run", return_value=completed
        ), mock.patch.object(broker, "_prune_dead_leases"), mock.patch.object(
            broker.BRIDGE_JOURNAL, "append", return_value=True
        ) as append:
            result = broker._connect(args)

        self.assertEqual(0, result)
        self.assertEqual(
            [
                "project-resolution", "ownership", "service-reused", "lease-inserted",
                "proxy-started", "proxy-exit", "lease-cleanup",
            ],
            [call.args[0].stage for call in append.call_args_list],
        )

    def test_connect_records_project_resolution_failure(self) -> None:
        args = SimpleNamespace(
            project="/missing", backend="JetBrains", context="codex", add_mode=()
        )

        with mock.patch.object(
            broker, "_resolve_project", side_effect=RuntimeError("missing")
        ), mock.patch.object(
            broker.BRIDGE_JOURNAL, "append", return_value=True
        ) as append:
            with self.assertRaisesRegex(RuntimeError, "missing"):
                broker._connect(args)

        self.assertTrue(append.called, "project-resolution failure was not journaled")
        event = append.call_args.args[0]
        self.assertEqual(("project-resolution", "failed", "project-resolution-failed"), (event.stage, event.outcome, event.reason))

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

    def test_repair_root_removes_only_dead_exact_root_record(self) -> None:
        root = Path(self.temporary_directory.name) / "target"; root.mkdir()
        other = Path(self.temporary_directory.name) / "other"; other.mkdir()
        state = {"services": {
            "target": {"project_root": str(root), "leases": {}, "pid": 101, "port": 24320},
            "other": {"project_root": str(other), "leases": {}, "pid": 202, "port": 24321},
        }}

        with mock.patch.object(broker, "_process_is_owned", return_value=False):
            result = broker._repair_root_state(state, root)

        self.assertEqual("repaired", result["status"])
        self.assertNotIn("target", state["services"])
        self.assertIn("other", state["services"])

    def test_repair_root_protects_live_lease(self) -> None:
        root = Path(self.temporary_directory.name) / "target"; root.mkdir()
        state = {"services": {"target": {
            "project_root": str(root),
            "leases": {"live": {"pid": os.getpid(), "process_started": "fixture"}},
            "pid": 101,
            "port": 24320,
        }}}

        with mock.patch.object(broker, "_prune_dead_leases"):
            result = broker._repair_root_state(state, root)

        self.assertEqual({"status": "protected", "reason": "live-lease"}, result)

    def test_repair_root_keeps_healthy_empty_service(self) -> None:
        root = Path(self.temporary_directory.name) / "target"; root.mkdir()
        record = {"project_root": str(root), "leases": {}, "pid": 101, "port": 24320}
        state = {"services": {"target": record}}

        with mock.patch.object(broker, "_process_is_owned", return_value=True), mock.patch.object(
            broker, "_tcp_ready", return_value=True
        ):
            result = broker._repair_root_state(state, root)

        self.assertEqual({"status": "unchanged", "reason": "healthy-service"}, result)
        self.assertIs(record, state["services"]["target"])

    def test_repair_root_protects_unstoppable_owned_service(self) -> None:
        root = Path(self.temporary_directory.name) / "target"; root.mkdir()
        record = {"project_root": str(root), "leases": {}, "pid": 101, "port": 24320}
        state = {"services": {"target": record}}

        with mock.patch.object(broker, "_process_is_owned", return_value=True), mock.patch.object(
            broker, "_tcp_ready", return_value=False
        ), mock.patch.object(broker, "_stop_owned_service", return_value=False):
            result = broker._repair_root_state(state, root)

        self.assertEqual({"status": "protected", "reason": "stop-failed"}, result)
        self.assertIs(record, state["services"]["target"])

    def test_port_allocator_skips_listening_port(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            occupied = listener.getsockname()[1]
            with mock.patch.multiple(broker, PORT_FIRST=occupied, PORT_LAST=occupied + 1):
                selected = broker._free_port(set())

        self.assertEqual(selected, occupied + 1)

    def test_startup_identity_failure_terminates_untracked_service(self) -> None:
        project_root = Path(self.temporary_directory.name) / "project"
        project_root.mkdir()
        broker.LOG_DIR.mkdir(parents=True)
        process = mock.Mock(pid=43210)
        process.poll.return_value = None
        state = {"services": {}}

        with mock.patch.object(
            broker.subprocess, "Popen", return_value=process
        ), mock.patch.object(
            broker, "_process_details", return_value=None
        ), mock.patch.object(
            broker, "_free_port", return_value=42123
        ), mock.patch.object(
            broker, "START_TIMEOUT_SECONDS", 0
        ), mock.patch.object(
            broker.time, "sleep"
        ), mock.patch.object(
            broker.os, "killpg"
        ) as killpg:
            with self.assertRaisesRegex(RuntimeError, "exited during startup"):
                broker._start_service(state, project_root, "LSP", "codex")

        killpg.assert_called_once_with(process.pid, broker.signal.SIGTERM)
        self.assertEqual({}, state["services"])

    def test_startup_wrong_identity_terminates_untracked_service(self) -> None:
        project_root = Path(self.temporary_directory.name) / "project"
        project_root.mkdir()
        broker.LOG_DIR.mkdir(parents=True)
        process = mock.Mock(pid=43210)
        process.poll.return_value = None
        state = {"services": {}}

        with mock.patch.object(
            broker.subprocess, "Popen", return_value=process
        ), mock.patch.object(
            broker, "_process_details", return_value=("start", "/usr/bin/foreign")
        ), mock.patch.object(
            broker, "_free_port", return_value=42123
        ), mock.patch.object(
            broker, "START_TIMEOUT_SECONDS", 0
        ), mock.patch.object(
            broker.time, "sleep"
        ), mock.patch.object(
            broker.os, "killpg"
        ) as killpg:
            with self.assertRaisesRegex(RuntimeError, "identity.*startup"):
                broker._start_service(state, project_root, "LSP", "codex")

        killpg.assert_called_once_with(process.pid, broker.signal.SIGTERM)
        self.assertEqual({}, state["services"])

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

    def test_intellij_launcher_timeout_exceeds_bootstrap_and_ready_timeouts(self) -> None:
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        launcher = Path(self.temporary_directory.name) / "open-intellij"
        launcher.write_text("fixture", encoding="utf-8")
        with mock.patch.object(broker, "INTELLIJ_LAUNCHER", launcher), mock.patch.object(
            broker, "INTELLIJ_LAUNCH_TIMEOUT_SECONDS", 1
        ), mock.patch.object(
            broker.subprocess, "run", return_value=completed
        ) as run:
            broker._open_intellij(Path("/tmp/example"))

        self.assertGreaterEqual(run.call_args.kwargs["timeout"], 2050)

    def test_untracked_service_cleanup_escalates_and_reaps(self) -> None:
        process = mock.Mock(pid=43210)
        process.poll.side_effect = [None, None]
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["serena"], 5),
            0,
        ]
        with mock.patch.object(broker.os, "killpg") as killpg:
            broker._stop_spawned_service(process)

        self.assertEqual(
            [
                mock.call(process.pid, broker.signal.SIGTERM),
                mock.call(process.pid, broker.signal.SIGKILL),
            ],
            killpg.call_args_list,
        )
        self.assertEqual(2, process.wait.call_count)

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
