# Workspace Harbor

An IntelliJ lifecycle and worktree orchestrator for Serena-powered coding
agents.

Workspace Harbor keeps IntelliJ IDEA project windows, Serena services, and
Codex tasks from colliding. It opens and trusts exact Git worktree roots,
reuses one IntelliJ application process, assigns logical ownership per task,
and closes only broker-owned idle projects that pass every safety check.

IntelliJ IDEA Ultimate is the only supported IDE. PyCharm remains installed
but is not opened, trusted, inventoried, or closed by Workspace Harbor.

## Local requirements

- IntelliJ IDEA at `$HOME/Applications/IntelliJ IDEA.app`
- Workspace Harbor and Serena IntelliJ plugins
- Official JetBrains language plugins required by the repository
- Git and Python 3.9 or newer

The build and verification commands use IntelliJ's bundled JBR; a separate
system JDK is not required:

```sh
export INTELLIJ_APP_PATH="$HOME/Applications/IntelliJ IDEA.app"
export JAVA_HOME="$INTELLIJ_APP_PATH/Contents/jbr/Contents/Home"
./gradlew test buildPlugin verifyPlugin --console=plain
python3 -m unittest discover -s tests/python -p 'test_*.py' -v
```

## Commands

- `open-codex-project-in-intellij ROOT` validates, trusts, opens, and registers
  one exact Git root.
- `intellij-project-trust allow|status ROOT` and `audit` manage only the active
  IntelliJ trusted-path registry.
- `intellij-project-reaper status --json` reports lifecycle state;
  `cleanup` closes only fully eligible managed projects, and `unregister ROOT`
  removes the registry record after a worktree has been safely closed and removed.
- `serena-codex jetbrains-service-status ROOT` requires one IntelliJ-owned
  Serena service and rejects foreign or duplicate matches.
- `serena-project-doctor ROOT` performs one bounded health and language-coverage
  check; `--bootstrap` performs explicit dependency preparation after reporting
  any one-time decisions.
- `workspace-harbor-bootstrap status|run ROOT` plans and caches deterministic
  repository setup; `decide` records local language, tracking, or custom-command
  consent.
- `serena-worktree-broker status|cleanup` reports ownership and reclaims only
  broker-owned idle services.

## Trust model

Managed roots must be exact canonical Git top levels beneath either
`$HOME/Documents/Codex` or `$HOME/.codex/src`. The trust helper rejects nested
paths, non-Git directories, symlink escapes, malformed state, and roots outside
those parents. It writes IntelliJ's native registry with locking, backup, and
atomic replacement. When IntelliJ is running, it also calls Workspace Harbor's
authenticated loopback endpoint so the in-memory trust state changes before
the project is opened; this prevents a stale live registry from producing a
GUI trust prompt.

`intellij-project-trust audit` reports broad and out-of-scope entries but never
removes them automatically.

## Ownership model

Ownership is one logical Codex task per canonical worktree. Parent agents and
their subagents share a workspace owner by exporting the same
`WORKSPACE_HARBOR_OWNER_ID`. Different tasks may use different worktrees or
repositories concurrently in separate IntelliJ project windows. A second
logical owner is rejected for an already-owned worktree, preventing two tasks
from racing the same project model or Serena service.

The broker exposes owners in `serena-worktree-broker status`. IntelliJ itself
remains one application process; project-window and worktree ownership is the
unit Workspace Harbor manages.

## Persistent dependency bootstrap

The IntelliJ opener runs one idempotent preparation check before its
already-open shortcut. An unchanged worktree is a fast cache hit: no package
manager runs when an agent stops, restarts, or reconnects. Setup runs again
only when a selected command, manifest, lockfile, runtime/tool identity,
required environment marker, or Harbor recipe version changes, or when
`--force` is requested.

Plan precedence is conservative:

1. A conventional `bootstrap` task in `.codex/tasks.toml`, or a task selected
   by `.serena/codex-integration.yml`.
2. An argv-form custom command that has matching local approval.
3. A Workspace Harbor recipe backed by an unambiguous manifest and lockfile.

Harbor never scrapes prose into commands and never runs every installer found
under a repository. Conflicting managers, source without a matching dependency
boundary, and custom-command changes return `needs-decision`. Local decisions
are shared by sibling Git worktrees; successful installation records remain
per worktree.

Built-in recipes currently cover npm, pnpm, Yarn Classic/Berry, Bun, uv,
Poetry, Cargo fetch, and Go module download. Gradle and Maven remain
IntelliJ-native model imports unless the repository configures a setup task.
Other ecosystems are reported and require an explicit task or command.

Examples:

```sh
workspace-harbor-bootstrap status "$(git rev-parse --show-toplevel)" --json
serena-project-doctor --bootstrap "$(git rev-parse --show-toplevel)"
workspace-harbor-bootstrap decide "$(git rev-parse --show-toplevel)" \
  language rust enable
workspace-harbor-bootstrap decide "$(git rev-parse --show-toplevel)" \
  tracking local
```

Tracked integration configuration describes desired policy but does not grant
permission to execute arbitrary custom commands. Exact custom-command approval
is stored privately under `$CODEX_HOME/state/workspace-harbor/bootstrap` and is
invalidated when its argv, working directory, declared inputs, or markers
change. Installer output is bounded and redacted; raw output is not retained by
the bootstrap helper.

## Safety and recovery

Closing is fail-closed. Unsaved documents, indexing, active run/debug/terminal
sessions, modal dialogs, unknown plugin state, a broker lease, or ambiguous
ownership all protect a project. Do not kill IntelliJ or Serena processes
broadly. Use:

```sh
serena-project-doctor "$(git rev-parse --show-toplevel)"
workspace-harbor-bootstrap status "$(git rev-parse --show-toplevel)" --json
serena-worktree-broker status
serena-worktree-broker cleanup
```

Deployment backups live under `~/.codex/backups/workspace-harbor/`.
