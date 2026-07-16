"""Tests for durable IntelliJ opener coordination."""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "bin/workspace_harbor_opener_queue.py"
SPEC = importlib.util.spec_from_file_location(
    "workspace_harbor_opener_queue", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
queue = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = queue
SPEC.loader.exec_module(queue)


def current_owner(request_id: str) -> queue.OwnerIdentity:
    return queue.OwnerIdentity(
        request_id=request_id,
        pid=os.getpid(),
        process_started=queue.read_process_start(os.getpid()),
        command_token="python",
    )


def claim_worker(
    state_dir: str,
    root: str,
    request_id: str,
    acquired: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    coordinator = queue.OpenerCoordinator(Path(state_dir), poll_seconds=0.01)
    owner = queue.OwnerIdentity(
        request_id=request_id,
        pid=os.getpid(),
        process_started=queue.read_process_start(os.getpid()),
        command_token="python",
    )
    try:
        result = coordinator.acquire_launch(
            Path(root), owner, time.time() + 5
        )
        results.put((request_id, result))
        acquired.set()
        if not release.wait(timeout=5):
            raise RuntimeError("release event timed out")
        coordinator.release_launch(owner)
    except Exception as error:  # pragma: no cover - reported to parent
        results.put((request_id, {"error": str(error)}))


class OpenerCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.state_dir = self.base / "state"
        self.root = self.base / "project"
        self.root.mkdir()
        self.coordinator = queue.OpenerCoordinator(
            self.state_dir, poll_seconds=0.01
        )
        self.deadline = time.time() + 2

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def wait_for(self, predicate: object, timeout: float = 2) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if callable(predicate) and predicate():
                return True
            time.sleep(0.01)
        return False

    def write_queue_state(self, value: dict[str, object]) -> Path:
        path = self.state_dir / "launch-queue.json"
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def test_one_root_has_one_operation_owner(self) -> None:
        first = current_owner("first")
        second = current_owner("second")

        result = self.coordinator.acquire_operation(
            self.root, first, self.deadline
        )

        self.assertEqual("acquired", result["status"])
        with self.assertRaisesRegex(queue.CoordinationError, "operation-deadline"):
            self.coordinator.acquire_operation(
                self.root, second, time.time() + 0.05
            )

    def test_state_permissions_are_private(self) -> None:
        self.coordinator.acquire_operation(
            self.root, current_owner("owner"), self.deadline
        )

        self.assertEqual(0o700, self.state_dir.stat().st_mode & 0o777)
        for path in self.state_dir.rglob("*"):
            expected = 0o700 if path.is_dir() else 0o600
            self.assertEqual(expected, path.stat().st_mode & 0o777, path)

    def test_launch_claims_are_fifo_and_record_maximum_position(self) -> None:
        context = multiprocessing.get_context("spawn")
        results = context.Queue()
        processes: list[multiprocessing.Process] = []
        acquired_events = [context.Event() for _ in range(3)]
        release_events = [context.Event() for _ in range(3)]
        roots = []
        for index in range(3):
            root = self.base / f"project-{index}"
            root.mkdir()
            roots.append(root)

        first = context.Process(
            target=claim_worker,
            args=(
                str(self.state_dir), str(roots[0]), "first",
                acquired_events[0], release_events[0], results,
            ),
        )
        first.start()
        processes.append(first)
        self.assertTrue(acquired_events[0].wait(timeout=2))

        for index, request_id in ((1, "second"), (2, "third")):
            process = context.Process(
                target=claim_worker,
                args=(
                    str(self.state_dir), str(roots[index]), request_id,
                    acquired_events[index], release_events[index], results,
                ),
            )
            process.start()
            processes.append(process)
            self.assertTrue(
                self.wait_for(
                    lambda request_id=request_id: request_id
                    in (self.state_dir / "launch-queue.json").read_text()
                )
            )

        release_events[0].set()
        self.assertTrue(acquired_events[1].wait(timeout=2))
        self.assertFalse(acquired_events[2].is_set())
        release_events[1].set()
        self.assertTrue(acquired_events[2].wait(timeout=2))
        release_events[2].set()

        observed = [results.get(timeout=2) for _ in range(3)]
        for process in processes:
            process.join(timeout=2)
            self.assertEqual(0, process.exitcode)
        self.assertEqual(["first", "second", "third"], [item[0] for item in observed])
        by_request = {request: result for request, result in observed}
        self.assertEqual(1, by_request["first"]["maximum_position"])
        self.assertGreaterEqual(by_request["second"]["maximum_position"], 1)
        self.assertGreaterEqual(by_request["third"]["maximum_position"], 2)

    def test_dead_launch_owner_is_reclaimed(self) -> None:
        child = subprocess.Popen(["/bin/sh", "-c", "sleep 0.1"])
        started = queue.read_process_start(child.pid)
        child.wait(timeout=2)
        dead = {
            "request_id": "dead",
            "pid": child.pid,
            "process_started": started,
            "command_token": "/bin/sh",
            "project_root": str(self.root.resolve()),
            "enqueued_at": time.time() - 10,
            "sequence": 1,
            "phase": "launching",
        }
        self.write_queue_state(
            {"version": 1, "next_sequence": 2, "owner": dead, "waiting": []}
        )

        result = self.coordinator.acquire_launch(
            self.root, current_owner("next"), self.deadline
        )

        self.assertEqual("acquired", result["status"])

    def test_malformed_queue_fails_closed(self) -> None:
        path = self.state_dir / "launch-queue.json"
        path.write_text("not-json\n", encoding="utf-8")
        os.chmod(path, 0o600)

        with self.assertRaisesRegex(queue.CoordinationError, "malformed"):
            self.coordinator.acquire_launch(
                self.root, current_owner("next"), self.deadline
            )

        self.assertEqual("not-json\n", path.read_text(encoding="utf-8"))

    def test_insecure_or_symlinked_state_fails_closed(self) -> None:
        insecure = self.state_dir / "launch-queue.json"
        insecure.write_text(
            json.dumps(
                {"version": 1, "next_sequence": 1, "owner": None, "waiting": []}
            ),
            encoding="utf-8",
        )
        os.chmod(insecure, 0o644)
        with self.assertRaisesRegex(queue.CoordinationError, "not a private"):
            self.coordinator.acquire_launch(
                self.root, current_owner("insecure"), self.deadline
            )

        insecure.unlink()
        (self.state_dir / "launch-queue.lock").unlink()
        target = self.base / "outside-lock"
        target.write_text("preserve\n", encoding="utf-8")
        os.chmod(target, 0o600)
        (self.state_dir / "launch-queue.lock").symlink_to(target)
        with self.assertRaisesRegex(queue.CoordinationError, "state"):
            self.coordinator.acquire_launch(
                self.root, current_owner("symlink"), self.deadline
            )
        self.assertEqual("preserve\n", target.read_text(encoding="utf-8"))

    def test_release_all_removes_only_matching_request(self) -> None:
        other_root = self.base / "other"
        other_root.mkdir()
        first = current_owner("first")
        second = current_owner("second")
        self.coordinator.acquire_operation(self.root, first, self.deadline)
        self.coordinator.acquire_operation(other_root, second, self.deadline)
        first_record = {
            **queue.owner_record(self.root.resolve(), first, phase="launching"),
            "sequence": 1,
            "enqueued_at": time.time(),
        }
        second_record = {
            **queue.owner_record(other_root.resolve(), second, phase="queued"),
            "sequence": 2,
            "enqueued_at": time.time(),
        }
        self.write_queue_state(
            {
                "version": 1,
                "next_sequence": 3,
                "owner": first_record,
                "waiting": [second_record],
            }
        )

        self.coordinator.release_all(first)

        state = json.loads(
            (self.state_dir / "launch-queue.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(state["owner"])
        self.assertEqual(["second"], [item["request_id"] for item in state["waiting"]])
        first_path, _ = self.coordinator.operation_paths(self.root)
        second_path, _ = self.coordinator.operation_paths(other_root)
        self.assertFalse(first_path.exists())
        self.assertTrue(second_path.exists())

    def write_legacy_owner(
        self, pid: int, process_started: str, project_root: Path | None = None
    ) -> Path:
        lock_dir = self.state_dir / "opener.lock"
        lock_dir.mkdir(mode=0o700)
        owner = lock_dir / "owner"
        owner.write_text(
            f"pid={pid}\n"
            f"process_started={process_started}\n"
            f"project_root={(project_root or self.root).resolve()}\n",
            encoding="utf-8",
        )
        os.chmod(owner, 0o600)
        return lock_dir

    def test_legacy_dead_owner_is_removed(self) -> None:
        child = subprocess.Popen(["/bin/sh", "-c", "sleep 0.1"])
        started = queue.read_process_start(child.pid)
        child.wait(timeout=2)
        lock_dir = self.write_legacy_owner(child.pid, started)

        result = self.coordinator.migrate_legacy_lock(lock_dir)

        self.assertEqual({"status": "removed-dead"}, result)
        self.assertFalse(lock_dir.exists())

    def test_legacy_live_owner_is_preserved(self) -> None:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(2)",
                "open-codex-project-in-intellij",
            ]
        )
        try:
            started = queue.read_process_start(child.pid)
            lock_dir = self.write_legacy_owner(child.pid, started)

            result = self.coordinator.migrate_legacy_lock(lock_dir)

            self.assertEqual({"status": "live"}, result)
            self.assertTrue(lock_dir.exists())
        finally:
            child.terminate()
            child.wait(timeout=2)

    def test_malformed_legacy_owner_fails_closed(self) -> None:
        lock_dir = self.state_dir / "opener.lock"
        lock_dir.mkdir(mode=0o700)
        owner = lock_dir / "owner"
        owner.write_text("malformed\n", encoding="utf-8")
        os.chmod(owner, 0o600)

        with self.assertRaisesRegex(queue.CoordinationError, "legacy-state"):
            self.coordinator.migrate_legacy_lock(lock_dir)

        self.assertTrue(owner.exists())

    def test_self_check_uses_only_temporary_state(self) -> None:
        configured_state = self.base / "must-not-exist"
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "self-check"],
            capture_output=True,
            text=True,
            check=False,
            env=os.environ
            | {"INTELLIJ_OPENER_STATE_DIR": str(configured_state)},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            {"phase": "self-check", "status": "healthy"},
            json.loads(result.stdout),
        )
        self.assertFalse(configured_state.exists())

    def test_cli_invalid_owner_returns_bounded_json(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "operation-acquire",
                str(self.root),
                "--request-id",
                "request",
                "--owner-pid",
                "-1",
                "--owner-start",
                "invalid",
                "--deadline",
                str(time.time() + 1),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=os.environ
            | {"INTELLIJ_OPENER_STATE_DIR": str(self.state_dir)},
        )

        packet = json.loads(result.stdout)
        self.assertEqual(2, result.returncode)
        self.assertEqual("owner-identity", packet["phase"])
        self.assertLessEqual(len(packet["error"]), 500)


if __name__ == "__main__":
    unittest.main()
