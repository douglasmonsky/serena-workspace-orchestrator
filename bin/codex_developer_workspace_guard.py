"""Policy for keeping Codex coding projects out of the Documents directory."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re
import shlex


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
