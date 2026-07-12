#!/usr/bin/env python3
"""Pure, fail-closed bootstrap evidence and deterministic plan selection."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
import fcntl
from pathlib import Path
import subprocess
import tempfile
from typing import Any

import yaml

RECIPE_VERSION = 1
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CODEX_TASK = Path(os.environ.get("CODEX_TASK", CODEX_HOME / "bin/codex-task"))
PRUNED_DIRECTORIES = {".git", ".idea", ".serena", ".venv", "venv", "node_modules", "target", "build", "dist", "vendor", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
STATE_VERSION = 1


@dataclass(frozen=True)
class BootstrapPlan:
    plan_id: str
    source: str
    ecosystem: str
    cwd: str
    argv: tuple[str, ...]
    inputs: tuple[str, ...]
    markers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return asdict(self) | {"argv": list(self.argv), "inputs": list(self.inputs), "markers": list(self.markers)}


def resolve_root(value: str | Path | None) -> Path:
    candidate = Path(value).expanduser() if value is not None else Path.cwd()
    root = candidate.resolve(strict=True)
    if not root.is_dir(): raise ValueError(f"not a directory: {root}")
    return root


def _mapping(path: Path) -> dict[str, object]:
    if not path.is_file(): return {}
    try: value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error: raise ValueError(f"invalid YAML: {path}") from error
    if value is None: return {}
    if not isinstance(value, dict): raise ValueError(f"mapping required: {path}")
    return value


def load_policy(root: Path) -> dict[str, object]:
    root = resolve_root(root)
    policy: dict[str, object] = {}
    global_config = CODEX_HOME / "serena-integration.yml"
    for path in (global_config, root / ".serena/codex-integration.yml"):
        data = _mapping(path)
        policy = _deep_merge(policy, data)
    bootstrap = policy.get("bootstrap", {})
    if not isinstance(bootstrap, dict): raise ValueError("bootstrap must be a mapping")
    if "task" in bootstrap and "command" in bootstrap: raise ValueError("bootstrap task and command are mutually exclusive")
    if "enabled" in bootstrap and type(bootstrap["enabled"]) is not bool: raise ValueError("bootstrap enabled must be boolean")
    if "use_builtin_recipes" in bootstrap and type(bootstrap["use_builtin_recipes"]) is not bool: raise ValueError("use_builtin_recipes must be boolean")
    for key in ("task",):
        if key in bootstrap and (not isinstance(bootstrap[key], str) or not bootstrap[key]): raise ValueError("task must be a non-empty string")
    if "command" in bootstrap:
        command = bootstrap["command"]
        if not isinstance(command, dict) or set(command) - {"argv", "cwd", "inputs", "markers"}: raise ValueError("invalid command schema")
        if not isinstance(command.get("argv"), list) or not command["argv"] or not all(isinstance(v, str) and v for v in command["argv"]): raise ValueError("command argv must be non-empty strings")
        for key in ("inputs", "markers"):
            if key in command and (not isinstance(command[key], list) or not all(isinstance(v, str) and v for v in command[key])): raise ValueError(f"command {key} must be strings")
        if "cwd" in command and not isinstance(command["cwd"], str): raise ValueError("command cwd must be string")
    boundaries = bootstrap.get("boundaries", {})
    if not isinstance(boundaries, dict) or set(boundaries) - {"include", "ignore"}: raise ValueError("invalid boundaries schema")
    for value in boundaries.values():
        if not isinstance(value, list): raise ValueError("boundary values must be lists")
    return policy


def _deep_merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    result = dict(base)
    for key, value in override.items():
        old = result.get(key)
        result[key] = _deep_merge(old, value) if isinstance(old, dict) and isinstance(value, dict) else value
    return result


def repository_identity(root: Path) -> str:
    root = resolve_root(root)
    try:
        output = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True, timeout=5, check=False)
        common = Path(output.stdout.strip()).resolve() if output.returncode == 0 else root
    except (OSError, subprocess.SubprocessError): common = root
    return hashlib.sha256(str(common).encode()).hexdigest()


def _state_dir() -> Path:
    return Path(os.environ.get("WORKSPACE_HARBOR_BOOTSTRAP_STATE_DIR", CODEX_HOME / "state/workspace-harbor-bootstrap"))


def _repository_path(root: Path) -> Path:
    return _state_dir() / "repositories" / (repository_identity(root) + ".json")


def _read_state(path: Path) -> dict[str, object]:
    if not path.exists(): return {"version": STATE_VERSION, "decisions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error: raise ValueError("corrupt bootstrap state") from error
    if not isinstance(data, dict) or set(data) != {"version", "decisions"} or data["version"] != STATE_VERSION or not isinstance(data["decisions"], dict): raise ValueError("invalid bootstrap state")
    allowed = {"language": {"enable", "ignore"}, "tracking": {"shared", "local"}, "command": {"approve", "reject"}}
    for key, item in data["decisions"].items():
        if not isinstance(key, str) or ":" not in key or not isinstance(item, dict) or set(item) != {"decision", "evidence"}: raise ValueError("invalid bootstrap decision")
        category = key.split(":", 1)[0]
        if category not in allowed or item["decision"] not in allowed[category] or not isinstance(item["evidence"], str) or not item["evidence"]: raise ValueError("invalid bootstrap decision")
    return data


def _write_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path.parent.parent, 0o700); os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(state, output, sort_keys=True); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        try: os.unlink(temporary)
        except OSError: pass
        raise


def repository_decisions(root: Path) -> dict[str, object]:
    """Read private repository-scoped decisions; corrupt state is never trusted."""
    return _read_state(_repository_path(root))["decisions"]


def language_decision(root: Path, language: str) -> str | None:
    item = repository_decisions(root).get(f"language:{language}")
    return item["decision"] if isinstance(item, dict) and item.get("evidence") == language_evidence(root, language) else None


def _decision_subject(root: Path, category: str, subject: str) -> str:
    if category == "language":
        if subject not in {"python", "rust", "go", "java", "kotlin", "typescript", "svelte", "vue", "angular", "csharp", "php", "ruby", "swift"}: raise ValueError("unknown language")
        return language_evidence(root, subject)
    if category == "tracking": return "tracking"
    if category == "command":
        plans = plan_repository(root)["plans"]
        command = next((p for p in plans if p["source"] == "command"), None)
        if command is None: raise ValueError("no current custom command")
        material = json.dumps({key: command[key] for key in ("argv", "cwd", "inputs", "markers")}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode()).hexdigest()
    raise ValueError("unknown decision category")


def record_decision(root: Path, category: str, subject: str, decision: str) -> dict[str, object]:
    allowed = {"language": {"enable", "ignore"}, "tracking": {"shared", "local"}, "command": {"approve", "reject"}}
    if category not in allowed or decision not in allowed[category]: raise ValueError("invalid decision")
    root = resolve_root(root); key = _decision_subject(root, category, subject)
    path = _repository_path(root); lock_path = _state_dir() / "locks" / (repository_identity(root) + ".lock")
    lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600); fcntl.flock(lock, fcntl.LOCK_EX)
        state = _read_state(path); decisions = state["decisions"]
        decisions[f"{category}:{subject}"] = {"decision": decision, "evidence": key}
        _write_state(path, state)
    return {"repository": repository_identity(root), "category": category, "subject": subject, "decision": decision}


def _contains_source(root: Path, suffixes: set[str]) -> bool:
    for path, directories, files in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if d not in PRUNED_DIRECTORIES and not (Path(path) / d).is_symlink()]
        if any(Path(name).suffix.lower() in suffixes for name in files): return True
    return False


def language_evidence(root: Path, language: str) -> str:
    """Report repository facts only; Task 3 combines them with language policy decisions."""
    root = resolve_root(root); language = language.lower()
    files = {p.name for p in root.iterdir()}
    rules = {
        "python": ({".py", ".pyi"}, "pyproject.toml" in files and bool({"uv.lock", "poetry.lock"} & files)),
        "rust": ({".rs"}, {"Cargo.toml", "Cargo.lock"} <= files),
        "go": ({".go"}, {"go.mod", "go.sum"} <= files),
        "java": ({".java"}, bool({"build.gradle", "build.gradle.kts", "pom.xml"} & files)),
        "kotlin": ({".kt", ".kts"}, bool({"build.gradle", "build.gradle.kts", "pom.xml"} & files)),
        "typescript": ({".ts", ".tsx", ".js", ".jsx"}, "package.json" in files and bool({"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"} & files)),
        "svelte": ({".svelte"}, "package.json" in files and bool({"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"} & files)),
        "vue": ({".vue"}, "package.json" in files and bool({"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"} & files)),
        "angular": ({".ts"}, "angular.json" in files and "package.json" in files),
        "csharp": ({".cs"}, any(root.glob("*.csproj"))), "php": ({".php"}, "composer.lock" in files),
        "ruby": ({".rb"}, "Gemfile.lock" in files), "swift": ({".swift"}, "Package.resolved" in files),
    }
    suffixes, confirmed = rules.get(language, (set(), False))
    source = _contains_source(root, suffixes)
    return "confirmed" if source and confirmed else "source-only" if source else "absent"


def _plan(source: str, ecosystem: str, cwd: Path, argv: list[str], inputs: list[Path], markers: list[str] | None = None) -> BootstrapPlan:
    # Stable structural identity; Task 2 hashes every declared input for execution freshness.
    digest = hashlib.sha256((source + ecosystem + str(cwd) + "\0".join(argv)).encode()).hexdigest()[:16]
    return BootstrapPlan(digest, source, ecosystem, str(cwd), tuple(argv), tuple(str(p) for p in inputs), tuple(markers or ()))


def _boundary(root: Path, value: object) -> Path:
    if not isinstance(value, str): raise ValueError("boundary must be a relative path")
    try: candidate = (root / value).resolve(strict=True)
    except OSError as error: raise ValueError("boundary escapes root or is not a directory") from error
    if candidate == root or root not in candidate.parents or not candidate.is_dir(): raise ValueError("boundary escapes root or is not a directory")
    return candidate


def _builtin(boundary: Path) -> tuple[list[BootstrapPlan], list[dict[str, str]]]:
    present = lambda name: (boundary / name).is_file()
    plans: list[BootstrapPlan] = []; decisions: list[dict[str, str]] = []
    js = [name for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb") if present(name)]
    if present("package.json") and len({"bun" if n.startswith("bun") else n for n in js}) > 1:
        return [], [{"code": "ambiguous-javascript-manager", "path": str(boundary)}]
    if present("package.json") and js:
        lock = js[0]
        try: package = json.loads((boundary / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): return [], [{"code": "invalid-package-json", "path": str(boundary)}]
        if not isinstance(package, dict): return [], [{"code": "invalid-package-json", "path": str(boundary)}]
        if lock == "package-lock.json": argv, eco = ["npm", "ci"], "npm"
        elif lock == "pnpm-lock.yaml": argv, eco = ["pnpm", "install", "--frozen-lockfile"], "pnpm"
        elif lock.startswith("bun"): argv, eco = ["bun", "install", "--frozen-lockfile"], "bun"
        elif (isinstance(package.get("packageManager"), str) and package["packageManager"].startswith("yarn@")) or (boundary / ".yarnrc.yml").is_file(): argv, eco = ["yarn", "install", "--immutable"], "yarn-berry"
        else: argv, eco = ["yarn", "install", "--frozen-lockfile"], "yarn-classic"
        plans.append(_plan("recipe", eco, boundary, argv, [boundary / "package.json", boundary / lock]))
    if present("pyproject.toml") and present("uv.lock"): plans.append(_plan("recipe", "uv", boundary, ["uv", "sync", "--frozen"], [boundary / "pyproject.toml", boundary / "uv.lock"]))
    elif present("pyproject.toml") and present("poetry.lock") and "[tool.poetry]" in (boundary / "pyproject.toml").read_text(errors="ignore"): plans.append(_plan("recipe", "poetry", boundary, ["poetry", "install", "--sync", "--no-interaction"], [boundary / "pyproject.toml", boundary / "poetry.lock"]))
    if present("Cargo.toml") and present("Cargo.lock") and _contains_source(boundary, {".rs"}): plans.append(_plan("recipe", "rust", boundary, ["cargo", "fetch", "--locked"], [boundary / "Cargo.toml", boundary / "Cargo.lock"]))
    if present("go.mod") and present("go.sum") and _contains_source(boundary, {".go"}): plans.append(_plan("recipe", "go", boundary, ["go", "mod", "download"], [boundary / "go.mod", boundary / "go.sum"]))
    if any(present(name) for name in ("build.gradle", "build.gradle.kts", "pom.xml")): plans.append(_plan("recipe", "ide-managed", boundary, [], [boundary / n for n in ("build.gradle", "build.gradle.kts", "pom.xml") if present(n)]))
    return plans, decisions


def _task_plan(root: Path, name: str) -> BootstrapPlan | None:
    try: result = subprocess.run([str(CODEX_TASK), "list", "--json"], cwd=root, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError): return None
    if result.returncode != 0: return None
    try: data = json.loads(result.stdout)
    except json.JSONDecodeError: return None
    names = data if isinstance(data, list) else data.get("tasks", []) if isinstance(data, dict) else []
    if name not in [x if isinstance(x, str) else x.get("name") for x in names if isinstance(x, (str, dict))]: return None
    taskfile = root / ".codex/tasks.toml"
    return _plan("task", "task", root, [str(CODEX_TASK), name, "--json"], [taskfile])


def _input(root: Path, value: str) -> Path:
    if Path(value).is_absolute(): raise ValueError("input must be relative")
    candidate = (root / value).resolve(strict=False)
    if candidate != root and root not in candidate.parents: raise ValueError("input escapes root")
    return candidate


def plan_repository(root: Path) -> dict[str, object]:
    root = resolve_root(root)
    try: policy = load_policy(root)
    except ValueError as error: return {"status": "needs-decision", "root": str(root), "plans": [], "decisions": [{"code": "invalid-policy", "message": str(error)}], "policy_source": str(root / ".serena/codex-integration.yml"), "recipe_version": RECIPE_VERSION}
    bootstrap = policy.get("bootstrap", {}); assert isinstance(bootstrap, dict)
    base = {"root": str(root), "decisions": [], "policy_source": str(root / ".serena/codex-integration.yml"), "recipe_version": RECIPE_VERSION}
    if bootstrap.get("enabled") is False: return {"status": "disabled", "plans": [], **base}
    command = bootstrap.get("command")
    if command is not None:
        assert isinstance(command, dict)
        cwd = root / command.get("cwd", ".")
        if not cwd.is_dir() or cwd.resolve() != root and root not in cwd.resolve().parents: return {"status": "needs-decision", "plans": [], "decisions": [{"code": "invalid-command"}], **{k:v for k,v in base.items() if k != "decisions"}}
        try: inputs = [_input(root, p) for p in command.get("inputs", [])]; markers = [_input(root, p) for p in command.get("markers", [])]
        except ValueError: return {"status": "needs-decision", "plans": [], "decisions": [{"code": "invalid-command"}], **{k:v for k,v in base.items() if k != "decisions"}}
        plans = [_plan("command", "custom", cwd.resolve(), command["argv"], inputs, [str(p) for p in markers])]
        if not bootstrap.get("use_builtin_recipes", False): return {"status": "ready", "plans": [p.as_dict() for p in plans], **base}
    else: plans = []
    if "task" in bootstrap:
        planned = _task_plan(root, bootstrap["task"])
        if not planned: return {"status": "needs-decision", "plans": [], "decisions": [{"code": "missing-configured-task"}], **{k:v for k,v in base.items() if k != "decisions"}}
        if not bootstrap.get("use_builtin_recipes", False): return {"status": "ready", "plans": [planned.as_dict()], **base}
        plans.append(planned)
    elif command is None:
        planned = _task_plan(root, "bootstrap")
        if planned: return {"status": "ready", "plans": [planned.as_dict()], **base}
    if bootstrap.get("use_builtin_recipes") is False: return {"status": "not-needed", "plans": [p.as_dict() for p in plans], **base}
    boundaries = [root]
    try:
        config_boundaries = bootstrap.get("boundaries", {})
        if not isinstance(config_boundaries, dict): raise ValueError("boundaries must be mapping")
        ignored = {_boundary(root, value) for value in config_boundaries.get("ignore", [])}
        for value in config_boundaries.get("include", []):
            candidate = _boundary(root, value)
            if candidate not in ignored: boundaries.append(candidate)
    except (TypeError, ValueError) as error: return {"status": "needs-decision", "plans": [], "decisions": [{"code": "invalid-boundary", "message": str(error)}], **{k:v for k,v in base.items() if k != "decisions"}}
    decisions: list[dict[str, str]] = []
    for item in boundaries:
        found, requested = _builtin(item); plans.extend(found); decisions.extend(requested)
    if decisions: return {"status": "needs-decision", "plans": [], "decisions": decisions, **{k:v for k,v in base.items() if k != "decisions"}}
    return {"status": "ready" if plans else "not-needed", "plans": [p.as_dict() for p in plans], **base}
