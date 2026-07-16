"""Tests for privacy-safe Workspace Harbor bridge diagnostics."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


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


if __name__ == "__main__":
    unittest.main()
