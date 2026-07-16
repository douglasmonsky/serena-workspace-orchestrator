"""Tests for privacy-safe Workspace Harbor bridge diagnostics."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
MODULE_PATH = BIN_DIR / "workspace_harbor_bridge.py"
SPEC = importlib.util.spec_from_file_location("workspace_harbor_bridge", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class BridgeJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary_directory.name) / "bridge-state"
        self.journal = bridge.BridgeJournal(self.state, max_bytes=700, backups=2)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def event(stage: str = "project-resolution", *, attempt: str = "a" * 32):
        return bridge.BridgeEvent(
            attempt_id=attempt,
            timestamp="2026-07-15T20:00:00+00:00",
            root_digest="b" * 24,
            service_key="c" * 20,
            owner_source="codex-host",
            stage=stage,
            outcome="ok",
            reason=None,
            duration_ms=1,
        )

    def test_append_stores_only_allowlisted_fields(self) -> None:
        event = dataclasses.replace(
            self.event("proxy-exit"),
            outcome="failed",
            reason="proxy-exit-1",
            duration_ms=41,
        )

        self.assertTrue(self.journal.append(event))

        payload = json.loads(self.journal.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(dataclasses.asdict(event), payload)
        rendered = json.dumps(payload)
        for forbidden in ("prompt", "arguments", "environment", "source_text"):
            self.assertNotIn(forbidden, rendered)

    def test_append_failure_does_not_raise(self) -> None:
        self.journal.state_dir.write_text("not a directory", encoding="utf-8")

        self.assertFalse(self.journal.append(self.event()))

    def test_rotation_keeps_current_plus_two_backups(self) -> None:
        for index in range(12):
            self.assertTrue(self.journal.append(self.event(attempt=f"{index:032x}")))

        self.assertTrue(self.journal.path.exists())
        self.assertTrue(self.journal.path.with_suffix(".jsonl.1").exists())
        self.assertTrue(self.journal.path.with_suffix(".jsonl.2").exists())
        self.assertFalse(self.journal.path.with_suffix(".jsonl.3").exists())

    def test_recent_filters_by_root_digest(self) -> None:
        first = self.event(attempt="1" * 32)
        other = dataclasses.replace(
            self.event(attempt="2" * 32), root_digest="d" * 24
        )
        latest = self.event("ownership", attempt="3" * 32)
        for event in (first, other, latest):
            self.assertTrue(self.journal.append(event))

        result = self.journal.recent(Path("/fixture/root"), limit=10, root_digest_override="b" * 24)

        self.assertEqual([dataclasses.asdict(first), dataclasses.asdict(latest)], result)

    def test_reason_rejects_newlines_and_oversized_values(self) -> None:
        newline = dataclasses.replace(self.event(), outcome="failed", reason="bad\nvalue")
        oversized = dataclasses.replace(self.event(), outcome="failed", reason="x" * 97)
        free_form = dataclasses.replace(
            self.event(), outcome="failed", reason="source contained private text"
        )

        self.assertFalse(self.journal.append(newline))
        self.assertFalse(self.journal.append(oversized))
        self.assertFalse(self.journal.append(free_form))
        self.assertFalse(self.journal.path.exists())


class ConfigCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = Path("/fixture/serena-worktree-broker")
        self.output = """serena
  enabled: true
  transport: stdio
  command: /fixture/serena-worktree-broker
  args: connect --context=codex --backend=JetBrains --add-mode=query-projects
  cwd: -
  env: SECRET=*****
