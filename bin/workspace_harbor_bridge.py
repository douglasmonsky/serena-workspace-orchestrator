"""Privacy-safe observability and diagnostics for the Serena MCP bridge."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


BRIDGE_SCHEMA_VERSION = 1
DEFAULT_JOURNAL_MAX_BYTES = 524_288
DEFAULT_JOURNAL_BACKUPS = 4
MAX_REASON_LENGTH = 96
HEX_20 = re.compile(r"^[0-9a-f]{20}$")
HEX_24 = re.compile(r"^[0-9a-f]{24}$")
HEX_32 = re.compile(r"^[0-9a-f]{32}$")
REASON_CODE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALLOWED_OWNER_SOURCES = frozenset(
    {"explicit", "root-thread", "subagent-lineage", "codex-host", "process-fallback"}
)
ALLOWED_STAGES = frozenset(
    {
        "project-resolution",
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
