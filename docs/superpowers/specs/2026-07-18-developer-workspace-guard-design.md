# Developer Workspace Guard Design

**Date:** 2026-07-18
**Status:** Approved design; pending written-spec review

## Purpose

Prevent Codex coding projects from being created or modified below
`/Users/Monsky/Documents`. New repositories, worktrees, dependency trees, generated
code, and source edits belong below `/Users/Monsky/Developer/Codex`. Ordinary
document workflows and deliberate migrations out of Documents must remain usable.

The existing `desktop.git-worktree-root` setting controls only Codex-managed Git
worktrees. It does not change saved local-project paths, a task's inherited working
directory, or arbitrary shell and patch destinations. Global `AGENTS.md` guidance
documents the policy but cannot enforce it. The guard therefore belongs at the
global `PreToolUse` boundary.

## Chosen Approach

Add a small dependency-free Python command named
`codex-developer-workspace-guard` to the Workspace Harbor tooling repository and
deploy it into `~/.codex/bin`. Register it as a global `PreToolUse` hook alongside
the existing Serena reminder hook.

The command reads one Codex hook event as JSON from standard input and emits the
supported `hookSpecificOutput` response. It allows operations by default and emits
a stable deny decision only when the event provides enough evidence that a coding
project would be created or mutated under Documents. Malformed or unknown events
must fail closed only for clearly mutating invocations; they must not turn ordinary
Codex use into a global outage.

## Policy

### Protected and approved roots

- Protected legacy root: `/Users/Monsky/Documents`
- Approved coding roots:
  - `/Users/Monsky/Developer/Codex`
  - `/Users/Monsky/.codex/src`

Path comparisons use resolved, component-aware paths. Prefix lookalikes such as
`Documents-old` and `Developer/CodexBackup` do not match either policy root.

### Always allowed

- Read-only commands and tools.
- Operations whose working directory and every write destination are outside
  Documents.
- Writes below the approved coding roots.
- Deliberate copy or move operations whose source is below Documents and whose
  destination is below `/Users/Monsky/Developer/Codex`.
- Inspection needed to establish migration safety, including Git status/log/show,
  `find`, `rg`, `lsof`, hashes, size checks, and comparisons.
- Ordinary document, spreadsheet, presentation, image, and PDF tools. The guard is
  scoped to coding/project mutations rather than all personal-file writes.

### Denied

- `git init`, `git clone`, or worktree creation targeting Documents.
- Project scaffolding or package-manager initialization/install commands targeting
  Documents or running from a Documents coding checkout.
- Directory creation under Documents when the command also establishes coding
  markers or uses a recognized project scaffolder.
- Source-code patch, edit, or write tools targeting a path below Documents.
- Mutating Git and repository task-runner commands executed from a Documents Git
  checkout.
- Shell commands with redirection or other explicit output paths below Documents
  when the output is source code, project configuration, dependency state, or Git
  metadata.

The denial explains that the task must create or migrate a standalone checkout
below `/Users/Monsky/Developer/Codex` before editing. It never silently rewrites a
destination.

## Event Classification

The classifier is pure and separately testable:

1. Normalize the event's tool name, working directory, and structured tool input.
2. Extract explicit filesystem paths without expanding shell substitutions,
   environment variables, or globs.
3. Classify the tool as read-only, project-mutating, source-writing, migration, or
   unknown.
4. Deny only a project-mutating or source-writing operation associated with the
   protected root.
5. Return stable reason token `documents-project-write-blocked` plus a concise
   remediation message.

The first release supports Codex shell/unified-exec inputs and patch/edit/write
inputs observed in the installed hook protocol. Unknown tool schemas are allowed
unless their tool name and explicit destination independently prove a protected
project write.

## Configuration and Deployment

- The deployment manifest installs the executable atomically with the other
  Workspace Harbor helpers.
- `~/.codex/hooks.json` gains one global `PreToolUse` command hook. The existing
  Serena hook remains unchanged.
- Hook trust state is refreshed through the supported configuration mechanism; the
  implementation does not edit Codex's private saved-project database.
- Stale `[projects]` trust records below Documents are removed only after parsing
  and rewriting `config.toml` safely with a backup.
- Existing saved-project entries in the Codex UI remain visible until they can be
  removed or recreated through a supported UI/API. The guard prevents those entries
  from causing new coding writes in the meantime.

## Testing

Unit tests feed real-shaped hook JSON to the command and assert both exit behavior
and emitted decisions.

Required cases:

- Deny Git initialization, cloning, scaffolding, source patching, dependency
  installation, and redirected source output below Documents.
- Deny repository mutation when the task CWD is a Documents Git checkout.
- Allow reads from Documents.
- Allow ordinary office-document tools.
- Allow a verified migration from Documents to Developer/Codex.
- Allow mutations below Developer/Codex and `.codex/src`.
- Treat relative paths against the event CWD and reject traversal into Documents.
- Avoid false matches for similarly named directories.
- Return bounded, content-free errors for malformed JSON.

The focused tests must be observed failing before implementation. After they pass,
run the complete Python suite, the IntelliJ plugin build, deployment dry-run, live
hook fixture checks, TOML/JSON validation, and a final source-versus-deployed hash
comparison.

## Rollout and Recovery

Deployment is backed up and atomic. Before enabling the hook, invoke it directly
with allow and deny fixtures. After enabling it, start a fresh Codex task because
running tasks may retain previously loaded hook configuration. If the hook causes
an unexpected denial, remove only its new hook entry and restore the deployed
helper/config backups; do not disable the existing Serena hooks.

