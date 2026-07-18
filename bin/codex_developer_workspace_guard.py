"""Policy for keeping Codex coding projects out of the Documents directory."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import tomllib


BLOCK_REASON = "documents-project-write-blocked"
_SHELL_TOOL_NAMES = frozenset({"bash", "shell", "exec", "exec_command", "unified_exec"})
_PATCH_TOOL_FRAGMENTS = ("apply_patch", "write_file", "edit_file", "create_file")
_MUTATING_GIT_COMMANDS = frozenset(
    {
        "add",
        "checkout",
        "clean",
        "clone",
        "commit",
        "fetch",
        "init",
        "merge",
        "mv",
        "pull",
        "push",
        "rebase",
        "reset",
        "restore",
        "rm",
        "submodule",
        "switch",
        "tag",
        "worktree",
    }
)
_PROJECT_COMMAND_PREFIXES = (
    ("npm", "create"),
    ("npm", "init"),
    ("npm", "install"),
    ("npm", "i"),
    ("npx",),
    ("pnpm", "create"),
    ("pnpm", "init"),
    ("pnpm", "install"),
    ("yarn", "create"),
    ("yarn", "init"),
    ("yarn", "install"),
    ("bun", "create"),
    ("bun", "init"),
    ("bun", "install"),
    ("uv", "init"),
    ("uv", "add"),
    ("uv", "sync"),
    ("cargo", "new"),
    ("cargo", "init"),
    ("cargo", "add"),
    ("poetry", "new"),
    ("poetry", "init"),
    ("poetry", "install"),
    ("dotnet", "new"),
    ("django-admin", "startproject"),
    ("rails", "new"),
    ("python", "-m", "pip", "install"),
    ("python3", "-m", "pip", "install"),
    ("pip", "install"),
    ("pip3", "install"),
)
_PATCH_HEADER = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
_REDIRECTION = re.compile(
    r"(?:^|\s)(?:[012]?>|>>)(?:\s*)(?P<path>'[^']+'|\"[^\"]+\"|[^\s;&|]+)"
)


@dataclass(frozen=True)
class Decision:
    """A stable hook policy result."""

    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class InstallResult:
    """Summary of an idempotent global guard installation."""

    hook_changed: bool
    project_records_removed: int = 0


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_literal(raw: str, workdir: Path, home: Path) -> Path | None:
    value = raw.strip()
    if not value or any(marker in value for marker in ("$", "`", "*", "?", "[")):
        return None
    if value == "~":
        candidate = home
    elif value.startswith("~/"):
        candidate = home / value[2:]
    else:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = workdir / candidate
    return candidate.resolve(strict=False)


def _event_workdir(
    event: Mapping[str, object],
    tool_input: Mapping[str, object],
    home: Path,
) -> Path:
    raw = tool_input.get("workdir") or event.get("cwd") or event.get("current_working_directory")
    if not isinstance(raw, str):
        return home
    return _resolve_literal(raw, home, home) or home


def _command_text(tool_input: Mapping[str, object]) -> str:
    for key in ("cmd", "command", "script"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return ""


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _non_option_tokens(tokens: list[str]) -> list[str]:
    return [token for token in tokens if not token.startswith("-")]


def _is_migration(command: str, workdir: Path, protected: Path, approved: tuple[Path, ...], home: Path) -> bool:
    if any(operator in command for operator in (";", "&&", "||", "|", "\n")):
        return False
    tokens = _tokens(command)
    if not tokens or Path(tokens[0]).name not in {"cp", "mv", "ditto", "rsync"}:
        return False
    arguments = _non_option_tokens(tokens[1:])
    if len(arguments) < 2:
        return False
    source_paths = [_resolve_literal(value, workdir, home) for value in arguments[:-1]]
    destination = _resolve_literal(arguments[-1], workdir, home)
    return bool(
        destination
        and all(source is not None and _is_within(source, protected) for source in source_paths)
        and any(_is_within(destination, root) for root in approved)
    )


def _git_operation(tokens: list[str]) -> tuple[str, list[str]] | None:
    if not tokens or Path(tokens[0]).name != "git":
        return None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-C" and index + 1 < len(tokens):
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token.lower(), tokens[index + 1 :]
    return None


def _explicit_project_targets(
    operation: str,
    arguments: list[str],
    workdir: Path,
    home: Path,
) -> tuple[Path, ...]:
    candidates: list[str] = []
    if operation == "init":
        candidates = _non_option_tokens(arguments)
    elif operation == "clone":
        values = _non_option_tokens(arguments)
        if len(values) >= 2:
            candidates = [values[-1]]
    elif operation == "worktree" and "add" in arguments:
        add_index = arguments.index("add")
        values = _non_option_tokens(arguments[add_index + 1 :])
        if values:
            candidates = [values[0]]
    return tuple(
        path
        for candidate in candidates
        if (path := _resolve_literal(candidate, workdir, home)) is not None
    )


def _has_project_command(tokens: list[str]) -> bool:
    lowered = tuple(Path(token).name.lower() if index == 0 else token.lower() for index, token in enumerate(tokens))
    return any(lowered[: len(prefix)] == prefix for prefix in _PROJECT_COMMAND_PREFIXES)


def _redirection_targets(command: str, workdir: Path, home: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for match in _REDIRECTION.finditer(command):
        raw = match.group("path")
        unquoted = _tokens(raw)
        if len(unquoted) != 1:
            continue
        path = _resolve_literal(unquoted[0], workdir, home)
        if path is not None:
            paths.append(path)
    return tuple(paths)


def _patch_targets(tool_input: Mapping[str, object], workdir: Path, home: Path) -> tuple[Path, ...]:
    candidates: list[str] = []
    for key in ("path", "file", "file_path", "absolute_file_path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            candidates.append(value)
    patch = tool_input.get("patch") or tool_input.get("input")
    if isinstance(patch, str):
        candidates.extend(_PATCH_HEADER.findall(patch))
    return tuple(
        path
        for candidate in candidates
        if (path := _resolve_literal(candidate, workdir, home)) is not None
    )


def _is_source_write(
    tool_name: str,
    tool_input: Mapping[str, object],
    workdir: Path,
    protected: Path,
    home: Path,
) -> bool:
    if any(fragment in tool_name for fragment in _PATCH_TOOL_FRAGMENTS):
        return any(_is_within(path, protected) for path in _patch_targets(tool_input, workdir, home))
    command = _command_text(tool_input)
    return any(_is_within(path, protected) for path in _redirection_targets(command, workdir, home))


def _is_project_mutation(
    tool_name: str,
    tool_input: Mapping[str, object],
    workdir: Path,
    protected: Path,
    home: Path,
) -> bool:
    if tool_name not in _SHELL_TOOL_NAMES and not any(name in tool_name for name in _SHELL_TOOL_NAMES):
        return False
    command = _command_text(tool_input)
    tokens = _tokens(command)
    if not tokens:
        return False
    git_operation = _git_operation(tokens)
    if git_operation is not None:
        operation, arguments = git_operation
        if operation not in _MUTATING_GIT_COMMANDS:
            return False
        targets = _explicit_project_targets(operation, arguments, workdir, home)
        return _is_within(workdir, protected) or any(_is_within(path, protected) for path in targets)
    if _has_project_command(tokens):
        if _is_within(workdir, protected):
            return True
        return any(
            _is_within(path, protected)
            for token in tokens[1:]
            if (path := _resolve_literal(token, workdir, home)) is not None
        )
    return False


def classify_event(event: Mapping[str, object], home: Path | None = None) -> Decision:
    """Classify one Codex hook event without mutating external state."""

    account_home = (home or Path.home()).resolve()
    protected = (account_home / "Documents").resolve(strict=False)
    approved = (
        (account_home / "Developer/Codex").resolve(strict=False),
        (account_home / ".codex/src").resolve(strict=False),
    )
    tool_name = str(event.get("tool_name") or event.get("toolName") or "").strip().lower()
    raw_tool_input = event.get("tool_input") or event.get("toolInput") or {}
    if not isinstance(raw_tool_input, Mapping):
        return Decision(True)
    workdir = _event_workdir(event, raw_tool_input, account_home)
    command = _command_text(raw_tool_input)
    if _is_migration(command, workdir, protected, approved, account_home):
        return Decision(True)
    if _is_source_write(tool_name, raw_tool_input, workdir, protected, account_home):
        return Decision(False, BLOCK_REASON)
    if _is_project_mutation(tool_name, raw_tool_input, workdir, protected, account_home):
        return Decision(False, BLOCK_REASON)
    return Decision(True)


def decision_payload(decision: Decision) -> dict[str, object]:
    """Return the supported Codex PreToolUse response envelope."""

    action = "allow" if decision.allowed else "deny"
    reason = (
        "Developer workspace policy allows this operation."
        if decision.allowed
        else (
            f"{decision.reason}: coding projects must be created or migrated under "
            "/Users/Monsky/Developer/Codex before repository changes are made."
        )
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": action,
            "permissionDecisionReason": reason,
        }
    }


def _guard_hook_entry(codex_home: Path) -> dict[str, object]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": str(codex_home / "bin/codex-developer-workspace-guard"),
            }
        ]
    }


def _is_guard_hook_entry(entry: object) -> bool:
    if not isinstance(entry, Mapping):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    for hook in hooks:
        if not isinstance(hook, Mapping):
            continue
        command = hook.get("command")
        if isinstance(command, str) and Path(command).name == "codex-developer-workspace-guard":
            return True
    return False


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_file(path: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup.parent, 0o700)
    shutil.copy2(path, backup)


def install_hook(codex_home: Path) -> bool:
    """Install the global hook once while preserving unrelated hook entries."""

    home = codex_home.expanduser().resolve()
    hooks_path = home / "hooks.json"
    if hooks_path.exists():
        try:
            document = json.loads(hooks_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("hooks.json is not valid JSON") from error
    else:
        document = {"hooks": {}}
    if not isinstance(document, dict):
        raise ValueError("hooks.json root must be an object")
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json hooks must be an object")
    entries = hooks.setdefault("PreToolUse", [])
    if not isinstance(entries, list):
        raise ValueError("hooks.json PreToolUse must be a list")
    desired = _guard_hook_entry(home)
    retained = [entry for entry in entries if not _is_guard_hook_entry(entry)]
    updated = [*retained, desired]
    if updated == entries:
        os.chmod(hooks_path, 0o600)
        return False
    if hooks_path.is_file():
        backup = home / "backups/developer-workspace-guard" / _timestamp() / "hooks.json"
        _backup_file(hooks_path, backup)
    hooks["PreToolUse"] = updated
    serialized = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    _atomic_write(hooks_path, serialized, 0o600)
    return True


def command_hook_hash(
    *,
    event_name: str,
    matcher: str | None,
    command: str,
    timeout: int = 600,
    asynchronous: bool = False,
) -> str:
    """Return Codex's canonical trust fingerprint for a command hook."""

    identity: dict[str, object] = {
        "event_name": event_name,
        "hooks": [
            {
                "async": asynchronous,
                "command": command,
                "timeout": max(1, timeout),
                "type": "command",
            }
        ],
    }
    if matcher is not None:
        identity["matcher"] = matcher
    serialized = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(serialized).hexdigest()}"