"""

    def test_config_check_accepts_exact_enabled_broker(self) -> None:
        result = bridge.parse_codex_mcp_get(self.output, self.broker)

        self.assertEqual(("healthy", "configured"), (result.status, result.reason))
        self.assertEqual(str(self.broker), result.command)
        self.assertNotIn("SECRET", dataclasses.asdict(result).values())

    def test_config_check_rejects_wrong_backend(self) -> None:
        result = bridge.parse_codex_mcp_get(
            self.output.replace("JetBrains", "LSP"), self.broker
        )

        self.assertEqual(("invalid", "wrong-args"), (result.status, result.reason))

    def test_config_check_rejects_duplicate_required_argument(self) -> None:
        duplicated = self.output.replace(
            "--backend=JetBrains", "--backend=JetBrains --backend=JetBrains"
        )

        result = bridge.parse_codex_mcp_get(duplicated, self.broker)

        self.assertEqual(("invalid", "wrong-args"), (result.status, result.reason))

    def test_config_check_rejects_disabled_or_wrong_command(self) -> None:
        disabled = bridge.parse_codex_mcp_get(
            self.output.replace("enabled: true", "enabled: false"), self.broker
        )
        wrong = bridge.parse_codex_mcp_get(
            self.output.replace(str(self.broker), "/fixture/other"), self.broker
        )

        self.assertEqual("disabled", disabled.reason)
        self.assertEqual("wrong-command", wrong.reason)

    def test_config_check_rejects_missing_required_fields(self) -> None:
        result = bridge.parse_codex_mcp_get("serena\n  enabled: true\n", self.broker)

        self.assertEqual(("invalid", "missing-fields"), (result.status, result.reason))

    def test_config_check_reports_cli_nonzero_without_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            cli = Path(temporary_directory) / "codex"
            cli.write_text("#!/bin/sh\necho private-details >&2\nexit 7\n", encoding="utf-8")
            cli.chmod(0o755)

            result = bridge.check_codex_serena_config(cli, self.broker)

        self.assertEqual(
            ("invalid", "serena-config-missing", None),
            (result.status, result.reason, result.command),
        )

    def test_config_check_reports_cli_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            cli = Path(temporary_directory) / "codex"
            cli.write_text("#!/bin/sh\nsleep 2\n", encoding="utf-8")
            cli.chmod(0o755)

            result = bridge.check_codex_serena_config(
                cli, self.broker, timeout_seconds=0.1
            )

        self.assertEqual(
            ("unavailable", "codex-cli-timeout", None),
            (result.status, result.reason, result.command),
        )


class HandshakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.root = self.base / "project"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_broker(self, response_body: str) -> Path:
        path = self.base / "broker"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            + textwrap.dedent(response_body),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def test_handshake_initializes_and_lists_serena_tools(self) -> None:
        broker_path = self.write_broker(
            """
            methods = []
            for line in sys.stdin:
                message = json.loads(line)
                methods.append(message.get("method"))
                if message.get("id") == 1:
                    print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "fixture"}}}), flush=True)
                if message.get("id") == 2:
                    if methods != ["initialize", "notifications/initialized", "tools/list"]:
                        sys.exit(9)
                    print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "initial_instructions", "description": "discard me"}]}}), flush=True)
                    break
            """
        )

        result = bridge.run_handshake(self.root, broker_path, timeout_seconds=2)

        self.assertEqual("healthy", result.status)
        self.assertEqual("handshake-complete", result.reason)
        self.assertEqual(1, result.tool_count)
        self.assertTrue(result.expected_tool_found)
        self.assertEqual(0, result.proxy_exit)

    def test_handshake_mirrors_desktop_launcher_without_task_owner_env(self) -> None:
        broker_path = self.write_broker(
            """
            if os.environ.get("CODEX_THREAD_ID") or os.environ.get("WORKSPACE_HARBOR_OWNER_ID"):
                sys.exit(9)
            for line in sys.stdin:
                message = json.loads(line)
                if message.get("id") == 1:
                    print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}), flush=True)
                if message.get("id") == 2:
                    print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "initial_instructions"}]}}), flush=True)
                    break
            """
        )

        with mock.patch.dict(
            os.environ,
            {
                "CODEX_THREAD_ID": "11111111-1111-4111-8111-111111111111",
                "WORKSPACE_HARBOR_OWNER_ID": "custom-owner",
            },
        ):
            result = bridge.run_handshake(self.root, broker_path, timeout_seconds=1)

        self.assertEqual(("healthy", "handshake-complete"), (result.status, result.reason))

    def test_handshake_rejects_initialize_error(self) -> None:
        broker_path = self.write_broker(
            """
            message = json.loads(sys.stdin.readline())
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -1, "message": "private details"}}), flush=True)
            """
        )

        result = bridge.run_handshake(self.root, broker_path, timeout_seconds=1)

        self.assertEqual(("failed", "initialize-error"), (result.status, result.reason))

    def test_handshake_rejects_malformed_json(self) -> None:
        broker_path = self.write_broker(
            """
            sys.stdin.readline()
            print("not-json", flush=True)
            """
        )

        result = bridge.run_handshake(self.root, broker_path, timeout_seconds=1)

        self.assertEqual(("failed", "protocol-error"), (result.status, result.reason))

    def test_handshake_reports_early_eof(self) -> None:
        broker_path = self.write_broker(
            """
            sys.stdin.readline()
            """
        )

        result = bridge.run_handshake(self.root, broker_path, timeout_seconds=1)

        self.assertEqual(("failed", "initialize-eof"), (result.status, result.reason))

    def test_handshake_rejects_tools_response_before_initialize(self) -> None:
        broker_path = self.write_broker(
            """
            sys.stdin.readline()
            print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "initial_instructions"}]}}), flush=True)
            """
        )

        result = bridge.run_handshake(self.root, broker_path, timeout_seconds=1)

        self.assertEqual(("failed", "protocol-error"), (result.status, result.reason))

    def test_handshake_rejects_output_over_limit(self) -> None:
        broker_path = self.write_broker(
            """
            sys.stdin.readline()
            sys.stdout.write("x" * 512)
            sys.stdout.flush()
            time.sleep(1)
            """
        )

        result = bridge.run_handshake(
            self.root, broker_path, timeout_seconds=1, max_output_bytes=128
        )

        self.assertEqual(("failed", "output-limit"), (result.status, result.reason))

    def test_handshake_requires_expected_serena_tool(self) -> None:
        broker_path = self.write_broker(
            """
            for line in sys.stdin:
                message = json.loads(line)
                if message.get("id") == 1:
                    print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}), flush=True)
                if message.get("id") == 2:
                    print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "unrelated"}]}}), flush=True)
                    break
            """
        )

        result = bridge.run_handshake(self.root, broker_path, timeout_seconds=1)

        self.assertEqual(("failed", "expected-tool-missing"), (result.status, result.reason))
        self.assertEqual(1, result.tool_count)

    def test_handshake_times_out_and_reaps_diagnostic_group(self) -> None:
        broker_path = self.write_broker(
            """
            sys.stdin.readline()
            time.sleep(5)
            """
        )

        result = bridge.run_handshake(self.root, broker_path, timeout_seconds=0.2)

        self.assertEqual(("failed", "initialize-timeout"), (result.status, result.reason))
        if result.process_pid is not None:
            with self.assertRaises(ProcessLookupError):
                os.kill(result.process_pid, 0)

    def test_handshake_reports_tools_list_timeout(self) -> None:
        broker_path = self.write_broker(
            """
            message = json.loads(sys.stdin.readline())
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}), flush=True)
            sys.stdin.readline()
            sys.stdin.readline()
            time.sleep(5)
            """
        )

        result = bridge.run_handshake(self.root, broker_path, timeout_seconds=1)

        self.assertEqual(("failed", "tools-list-timeout"), (result.status, result.reason))

    def test_handshake_rejects_nonzero_proxy_exit(self) -> None:
        broker_path = self.write_broker(
            """
            for line in sys.stdin:
                message = json.loads(line)
                if message.get("id") == 1:
                    print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}), flush=True)
                if message.get("id") == 2:
                    print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "activate_project"}]}}), flush=True)
                    sys.exit(7)
            """
        )

        result = bridge.run_handshake(self.root, broker_path, timeout_seconds=1)

        self.assertEqual(
            ("failed", "proxy-exit-nonzero", 7),
            (result.status, result.reason, result.proxy_exit),
        )


if __name__ == "__main__":
    unittest.main()
