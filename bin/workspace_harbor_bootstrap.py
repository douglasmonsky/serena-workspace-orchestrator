#!/usr/bin/env python3
"""Pure, fail-closed bootstrap evidence and deterministic plan selection."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

import yaml

RECIPE_VERSION = 1
CODEX_TASK = Path(os.environ.get("CODEX_TASK", Path.home() / ".codex/bin/codex-task"))
PRUNED_DIRECTORIES = {".git", ".idea", ".serena", ".venv", "venv", "node_modules", "target", "build", "dist", "vendor", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


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
    global_config = Path.home() / ".codex/serena-integration.yml"
    for path in (global_config, root / ".serena/codex-integration.yml"):
        data = _mapping(path)
        policy.update(data)
    bootstrap = policy.get("bootstrap", {})
    if not isinstance(bootstrap, dict): raise ValueError("bootstrap must be a mapping")
    if "task" in bootstrap and "command" in bootstrap: raise ValueError("bootstrap task and command are mutually exclusive")
    if "enabled" in bootstrap and type(bootstrap["enabled"]) is not bool: raise ValueError("bootstrap enabled must be boolean")
    return policy


def repository_identity(root: Path) -> str:
    root = resolve_root(root)
    try:
        output = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-common-dir"], capture_output=True, text=True, timeout=5, check=False)
        common = (root / output.stdout.strip()).resolve() if output.returncode == 0 else root
    except (OSError, subprocess.SubprocessError): common = root
    return hashlib.sha256(str(common).encode()).hexdigest()


def _contains_source(root: Path, suffixes: set[str]) -> bool:
    for path, directories, files in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if d not in PRUNED_DIRECTORIES and not (Path(path) / d).is_symlink()]
        if any(Path(name).suffix.lower() in suffixes for name in files): return True
    return False


def language_evidence(root: Path, language: str) -> str:
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
    relative = "." if cwd == cwd.anchor and False else str(cwd)
    digest = hashlib.sha256((source + ecosystem + str(cwd) + "\0".join(argv)).encode()).hexdigest()[:16]
    return BootstrapPlan(digest, source, ecosystem, relative, tuple(argv), tuple(str(p) for p in inputs), tuple(markers or ()))


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
        lock = js[0]; package = (boundary / "package.json").read_text(encoding="utf-8", errors="ignore")
        if lock == "package-lock.json": argv, eco = ["npm", "ci"], "npm"
        elif lock == "pnpm-lock.yaml": argv, eco = ["pnpm", "install", "--frozen-lockfile"], "pnpm"
        elif lock.startswith("bun"): argv, eco = ["bun", "install", "--frozen-lockfile"], "bun"
        elif "packageManager" in package and "yarn@" in package or (boundary / ".yarnrc.yml").is_file(): argv, eco = ["yarn", "install", "--immutable"], "yarn-berry"
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
    try: data = json.loads(result.stdout)
    except json.JSONDecodeError: return None
    names = data if isinstance(data, list) else data.get("tasks", []) if isinstance(data, dict) else []
    if name not in [x if isinstance(x, str) else x.get("name") for x in names if isinstance(x, (str, dict))]: return None
    taskfile = root / ".codex/tasks.toml"
    return _plan("task", "task", root, [str(CODEX_TASK), name, "--json"], [taskfile])


def plan_repository(root: Path) -> dict[str, object]:
    root = resolve_root(root)
    try: policy = load_policy(root)
    except ValueError as error: return {"status": "needs-decision", "root": str(root), "plans": [], "decisions": [{"code": "invalid-policy", "message": str(error)}], "policy_source": str(root / ".serena/codex-integration.yml"), "recipe_version": RECIPE_VERSION}
    bootstrap = policy.get("bootstrap", {}); assert isinstance(bootstrap, dict)
    base = {"root": str(root), "decisions": [], "policy_source": str(root / ".serena/codex-integration.yml"), "recipe_version": RECIPE_VERSION}
    if bootstrap.get("enabled") is False: return {"status": "disabled", "plans": [], **base}
    task = bootstrap.get("task", "bootstrap")
    if isinstance(task, str):
        planned = _task_plan(root, task)
        if planned: return {"status": "ready", "plans": [planned.as_dict()], **base}
    command = bootstrap.get("command")
    if command is not None:
        if not isinstance(command, dict) or not isinstance(command.get("argv"), list) or not all(isinstance(v, str) and v for v in command["argv"]): return {"status": "needs-decision", "plans": [], "decisions": [{"code": "invalid-command"}], **{k:v for k,v in base.items() if k != "decisions"}}
        cwd = root / command.get("cwd", ".")
        if not cwd.is_dir() or cwd.resolve() != root and root not in cwd.resolve().parents: return {"status": "needs-decision", "plans": [], "decisions": [{"code": "invalid-command"}], **{k:v for k,v in base.items() if k != "decisions"}}
        inputs = [root / p for p in command.get("inputs", []) if isinstance(p, str)]
        return {"status": "ready", "plans": [_plan("command", "custom", cwd.resolve(), command["argv"], inputs, command.get("markers", [])).as_dict()], **base}
    boundaries = [root]
    try:
        config_boundaries = bootstrap.get("boundaries", {})
        if not isinstance(config_boundaries, dict): raise ValueError("boundaries must be mapping")
        for value in config_boundaries.get("include", []): boundaries.append(_boundary(root, value))
    except (TypeError, ValueError) as error: return {"status": "needs-decision", "plans": [], "decisions": [{"code": "invalid-boundary", "message": str(error)}], **{k:v for k,v in base.items() if k != "decisions"}}
    plans: list[BootstrapPlan] = []; decisions: list[dict[str, str]] = []
    for item in boundaries:
        found, requested = _builtin(item); plans.extend(found); decisions.extend(requested)
    if decisions: return {"status": "needs-decision", "plans": [], "decisions": decisions, **{k:v for k,v in base.items() if k != "decisions"}}
    return {"status": "ready" if plans else "not-needed", "plans": [p.as_dict() for p in plans], **base}
