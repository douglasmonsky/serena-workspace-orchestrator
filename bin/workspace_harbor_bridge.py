"""Privacy-safe observability and diagnostics for the Serena MCP bridge."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import time
from typing import Any, Sequence
import uuid


BRIDGE_SCHEMA_VERSION = 1
INCIDENT_SCHEMA_VERSION = 1
DEFAULT_JOURNAL_MAX_BYTES = 524_288
DEFAULT_JOURNAL_BACKUPS = 4
MAX_REASON_LENGTH = 96
HEX_20 = re.compile(r"^[0-9a-f]{20}$")
HEX_24 = re.compile(r"^[0-9a-f]{24}$")
HEX_32 = re.compile(r"^[0-9a-f]{32}$")
REASON_CODE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CODEX_HOST_OWNER = re.compile(r"^codex-host-[0-9a-f]{24}$")
ALLOWED_OWNER_SOURCES = frozenset(
    {"explicit", "root-thread", "subagent-lineage", "codex-host", "process-fallback"}
)
ALLOWED_STAGES = frozenset(
    {
        "project-resolution",
        "projectless-fallback",
        "ownership",
        "service-reused",
        "service-started",
        "lease-inserted",
        "proxy-started",
        "proxy-exit",
        "lease-cleanup",
        "initialize",
        "tools-list",
        "handshake-cleanup",
    }
)
ALLOWED_OUTCOMES = frozenset({"ok", "failed", "protected", "unavailable"})
EXPECTED_SERENA_TOOLS = frozenset(
    {"initial_instructions", "activate_project", "get_symbols_overview"}
)
INCIDENT_STATES = frozenset(
    {
        "diagnosing",
        "restart-eligible",
        "restart-prepared",
        "resume-pending",
        "closed-healthy",
        "closed-blocked",
        "closed-fresh-task-required",
    }
)
ALLOWED_INCIDENT_TRANSITIONS = {
    "diagnosing": frozenset(
        {"restart-eligible", "closed-healthy", "closed-blocked"}
    ),
    "restart-eligible": frozenset({"restart-prepared", "closed-blocked"}),
    "restart-prepared": frozenset({"resume-pending", "closed-blocked"}),
    "resume-pending": frozenset({"closed-healthy", "closed-fresh-task-required"}),
}


@dataclass(frozen=True)
class BridgeEvent:
    attempt_id: str
    timestamp: str
    root_digest: str | None
    service_key: str | None
    owner_source: str | None
    stage: str
    outcome: str
    reason: str | None
    duration_ms: int
    schema_version: int = BRIDGE_SCHEMA_VERSION


@dataclass(frozen=True)
class ConfigCheck:
    status: str
    reason: str
    command: str | None


@dataclass(frozen=True)
class HandshakeResult:
    status: str
    reason: str
    initialize_ms: int | None
    tools_list_ms: int | None
    tool_count: int
    expected_tool_found: bool
    proxy_exit: int | None
    process_pid: int | None = None


@dataclass(frozen=True)
class BridgeIncident:
    id: str
    root: str
    root_digest: str
    thread_id: str
    created_at: str
    updated_at: str
    state: str
    restart_attempted: bool
    heartbeat_id: str | None
    reason: str
    dogfood_restart: bool = False
    schema_version: int = INCIDENT_SCHEMA_VERSION


def root_digest(root: Path) -> str:
    resolved = str(root.expanduser().resolve(strict=False))
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]


def _validate_optional(value: str | None, pattern: re.Pattern[str], name: str) -> None:
    if value is not None and pattern.fullmatch(value) is None:
        raise ValueError(f"invalid {name}")


def _validate_event(event: BridgeEvent) -> None:
    if event.schema_version != BRIDGE_SCHEMA_VERSION:
        raise ValueError("invalid schema version")
    if HEX_32.fullmatch(event.attempt_id) is None:
        raise ValueError("invalid attempt id")
    if not event.timestamp or len(event.timestamp) > 64 or "\n" in event.timestamp:
        raise ValueError("invalid timestamp")
    _validate_optional(event.root_digest, HEX_24, "root digest")
    _validate_optional(event.service_key, HEX_20, "service key")
    if event.owner_source is not None and event.owner_source not in ALLOWED_OWNER_SOURCES:
        raise ValueError("invalid owner source")
    if event.stage not in ALLOWED_STAGES:
        raise ValueError("invalid stage")
    if event.outcome not in ALLOWED_OUTCOMES:
        raise ValueError("invalid outcome")
    if event.reason is not None and (
        not event.reason
        or len(event.reason) > MAX_REASON_LENGTH
        or REASON_CODE.fullmatch(event.reason) is None
    ):
        raise ValueError("invalid reason")
    if isinstance(event.duration_ms, bool) or event.duration_ms < 0:
        raise ValueError("invalid duration")


def _event_line(event: BridgeEvent) -> str:
    return json.dumps(asdict(event), separators=(",", ":"), sort_keys=True) + "\n"


class BridgeJournal:
    """A small fail-open operational journal containing no MCP payload data."""

    def __init__(
        self,
        state_dir: Path,
        *,
        max_bytes: int = DEFAULT_JOURNAL_MAX_BYTES,
        backups: int = DEFAULT_JOURNAL_BACKUPS,
    ) -> None:
        self.state_dir = state_dir
        self.path = state_dir / "journal.jsonl"
        self.lock_path = state_dir / "journal.lock"
        self.max_bytes = max_bytes
        self.backups = backups

    def append(self, event: BridgeEvent) -> bool:
        try:
            _validate_event(event)
            line = _event_line(event)
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.state_dir, 0o700)
            with self.lock_path.open("a+", encoding="utf-8") as lock:
                os.chmod(self.lock_path, 0o600)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                self._rotate_if_needed(len(line.encode("utf-8")))
                with self.path.open("a", encoding="utf-8") as stream:
                    os.chmod(self.path, 0o600)
                    stream.write(line)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        current_bytes = self.path.stat().st_size if self.path.exists() else 0
        if current_bytes == 0 or current_bytes + incoming_bytes <= self.max_bytes:
            return
        for index in range(self.backups, 0, -1):
            source = self.path if index == 1 else Path(f"{self.path}.{index - 1}")
            destination = Path(f"{self.path}.{index}")
            if source.exists():
                os.replace(source, destination)

    def recent(
        self,
        root: Path,
        limit: int = 20,
        *,
        root_digest_override: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        expected = root_digest_override or root_digest(root)
        records: list[dict[str, Any]] = []
        paths = [Path(f"{self.path}.{index}") for index in range(self.backups, 0, -1)]
        paths.append(self.path)
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    payload = json.loads(line)
                    event = BridgeEvent(**payload)
                    _validate_event(event)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if event.root_digest == expected:
                    records.append(asdict(event))
        return records[-limit:]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_incident(incident: BridgeIncident) -> None:
    if incident.schema_version != INCIDENT_SCHEMA_VERSION:
        raise ValueError("invalid incident schema version")
    if HEX_32.fullmatch(incident.id) is None:
        raise ValueError("invalid incident id")
    if HEX_24.fullmatch(incident.root_digest) is None:
        raise ValueError("invalid incident root digest")
    if not incident.root or "\n" in incident.root:
        raise ValueError("invalid incident root")
    if SAFE_ID.fullmatch(incident.thread_id) is None:
        raise ValueError("invalid incident thread")
    if incident.state not in INCIDENT_STATES:
        raise ValueError("invalid incident state")
    for timestamp in (incident.created_at, incident.updated_at):
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError as error:
            raise ValueError("invalid incident timestamp") from error
        if parsed.tzinfo is None:
            raise ValueError("invalid incident timestamp")
    if not REASON_CODE.fullmatch(incident.reason):
        raise ValueError("invalid incident reason")
    if (
        incident.heartbeat_id is not None
        and SAFE_ID.fullmatch(incident.heartbeat_id) is None
    ):
        raise ValueError("invalid incident heartbeat")


class IncidentStore:
    """Private, atomic state for one guarded Codex bridge-recovery attempt."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.lock_path = state_dir / ".lock"

    def path_for(self, incident_id: str) -> Path:
        if HEX_32.fullmatch(incident_id) is None:
            raise ValueError("invalid incident id")
        return self.state_dir / f"{incident_id}.json"

    def _ensure_directory(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)

    def _lock(self):
        self._ensure_directory()
        lock = self.lock_path.open("a+", encoding="utf-8")
        os.chmod(self.lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return lock

    @staticmethod
    def _decode(path: Path) -> BridgeIncident:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            incident = BridgeIncident(**payload)
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise ValueError("invalid incident record") from error
        _validate_incident(incident)
        return incident

    def _write_locked(self, incident: BridgeIncident) -> None:
        _validate_incident(incident)
        path = self.path_for(incident.id)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(asdict(incident), stream, separators=(",", ":"), sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            directory = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def load(self, incident_id: str) -> BridgeIncident:
        with self._lock():
            return self._decode(self.path_for(incident_id))

    def create(
        self,
        root: Path,
        thread_id: str,
        state: str,
        reason: str,
    ) -> BridgeIncident:
        canonical = root.expanduser().resolve(strict=False)
        now = _utc_now()
        incident = BridgeIncident(
            id=uuid.uuid4().hex,
            root=str(canonical),
            root_digest=root_digest(canonical),
            thread_id=thread_id,
            created_at=now,
            updated_at=now,
            state=state,
            restart_attempted=False,
            heartbeat_id=None,
            reason=reason,
        )
        with self._lock():
            self._write_locked(incident)
        return incident

    def create_or_reuse_restart(
        self, root: Path, thread_id: str, reason: str
    ) -> BridgeIncident:
        canonical = str(root.expanduser().resolve(strict=False))
        with self._lock():
            for path in sorted(self.state_dir.glob("*.json")):
                incident = self._decode(path)
                if (
                    incident.root == canonical
                    and incident.thread_id == thread_id
                    and incident.state == "restart-eligible"
                ):
                    return incident
            now = _utc_now()
            incident = BridgeIncident(
                id=uuid.uuid4().hex,
                root=canonical,
                root_digest=root_digest(Path(canonical)),
                thread_id=thread_id,
                created_at=now,
                updated_at=now,
                state="restart-eligible",
                restart_attempted=False,
                heartbeat_id=None,
                reason=reason,
            )
            self._write_locked(incident)
            return incident

    def transition(
        self,
        incident_id: str,
        expected: str,
        next_state: str,
        *,
        reason: str | None = None,
        restart_attempted: bool | None = None,
        heartbeat_id: str | None = None,
        dogfood_restart: bool | None = None,
    ) -> BridgeIncident:
        if next_state not in ALLOWED_INCIDENT_TRANSITIONS.get(expected, frozenset()):
            raise ValueError("invalid incident transition")
        with self._lock():
            current = self._decode(self.path_for(incident_id))
            if current.state != expected:
                raise ValueError("incident state changed")
            updated = replace(
                current,
                state=next_state,
                updated_at=_utc_now(),
                reason=reason or current.reason,
                restart_attempted=(
                    current.restart_attempted
                    if restart_attempted is None
                    else restart_attempted
                ),
                heartbeat_id=(
                    current.heartbeat_id if heartbeat_id is None else heartbeat_id
                ),
                dogfood_restart=(
                    current.dogfood_restart
                    if dogfood_restart is None
                    else dogfood_restart
                ),
            )
            self._write_locked(updated)
            return updated

    def close(
        self, incident_id: str, terminal_state: str, *, reason: str
    ) -> BridgeIncident:
        current = self.load(incident_id)
        return self.transition(
            incident_id,
            current.state,
            terminal_state,
            reason=reason,
        )


def parse_codex_mcp_get(output: str, expected_broker: Path) -> ConfigCheck:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        if key in {"enabled", "transport", "command", "args"}:
            fields[key] = value.strip()
    if set(fields) != {"enabled", "transport", "command", "args"}:
        return ConfigCheck("invalid", "missing-fields", fields.get("command"))
    if fields["enabled"] != "true":
        return ConfigCheck("invalid", "disabled", fields["command"])
    if fields["transport"] != "stdio":
        return ConfigCheck("invalid", "wrong-transport", fields["command"])
    if fields["command"] != str(expected_broker):
        return ConfigCheck("invalid", "wrong-command", fields["command"])
    try:
        arguments = shlex.split(fields["args"])
    except ValueError:
        return ConfigCheck("invalid", "wrong-args", fields["command"])
    required = {
        "--context=codex",
        "--backend=JetBrains",
        "--add-mode=query-projects",
    }
    if (
        len(arguments) != 4
        or arguments[0] != "connect"
        or set(arguments[1:]) != required
    ):
        return ConfigCheck("invalid", "wrong-args", fields["command"])
    return ConfigCheck("healthy", "configured", fields["command"])


def check_codex_serena_config(
    codex_cli: Path, expected_broker: Path, *, timeout_seconds: float = 5
) -> ConfigCheck:
    if not codex_cli.is_file() or not os.access(codex_cli, os.X_OK):
        return ConfigCheck("unavailable", "codex-cli-missing", None)
    try:
        completed = subprocess.run(
            [str(codex_cli), "mcp", "get", "serena"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ConfigCheck("unavailable", "codex-cli-timeout", None)
    except OSError:
        return ConfigCheck("unavailable", "codex-cli-failed", None)
    if completed.returncode != 0:
        return ConfigCheck("invalid", "serena-config-missing", None)
    return parse_codex_mcp_get(completed.stdout, expected_broker)


def find_desktop_host_owner(
    root: Path,
    broker: Path,
    *,
    timeout_seconds: float = 8,
    max_output_bytes: int = 1_048_576,
) -> str | None:
    if not broker.is_file() or not os.access(broker, os.X_OK):
        return None
    try:
        completed = subprocess.run(
            [str(broker), "status", "--json"],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if (
        completed.returncode != 0
        or len(completed.stdout) > max_output_bytes
        or len(completed.stderr) > max_output_bytes
    ):
        return None
    try:
        payload = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    canonical = str(root.expanduser().resolve(strict=False))
    matching = [
        item
        for item in payload
        if isinstance(item, dict)
        and item.get("project_root") == canonical
        and item.get("backend") == "JetBrains"
        and item.get("healthy") is True
    ]
    if len(matching) != 1:
        return None
    owners = matching[0].get("owners")
    if not isinstance(owners, list) or len(owners) != 1:
        return None
    owner = owners[0]
    return owner if isinstance(owner, str) and CODEX_HOST_OWNER.fullmatch(owner) else None


def _message_bytes(message: dict[str, Any]) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"


def _initialize_request() -> bytes:
    return _message_bytes(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "workspace-harbor-bridge-doctor",
                    "version": "1",
                },
            },
        }
    )


def _post_initialize_requests() -> bytes:
    messages = (
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    return b"".join(_message_bytes(message) for message in messages)


def _stop_diagnostic_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return


def _handshake_event(
    journal: BridgeJournal | None,
    attempt_id: str,
    root: Path,
    started: float,
    stage: str,
    outcome: str,
    reason: str | None,
) -> None:
    if journal is None:
        return
    journal.append(
        BridgeEvent(
            attempt_id=attempt_id,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            root_digest=root_digest(root),
            service_key=None,
            owner_source=None,
            stage=stage,
            outcome=outcome,
            reason=reason,
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        )
    )


def _result(
    status: str,
    reason: str,
    *,
    initialized: float | None,
    tools_listed: float | None,
    started: float,
    tool_count: int = 0,
    expected_tool_found: bool = False,
    process: subprocess.Popen[bytes] | None = None,
) -> HandshakeResult:
    return HandshakeResult(
        status=status,
        reason=reason,
        initialize_ms=(
            max(0, round((initialized - started) * 1000))
            if initialized is not None
            else None
        ),
        tools_list_ms=(
            max(0, round((tools_listed - initialized) * 1000))
            if tools_listed is not None and initialized is not None
            else None
        ),
        tool_count=tool_count,
        expected_tool_found=expected_tool_found,
        proxy_exit=process.poll() if process is not None else None,
        process_pid=process.pid if process is not None else None,
    )


def run_handshake(
    root: Path,
    broker: Path,
    *,
    timeout_seconds: float = 12,
    max_output_bytes: int = 2_097_152,
    journal: BridgeJournal | None = None,
    extra_environment: dict[str, str] | None = None,
) -> HandshakeResult:
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        return _result(
            "unavailable",
            "project-missing",
            initialized=None,
            tools_listed=None,
            started=time.monotonic(),
        )
    if not broker.is_file() or not os.access(broker, os.X_OK):
        return _result(
            "unavailable",
            "broker-missing",
            initialized=None,
            tools_listed=None,
            started=time.monotonic(),
        )
    attempt_id = uuid.uuid4().hex
    started = time.monotonic()
    environment = os.environ.copy()
    # The desktop MCP launcher owns services by its validated Codex host because
    # it does not receive per-task lineage. Mirror that launch surface so a
    # doctor invoked from a task does not manufacture an ownership conflict.
    environment.pop("CODEX_THREAD_ID", None)
    environment.pop("WORKSPACE_HARBOR_OWNER_ID", None)
    if extra_environment:
        environment.update(extra_environment)
    command: Sequence[str] = (
        str(broker),
        "connect",
        "--project",
        str(resolved_root),
        "--context=codex",
        "--backend=JetBrains",
        "--add-mode=query-projects",
    )
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
            env=environment,
        )
    except OSError:
        _handshake_event(
            journal,
            attempt_id,
            resolved_root,
            started,
            "initialize",
            "failed",
            "broker-launch-failed",
        )
        return _result(
            "unavailable",
            "broker-launch-failed",
            initialized=None,
            tools_listed=None,
            started=started,
        )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    initialized: float | None = None
    tools_listed: float | None = None
    tool_count = 0
    expected_found = False
    total_bytes = 0
    stdout_buffer = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    try:
        process.stdin.write(_initialize_request())
        process.stdin.flush()
        deadline = started + timeout_seconds
        failure: str | None = None
        while tools_listed is None and failure is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = (
                    "initialize-timeout"
                    if initialized is None
                    else "tools-list-timeout"
                )
                break
            events = selector.select(timeout=min(remaining, 0.05))
            if not events and process.poll() is not None:
                failure = "initialize-eof" if initialized is None else "tools-list-eof"
                break
            for key, _ in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total_bytes += len(chunk)
                if total_bytes > max_output_bytes:
                    failure = "output-limit"
                    break
                if key.data == "stderr":
                    continue
                stdout_buffer.extend(chunk)
                if len(stdout_buffer) > 1_048_576 and b"\n" not in stdout_buffer:
                    failure = "output-limit"
                    break
                while b"\n" in stdout_buffer:
                    line, _, remainder = stdout_buffer.partition(b"\n")
                    stdout_buffer = bytearray(remainder)
                    try:
                        message = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        failure = "protocol-error"
                        break
                    if not isinstance(message, dict):
                        failure = "protocol-error"
                        break
                    if message.get("id") == 1:
                        if "error" in message or not isinstance(
                            message.get("result"), dict
                        ):
                            failure = "initialize-error"
                            break
                        initialized = time.monotonic()
                        process.stdin.write(_post_initialize_requests())
                        process.stdin.flush()
                        _handshake_event(
                            journal,
                            attempt_id,
                            resolved_root,
                            started,
                            "initialize",
                            "ok",
                            None,
                        )
                    elif message.get("id") == 2:
                        if initialized is None:
                            failure = "protocol-error"
                            break
                        result_payload = message.get("result")
                        tools = (
                            result_payload.get("tools")
                            if isinstance(result_payload, dict)
                            else None
                        )
                        if "error" in message or not isinstance(tools, list):
                            failure = "tools-list-error"
                            break
                        names = {
                            item.get("name")
                            for item in tools
                            if isinstance(item, dict) and isinstance(item.get("name"), str)
                        }
                        tool_count = len(names)
                        expected_found = bool(names & EXPECTED_SERENA_TOOLS)
                        tools_listed = time.monotonic()
                        if not expected_found:
                            failure = "expected-tool-missing"
                        break
                if failure is not None or tools_listed is not None:
                    break
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if failure is not None:
            stage = "initialize" if initialized is None else "tools-list"
            _handshake_event(
                journal, attempt_id, resolved_root, started, stage, "failed", failure
            )
            _stop_diagnostic_group(process)
            return _result(
                "failed",
                failure,
                initialized=initialized,
                tools_listed=tools_listed,
                started=started,
                tool_count=tool_count,
                expected_tool_found=expected_found,
                process=process,
            )
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _stop_diagnostic_group(process)
        exit_code = process.poll()
        if exit_code not in {0, None}:
            _handshake_event(
                journal, attempt_id, resolved_root, started,
                "handshake-cleanup", "failed", "proxy-exit-nonzero",
            )
            return _result(
                "failed",
                "proxy-exit-nonzero",
                initialized=initialized,
                tools_listed=tools_listed,
                started=started,
                tool_count=tool_count,
                expected_tool_found=expected_found,
                process=process,
            )
        _handshake_event(
            journal, attempt_id, resolved_root, started, "tools-list", "ok", None
        )
        return _result(
            "healthy",
            "handshake-complete",
            initialized=initialized,
            tools_listed=tools_listed,
            started=started,
            tool_count=tool_count,
            expected_tool_found=expected_found,
            process=process,
        )
    finally:
        selector.close()
        if process.poll() is None:
            _stop_diagnostic_group(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
