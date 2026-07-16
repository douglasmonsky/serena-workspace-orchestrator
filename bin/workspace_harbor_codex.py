"""Fail-closed Codex app attestation, identity, checkpoint, and policy helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
from typing import Any, Iterable
import uuid


APP_BUNDLE = Path("/Applications/ChatGPT.app")
APP_EXECUTABLE = APP_BUNDLE / "Contents" / "MacOS" / "ChatGPT"
APP_BUNDLE_ID = "com.openai.codex"
STATE_SCHEMA_VERSION = 1
ATTESTATION_MAX_AGE_SECONDS = 30
HEARTBEAT_MIN_SECONDS = 30
HEARTBEAT_MAX_SECONDS = 180


class AttestationError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RestartAttestation:
    current_thread: str
    host_id: str
    active_threads: tuple[str, ...]
    unavailable_host_count: int
    observed_at: str
    heartbeat_id: str
    heartbeat_target: str
    heartbeat_next_run: str
    incident: str
    nonce: str
    schema_version: int = STATE_SCHEMA_VERSION


@dataclass(frozen=True)
class CodexProcessIdentity:
    pid: int
    started: str
    executable: str
    bundle_id: str


@dataclass(frozen=True)
class RelaunchCheckpoint:
    incident_id: str
    incident_store: str
    root: str
    thread_id: str
    heartbeat_id: str
    attestation_nonce: str
    doctor_pid: int
    app_identity: CodexProcessIdentity
    created_at: str
    schema_version: int = STATE_SCHEMA_VERSION


def _aware(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AttestationError(reason)
    return value.astimezone(timezone.utc)


def _uuid(value: str, reason: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise AttestationError(reason) from error
    if str(parsed) != value.lower():
        raise AttestationError(reason)
    return value


def _incident_id(value: str) -> str:
    if len(value) != 32:
        raise AttestationError("invalid-incident")
    try:
        int(value, 16)
    except ValueError as error:
        raise AttestationError("invalid-incident") from error
    return value


def _safe_identifier(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise AttestationError(reason)
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    if any(character not in allowed for character in value):
        raise AttestationError(reason)
    return value


def build_restart_attestation(
    *,
    current_thread: str,
    host_id: str,
    active_threads: Iterable[str],
    unavailable_hosts: Iterable[str],
    observed_at: datetime,
    heartbeat: dict[str, Any],
    incident: str,
    active_children: Iterable[str] = (),
    unknown_threads: Iterable[str] = (),
    nonce: str | None = None,
) -> RestartAttestation:
    current = _uuid(current_thread, "invalid-current-thread")
    _safe_identifier(host_id, "invalid-host")
    expected_incident = _incident_id(incident)
    active = tuple(active_threads)
    if len(set(active)) != len(active):
        raise AttestationError("duplicate-task-id")
    for thread_id in active:
        _uuid(thread_id, "invalid-active-thread")
    children = tuple(active_children)
    if children:
        raise AttestationError("active-child")
    unknown = tuple(unknown_threads)
    if unknown:
        raise AttestationError("unknown-task-status")
    unavailable = tuple(unavailable_hosts)
    if unavailable:
        raise AttestationError("unavailable-host")
    if current not in active:
        raise AttestationError("current-task-missing")
    if active != (current,):
        raise AttestationError("second-active-task")
    observed = _aware(observed_at, "invalid-observation-time")
    heartbeat_id = _safe_identifier(heartbeat.get("id"), "invalid-heartbeat-id")
    target = heartbeat.get("target_thread_id")
    if target != current:
        raise AttestationError("heartbeat-target-mismatch")
    if heartbeat.get("enabled") is not True:
        raise AttestationError("heartbeat-disabled")
    if heartbeat.get("incident") != expected_incident:
        raise AttestationError("incident-mismatch")
    next_run_value = heartbeat.get("next_run")
    if not isinstance(next_run_value, datetime):
        raise AttestationError("invalid-heartbeat-time")
    next_run = _aware(next_run_value, "invalid-heartbeat-time")
    delay = (next_run - observed).total_seconds()
    if delay < HEARTBEAT_MIN_SECONDS:
        raise AttestationError("heartbeat-too-soon")
    if delay > HEARTBEAT_MAX_SECONDS:
        raise AttestationError("heartbeat-too-late")
    nonce_value = nonce or secrets.token_hex(16)
    if len(nonce_value) != 32:
        raise AttestationError("invalid-nonce")
    try:
        int(nonce_value, 16)
    except ValueError as error:
        raise AttestationError("invalid-nonce") from error
    return RestartAttestation(
        current_thread=current,
        host_id=host_id,
        active_threads=active,
        unavailable_host_count=0,
        observed_at=observed.isoformat(),
        heartbeat_id=heartbeat_id,
        heartbeat_target=target,
        heartbeat_next_run=next_run.isoformat(),
        incident=expected_incident,
        nonce=nonce_value,
    )


def validate_restart_attestation(
    attestation: RestartAttestation,
    *,
    current_thread: str,
    incident: str,
    now: datetime | None = None,
) -> None:
    if attestation.schema_version != STATE_SCHEMA_VERSION:
        raise AttestationError("invalid-attestation-schema")
    _uuid(attestation.current_thread, "invalid-current-thread")
    _safe_identifier(attestation.host_id, "invalid-host")
    _incident_id(attestation.incident)
    _safe_identifier(attestation.heartbeat_id, "invalid-heartbeat-id")
    if len(attestation.nonce) != 32:
        raise AttestationError("invalid-nonce")
    try:
        int(attestation.nonce, 16)
    except ValueError as error:
        raise AttestationError("invalid-nonce") from error
    if attestation.current_thread != current_thread:
        raise AttestationError("attestation-thread-mismatch")
    if attestation.incident != incident:
        raise AttestationError("attestation-incident-mismatch")
    try:
        observed = _aware(
            datetime.fromisoformat(attestation.observed_at),
            "invalid-observation-time",
        )
        heartbeat_next = _aware(
            datetime.fromisoformat(attestation.heartbeat_next_run),
            "invalid-heartbeat-time",
        )
    except ValueError as error:
        raise AttestationError("invalid-attestation-time") from error
    heartbeat_delay = (heartbeat_next - observed).total_seconds()
    if heartbeat_delay < HEARTBEAT_MIN_SECONDS:
        raise AttestationError("heartbeat-too-soon")
    if heartbeat_delay > HEARTBEAT_MAX_SECONDS:
        raise AttestationError("heartbeat-too-late")
    current_time = _aware(now or datetime.now(timezone.utc), "invalid-current-time")
    age = (current_time - observed).total_seconds()
    if age < 0:
        raise AttestationError("attestation-from-future")
    if age > ATTESTATION_MAX_AGE_SECONDS:
        raise AttestationError("attestation-expired")
    if attestation.active_threads != (current_thread,):
        raise AttestationError("attestation-not-exclusive")
    if attestation.unavailable_host_count != 0:
        raise AttestationError("attestation-host-unavailable")
    if attestation.heartbeat_target != current_thread:
        raise AttestationError("attestation-heartbeat-mismatch")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_private_dataclass(path: Path, value: Any) -> None:
    _atomic_write_json(path, asdict(value))


def load_restart_attestation(path: Path) -> RestartAttestation:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["active_threads"] = tuple(payload["active_threads"])
    return RestartAttestation(**payload)


def load_checkpoint(path: Path) -> RelaunchCheckpoint:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["app_identity"] = CodexProcessIdentity(**payload["app_identity"])
    checkpoint = RelaunchCheckpoint(**payload)
    if checkpoint.schema_version != STATE_SCHEMA_VERSION:
        raise ValueError("invalid checkpoint schema")
    _incident_id(checkpoint.incident_id)
    _uuid(checkpoint.thread_id, "invalid checkpoint thread")
    _safe_identifier(checkpoint.heartbeat_id, "invalid checkpoint heartbeat")
    if checkpoint.doctor_pid <= 0:
        raise ValueError("invalid checkpoint pid")
    if (
        checkpoint.app_identity.executable != str(APP_EXECUTABLE)
        or checkpoint.app_identity.bundle_id != APP_BUNDLE_ID
    ):
        raise ValueError("invalid checkpoint identity")
    return checkpoint


def _candidate_pids() -> list[int]:
    try:
        completed = subprocess.run(
            ["/usr/bin/pgrep", "-x", "ChatGPT"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode not in {0, 1}:
        return []
    result = []
    for line in completed.stdout.splitlines():
        try:
            result.append(int(line.strip()))
        except ValueError:
            continue
    return result


def _read_process(pid: int) -> tuple[str, str] | None:
    try:
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout.strip("\n")
    if completed.returncode != 0 or len(output) < 25:
        return None
    started = output[:24].strip()
    try:
        command = shlex.split(output[24:].strip())
    except ValueError:
        return None
    if not command:
        return None
    return started, command[0]


def _bundle_identifier() -> str | None:
    try:
        completed = subprocess.run(
            [
                "/usr/bin/defaults",
                "read",
                str(APP_BUNDLE / "Contents" / "Info"),
                "CFBundleIdentifier",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def find_codex_app_identity() -> CodexProcessIdentity | None:
    pids = _candidate_pids()
    if len(pids) != 1 or _bundle_identifier() != APP_BUNDLE_ID:
        return None
    details = _read_process(pids[0])
    if details is None or details[1] != str(APP_EXECUTABLE):
        return None
    return CodexProcessIdentity(pids[0], details[0], details[1], APP_BUNDLE_ID)


def identity_matches(identity: CodexProcessIdentity) -> bool:
    if identity.bundle_id != APP_BUNDLE_ID or identity.executable != str(APP_EXECUTABLE):
        return False
    if _bundle_identifier() != APP_BUNDLE_ID:
        return False
    details = _read_process(identity.pid)
    return details == (identity.started, identity.executable)


def process_exists(pid: int) -> bool:
    try:
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "pid="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return completed.returncode == 0 and completed.stdout.strip() == str(pid)


class RestartPolicyStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "schema_version": STATE_SCHEMA_VERSION,
                "automatic_codex_restart": False,
            }
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != STATE_SCHEMA_VERSION
            or not isinstance(payload.get("automatic_codex_restart"), bool)
        ):
            raise ValueError("invalid restart policy")
        return payload

    def write(self, enabled: bool) -> dict[str, Any]:
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "automatic_codex_restart": bool(enabled),
        }
        _atomic_write_json(self.path, payload)
        return payload