def _guard_hook_identity(codex_home: Path) -> tuple[str, str, str | None, str]:
    hooks_path = codex_home / "hooks.json"
    document = json.loads(hooks_path.read_text(encoding="utf-8"))
    entries = document["hooks"]["PreToolUse"]
    for group_index, entry in enumerate(entries):
        if not _is_guard_hook_entry(entry):
            continue
        matcher = entry.get("matcher")
        for handler_index, handler in enumerate(entry["hooks"]):
            command = handler.get("command")
            if isinstance(command, str) and Path(command).name == "codex-developer-workspace-guard":
                key = f"{hooks_path}:pre_tool_use:{group_index}:{handler_index}"
                return key, command_hook_hash(
                    event_name="pre_tool_use",
                    matcher=matcher if isinstance(matcher, str) else None,
                    command=command,
                ), matcher, command
    raise ValueError("installed workspace guard hook could not be located")


_PROJECT_TABLE_HEADER = re.compile(
    r'^\s*\[projects\.(?P<key>"(?:\\.|[^"\\])*")\]\s*(?:#.*)?$'
)
_ANY_TABLE_HEADER = re.compile(r"^\s*\[\[?.+\]\]?\s*(?:#.*)?$")
_HOOK_STATE_TABLE_HEADER = re.compile(
    r'^\s*\[hooks\.state\.(?P<key>"(?:\\.|[^"\\])*")\]\s*(?:#.*)?$'
)
_TRUSTED_HASH_LINE = re.compile(r"^\s*trusted_hash\s*=")


