#!/usr/bin/env python3
"""Coordinate single-flight worktree opens and serialized IntelliJ launches."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
from typing import Any, Iterator
import sys
import uuid


@dataclass(frozen=True)
class OwnerIdentity:
    request_id: str
    pid: int
    process_started: str
    command_token: str


class CoordinationError(RuntimeError):
    def __init__(self, phase: str, message: str) -> None:
        super().__init__(f"{phase}: {message}")
        self.phase = phase


def root_digest(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:32]


def _process_details(pid: int) -> tuple[str, str] | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CoordinationError(
            "owner-identity", f"process identity probe failed: {error}"
        ) from error
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or not lines:
        return None
    fields = lines[0].split(None, 5)
    if len(fields) < 6:
        raise CoordinationError("owner-identity", "process identity was malformed")
    return " ".join(fields[:5]), fields[5]


def read_process_start(pid: int) -> str:
    details = _process_details(pid)
    if details is None:
        raise CoordinationError(
            "owner-identity", f"process identity unavailable: {pid}"
        )
    return details[0]


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
    ):
        raise CoordinationError("state-invalid", "state directory is not private")


def _validate_private_file(path: Path, phase: str) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise CoordinationError(phase, "state file is not a private regular file")
    if info.st_uid != os.getuid():
        raise CoordinationError(phase, "state file has an unexpected owner")


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    _ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path, phase: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    _validate_private_file(path, phase)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CoordinationError(phase, "state JSON is malformed") from error
    if not isinstance(value, dict):
        raise CoordinationError(phase, "state JSON is not an object")
    return value


@contextmanager
def _advisory_lock(path: Path) -> Iterator[None]:
    _ensure_private_directory(path.parent)
    existed = path.exists() or path.is_symlink()
    if existed:
        _validate_private_file(path, "state-invalid")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise CoordinationError("state-invalid", "lock file is unsafe") from error
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        if not existed:
            os.fchmod(handle.fileno(), 0o600)
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            raise CoordinationError("state-invalid", "lock file is unsafe")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_owner(owner: OwnerIdentity) -> None:
    if not owner.request_id or len(owner.request_id) > 128:
        raise CoordinationError("owner-identity", "request ID is invalid")
    if owner.pid <= 0 or not owner.process_started or not owner.command_token:
        raise CoordinationError("owner-identity", "owner identity is incomplete")
    details = _process_details(owner.pid)
    if details is None or details[0] != owner.process_started:
        raise CoordinationError("owner-identity", "caller process identity is stale")
    if owner.command_token not in details[1]:
        raise CoordinationError("owner-identity", "caller command identity is ambiguous")


def owner_record(
    root: Path, owner: OwnerIdentity, *, phase: str
) -> dict[str, Any]:
    return {
        "request_id": owner.request_id,
        "pid": owner.pid,
        "process_started": owner.process_started,
        "command_token": owner.command_token,
        "project_root": str(root),
        "phase": phase,
    }


def _record_identity(record: dict[str, Any], phase: str) -> OwnerIdentity:
    request_id = record.get("request_id")
    pid = record.get("pid")
    process_started = record.get("process_started")
    command_token = record.get("command_token")
    if (
        not isinstance(request_id, str)
        or not request_id
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(process_started, str)
        or not process_started
        or not isinstance(command_token, str)
        or not command_token
    ):
        raise CoordinationError(phase, "owner record is malformed")
    return OwnerIdentity(request_id, pid, process_started, command_token)


def _record_is_live(record: dict[str, Any], phase: str) -> bool:
    identity = _record_identity(record, phase)
    details = _process_details(identity.pid)
    if details is None or details[0] != identity.process_started:
        return False
    if identity.command_token not in details[1]:
        raise CoordinationError(phase, "live owner command identity is ambiguous")
    return True


def _matches(record: dict[str, Any], owner: OwnerIdentity) -> bool:
    return (
        record.get("request_id") == owner.request_id
        and record.get("pid") == owner.pid
        and record.get("process_started") == owner.process_started
    )


class OpenerCoordinator:
    def __init__(self, state_dir: Path, *, poll_seconds: float = 0.1) -> None:
        self.state_dir = state_dir
        self.poll_seconds = poll_seconds
        if poll_seconds <= 0:
            raise CoordinationError("state-invalid", "poll interval must be positive")
        _ensure_private_directory(self.state_dir)

    @property
    def operations_dir(self) -> Path:
        return self.state_dir / "operations"

    def _operation_paths(self, root: Path) -> tuple[Path, Path]:
        digest = root_digest(root)
        return (
            self.operations_dir / f"{digest}.json",
            self.operations_dir / f"{digest}.lock",
        )

    def operation_paths(self, root: Path) -> tuple[Path, Path]:
        return self._operation_paths(root.resolve())

    @property
    def queue_path(self) -> Path:
        return self.state_dir / "launch-queue.json"

    @property
    def queue_lock_path(self) -> Path:
        return self.state_dir / "launch-queue.lock"

    def acquire_operation(
        self, root: Path, owner: OwnerIdentity, deadline: float
    ) -> dict[str, object]:
        root = root.resolve()
        _validate_owner(owner)
        started = time.time()
        waited = False
        record_path, lock_path = self._operation_paths(root)
        while True:
            with _advisory_lock(lock_path):
                record = _load_json(record_path, "operation-state")
                if record is None or not _record_is_live(record, "operation-state"):
                    _atomic_json_write(
                        record_path, owner_record(root, owner, phase="opening")
                    )
                    return {
                        "status": "acquired",
                        "phase": "operation",
                        "wait_seconds": max(0.0, time.time() - started),
                        "joined": waited,
                    }
                if record.get("project_root") != str(root):
                    raise CoordinationError(
                        "operation-state", "operation root does not match its key"
                    )
                if _matches(record, owner):
                    return {
                        "status": "acquired",
                        "phase": "operation",
                        "wait_seconds": max(0.0, time.time() - started),
                        "joined": waited,
                    }
            if time.time() >= deadline:
                raise CoordinationError(
                    "operation-deadline",
                    "overall opener deadline reached waiting for worktree owner",
                )
            waited = True
            time.sleep(min(self.poll_seconds, max(0.0, deadline - time.time())))

    def release_operation(self, root: Path, owner: OwnerIdentity) -> None:
        root = root.resolve()
        record_path, lock_path = self._operation_paths(root)
        with _advisory_lock(lock_path):
            record = _load_json(record_path, "operation-state")
            if record is not None and _matches(record, owner):
                record_path.unlink()

    def _load_queue_state(self) -> dict[str, Any]:
        state = _load_json(self.queue_path, "queue-state")
        if state is None:
            return {
                "version": 1,
                "next_sequence": 1,
                "owner": None,
                "waiting": [],
            }
        if (
            state.get("version") != 1
            or not isinstance(state.get("next_sequence"), int)
            or state["next_sequence"] < 1
            or state.get("owner") is not None
            and not isinstance(state.get("owner"), dict)
            or not isinstance(state.get("waiting"), list)
            or not all(isinstance(item, dict) for item in state["waiting"])
        ):
            raise CoordinationError("queue-state", "queue state is malformed")
        records = [
            *([state["owner"]] if state["owner"] is not None else []),
            *state["waiting"],
        ]
        sequences: set[int] = set()
        for record in records:
            _record_identity(record, "queue-state")
            sequence = record.get("sequence")
            project_root = record.get("project_root")
            enqueued_at = record.get("enqueued_at")
            phase = record.get("phase")
            if (
                not isinstance(sequence, int)
                or sequence < 1
                or sequence in sequences
                or not isinstance(project_root, str)
                or not Path(project_root).is_absolute()
                or not isinstance(enqueued_at, (int, float))
                or phase not in {"queued", "launching"}
            ):
                raise CoordinationError("queue-state", "queue entry is malformed")
            sequences.add(sequence)
        waiting_sequences = [item["sequence"] for item in state["waiting"]]
        if waiting_sequences != sorted(waiting_sequences):
            raise CoordinationError("queue-state", "queue order is malformed")
        return state

    def _prune_dead_queue_entries(self, state: dict[str, Any]) -> bool:
        changed = False
        owner = state["owner"]
        if owner is not None and not _record_is_live(owner, "queue-state"):
            state["owner"] = None
            changed = True
        waiting = []
        for record in state["waiting"]:
            if _record_is_live(record, "queue-state"):
                waiting.append(record)
            else:
                changed = True
        state["waiting"] = waiting
        return changed

    def acquire_launch(
        self, root: Path, owner: OwnerIdentity, deadline: float
    ) -> dict[str, object]:
        root = root.resolve()
        _validate_owner(owner)
        started = time.time()
        maximum_position = 1
        enqueued = False
        while True:
            with _advisory_lock(self.queue_lock_path):
                state = self._load_queue_state()
                changed = self._prune_dead_queue_entries(state)
                launch_owner = state["owner"]
                if launch_owner is not None and _matches(launch_owner, owner):
                    if changed:
                        _atomic_json_write(self.queue_path, state)
                    return {
                        "status": "acquired",
                        "phase": "launch",
                        "wait_seconds": max(0.0, time.time() - started),
                        "maximum_position": maximum_position,
                    }
                matching = next(
                    (item for item in state["waiting"] if _matches(item, owner)),
                    None,
                )
                if matching is None:
                    sequence = state["next_sequence"]
                    state["next_sequence"] = sequence + 1
                    matching = {
                        **owner_record(root, owner, phase="queued"),
                        "sequence": sequence,
                        "enqueued_at": time.time(),
                    }
                    state["waiting"].append(matching)
                    enqueued = True
                    changed = True
                elif matching.get("project_root") != str(root):
                    raise CoordinationError(
                        "queue-state", "request ID is associated with another root"
                    )
                position = (
                    (1 if state["owner"] is not None else 0)
                    + state["waiting"].index(matching)
                    + 1
                )
                maximum_position = max(maximum_position, position)
                if state["owner"] is None and state["waiting"][0] is matching:
                    state["waiting"].pop(0)
                    matching["phase"] = "launching"
                    state["owner"] = matching
                    _atomic_json_write(self.queue_path, state)
                    return {
                        "status": "acquired",
                        "phase": "launch",
                        "wait_seconds": max(0.0, time.time() - started),
                        "maximum_position": maximum_position,
                        "queued": enqueued,
                    }
                if changed:
                    _atomic_json_write(self.queue_path, state)
            if time.time() >= deadline:
                raise CoordinationError(
                    "queue-deadline",
                    "overall opener deadline reached in launch queue",
                )
            time.sleep(min(self.poll_seconds, max(0.0, deadline - time.time())))

    def release_launch(self, owner: OwnerIdentity) -> None:
        with _advisory_lock(self.queue_lock_path):
            state = self._load_queue_state()
            changed = False
            if state["owner"] is not None and _matches(state["owner"], owner):
                state["owner"] = None
                changed = True
            waiting = [
                item for item in state["waiting"] if not _matches(item, owner)
            ]
            if len(waiting) != len(state["waiting"]):
                state["waiting"] = waiting
                changed = True
            if changed:
                _atomic_json_write(self.queue_path, state)

    def release_all(self, owner: OwnerIdentity) -> None:
        self.release_launch(owner)
        if not self.operations_dir.is_dir():
            return
        for record_path in sorted(self.operations_dir.glob("*.json")):
            lock_path = record_path.with_suffix(".lock")
            with _advisory_lock(lock_path):
                record = _load_json(record_path, "operation-state")
                if record is not None and _matches(record, owner):
                    record_path.unlink()

    def migrate_legacy_lock(self, legacy_lock_dir: Path) -> dict[str, object]:
        if not legacy_lock_dir.exists():
            return {"status": "absent"}
        info = legacy_lock_dir.stat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()
        ):
            raise CoordinationError(
                "legacy-state", "legacy opener lock directory is unsafe"
            )
        entries = list(legacy_lock_dir.iterdir())
        owner_path = legacy_lock_dir / "owner"
        if entries != [owner_path]:
            raise CoordinationError("legacy-state", "legacy opener lock is malformed")
        _validate_private_file(owner_path, "legacy-state")
        fields: dict[str, str] = {}
        try:
            for line in owner_path.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition("=")
                if not separator or key in fields:
                    raise ValueError("invalid owner field")
                fields[key] = value
            if set(fields) != {"pid", "process_started", "project_root"}:
                raise ValueError("invalid owner fields")
            pid = int(fields["pid"])
            if (
                pid <= 0
                or not fields["process_started"]
                or not Path(fields["project_root"]).is_absolute()
            ):
                raise ValueError("invalid owner values")
        except (OSError, ValueError) as error:
            raise CoordinationError("legacy-state", "legacy owner is malformed") from error
        details = _process_details(pid)
        if details is None or details[0] != fields["process_started"]:
            owner_path.unlink()
            legacy_lock_dir.rmdir()
            return {"status": "removed-dead"}
        if "open-codex-project-in-intellij" not in details[1]:
            raise CoordinationError(
                "legacy-state", "legacy live owner identity is ambiguous"
            )
        return {"status": "live"}


def _add_owner_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--owner-pid", required=True, type=int)
    parser.add_argument("--owner-start", required=True)


def _owner_from_arguments(arguments: argparse.Namespace) -> OwnerIdentity:
    return OwnerIdentity(
        request_id=arguments.request_id,
        pid=arguments.owner_pid,
        process_started=arguments.owner_start,
        command_token="open-codex-project-in-intellij",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("operation-acquire", "launch-acquire"):
        command = commands.add_parser(name)
        command.add_argument("root", type=Path)
        _add_owner_arguments(command)
        command.add_argument("--deadline", required=True, type=float)
    operation_release = commands.add_parser("operation-release")
    operation_release.add_argument("root", type=Path)
    _add_owner_arguments(operation_release)
    for name in ("launch-release", "release-all"):
        command = commands.add_parser(name)
        _add_owner_arguments(command)
    migration = commands.add_parser("migrate-legacy")
    migration.add_argument("path", type=Path)
    commands.add_parser("self-check")
    return parser


def _self_check() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temporary_directory:
        base = Path(temporary_directory)
        root = base / "project"
        root.mkdir()
        coordinator = OpenerCoordinator(base / "state", poll_seconds=0.01)
        owner = OwnerIdentity(
            request_id=str(uuid.uuid4()),
            pid=os.getpid(),
            process_started=read_process_start(os.getpid()),
            command_token=Path(__file__).name,
        )
        deadline = time.time() + 2
        coordinator.acquire_operation(root, owner, deadline)
        coordinator.acquire_launch(root, owner, deadline)
        coordinator.release_all(owner)
        queue_state = coordinator._load_queue_state()
        operation_path, _ = coordinator.operation_paths(root)
        if (
            queue_state["owner"] is not None
            or queue_state["waiting"]
            or operation_path.exists()
        ):
            raise CoordinationError("self-check", "fixture state did not clean up")
    return {"status": "healthy", "phase": "self-check"}


def _execute(arguments: argparse.Namespace) -> dict[str, object]:
    if arguments.command == "self-check":
        return _self_check()
    state_dir = Path(
        os.environ.get(
            "INTELLIJ_OPENER_STATE_DIR",
            Path.home() / ".codex" / "state" / "intellij-projects",
        )
    ).expanduser()
    poll_seconds = float(os.environ.get("INTELLIJ_OPENER_QUEUE_POLL", "0.1"))
    coordinator = OpenerCoordinator(state_dir, poll_seconds=poll_seconds)
    if arguments.command == "migrate-legacy":
        return coordinator.migrate_legacy_lock(arguments.path)
    owner = _owner_from_arguments(arguments)
    if arguments.command == "operation-acquire":
        result = coordinator.acquire_operation(
            arguments.root, owner, arguments.deadline
        )
    elif arguments.command == "operation-release":
        coordinator.release_operation(arguments.root, owner)
        result = {"status": "released", "phase": "operation"}
    elif arguments.command == "launch-acquire":
        result = coordinator.acquire_launch(arguments.root, owner, arguments.deadline)
    elif arguments.command == "launch-release":
        coordinator.release_launch(owner)
        result = {"status": "released", "phase": "launch"}
    elif arguments.command == "release-all":
        coordinator.release_all(owner)
        result = {"status": "released", "phase": "all"}
    else:  # pragma: no cover - argparse enforces the command set
        raise CoordinationError("arguments", "unknown command")
    return {**result, "request_id": owner.request_id}


def main(argv: list[str] | None = None) -> int:
    try:
        result = _execute(_parser().parse_args(argv))
    except CoordinationError as error:
        result = {
            "status": "failed",
            "phase": error.phase,
            "error": str(error)[:500],
        }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 1 if error.phase.endswith("-deadline") else 2
    except (OSError, ValueError) as error:
        result = {
            "status": "failed",
            "phase": "operational-failure",
            "error": str(error)[:500],
        }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