def _decode_toml_string(literal: str) -> str:
    parsed = tomllib.loads(f"value = {literal}\n")
    value = parsed["value"]
    if not isinstance(value, str):
        raise ValueError("project table key is not a string")
    return value


def remove_documents_project_tables(text: str, protected_root: Path) -> tuple[str, int]:
    """Remove only project tables whose decoded path is below Documents."""

    tomllib.loads(text)
    protected = protected_root.expanduser().resolve(strict=False)
    output: list[str] = []
    skipping = False
    removed = 0
    for line in text.splitlines(keepends=True):
        if _ANY_TABLE_HEADER.match(line):
            skipping = False
            match = _PROJECT_TABLE_HEADER.match(line)
            if match is not None:
                project_path = Path(_decode_toml_string(match.group("key"))).expanduser().resolve(strict=False)
                if _is_within(project_path, protected):
                    skipping = True
                    removed += 1
        if not skipping:
            output.append(line)
    candidate = "".join(output)
    tomllib.loads(candidate)
    return candidate, removed


def clean_project_records(codex_home: Path, account_home: Path | None = None) -> int:
    """Remove stale Documents project trust tables with a recoverable backup."""

    home = codex_home.expanduser().resolve()
    config_path = home / "config.toml"
    if not config_path.is_file():
        return 0
    original = config_path.read_text(encoding="utf-8")
    protected = ((account_home or Path.home()) / "Documents").resolve(strict=False)
    cleaned, removed = remove_documents_project_tables(original, protected)
    if removed == 0:
        return 0
    backup = home / "config.toml.backup-before-workspace-guard-20260718"
    if not backup.exists():
        _backup_file(config_path, backup)
    mode = config_path.stat().st_mode & 0o777
    _atomic_write(config_path, cleaned.encode("utf-8"), mode)
    return removed


def upsert_hook_trust(text: str, key: str, trusted_hash: str) -> str:
    """Update one hook-state trust entry without reformatting unrelated config."""

    tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    target_start: int | None = None
    target_end = len(lines)
    for index, line in enumerate(lines):
        if target_start is not None and _ANY_TABLE_HEADER.match(line):
            target_end = index
            break
        match = _HOOK_STATE_TABLE_HEADER.match(line)
        if match is not None and _decode_toml_string(match.group("key")) == key:
            target_start = index

    trusted_line = f'trusted_hash = "{trusted_hash}"\n'
    if target_start is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend(
            [
                f"[hooks.state.{json.dumps(key)}]\n",
                trusted_line,
            ]
        )
    else:
        trusted_index = next(
            (
                index
                for index in range(target_start + 1, target_end)
                if _TRUSTED_HASH_LINE.match(lines[index])
            ),
            None,
        )
        if trusted_index is None:
            lines.insert(target_start + 1, trusted_line)
        else:
            lines[trusted_index] = trusted_line
    candidate = "".join(lines)
    tomllib.loads(candidate)
    return candidate


def persist_guard_trust(codex_home: Path) -> bool:
    """Persist the installed guard's supported Codex trust fingerprint."""

    home = codex_home.expanduser().resolve()
    config_path = home / "config.toml"
    original = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    key, trusted_hash, _, _ = _guard_hook_identity(home)
    updated = upsert_hook_trust(original, key, trusted_hash)
    if updated == original:
        return False
    backup = home / "config.toml.backup-before-workspace-guard-20260718"
    if config_path.is_file() and not backup.exists():
        _backup_file(config_path, backup)
    mode = config_path.stat().st_mode & 0o777 if config_path.is_file() else 0o600
    _atomic_write(config_path, updated.encode("utf-8"), mode)
    return True


def install_guard(codex_home: Path, clean_records: bool = False) -> InstallResult:
    """Install the hook and optionally clean public project trust records."""

    home = codex_home.expanduser().resolve()
    config_path = home / "config.toml"
    if config_path.is_file():
        try:
            tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError("config.toml is not valid TOML") from error
    hook_changed = install_hook(codex_home)
    persist_guard_trust(codex_home)
    removed = clean_project_records(codex_home) if clean_records else 0
    return InstallResult(hook_changed=hook_changed, project_records_removed=removed)
