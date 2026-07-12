# Managed PyCharm Project Trust Design

Date: 2026-07-12
Status: approved design

## Goal

Prevent PyCharm trust prompts for repositories opened through the managed Codex
workflow while preserving PyCharm's protection against arbitrary projects.
Trust is granted only to the exact canonical Git repository or worktree root
requested by the managed opener.

## Safety boundary

An automatically trusted root must satisfy every condition below:

- It is an existing directory and a Git repository or worktree root.
- `git rev-parse --show-toplevel` resolves to the same canonical path requested.
- Its canonical path is beneath either `/Users/Monsky/Documents/Codex` or
  `/Users/Monsky/.codex/src`.
- The path does not escape an allowed parent through a symlink.
- The trust operation targets the exact root, never an ancestor or wildcard.

Paths outside these parents, nested directories that are not the Git top level,
non-Git directories, missing paths, and ambiguous paths are rejected. The
helper never disables PyCharm's trust feature globally.

## Components

### `pycharm-project-trust`

A small command-line helper owns PyCharm trust-registry interaction:

- `allow ROOT` validates the safety boundary and idempotently adds the exact
  root to PyCharm's native trusted-project registry.
- `status ROOT` reports `trusted`, `untrusted`, or `ineligible` without writing.
- `audit` reports exact trusted roots, broad ancestor entries, malformed state,
  and entries outside the approved parents. Audit is read-only.

The helper resolves the active PyCharm configuration directory rather than
hard-coding a product version where practical. An explicit environment override
is available only for isolated tests. If no single usable configuration can be
identified, the helper fails closed.

Trust-registry updates use an exclusive lock, read the latest file while locked,
parse and validate XML, preserve unrelated entries, write a private temporary
file, fsync it, atomically replace the original, and retain one timestamped
pre-change backup. Malformed XML is never overwritten. Generated entries use
PyCharm's `$USER_HOME$` form when the root is under the current home directory.

### Managed opener integration

`open-codex-project-in-pycharm` calls `pycharm-project-trust allow ROOT` after
canonical Git-root validation and before asking macOS to open the project.
Trust failure stops a new project open and prints one concise diagnostic. An
already-open project remains usable and is not retroactively rejected because
the trust registry is unavailable.

The existing opener serialization remains the outer lock. The trust helper also
has its own lock so direct calls and future clients cannot corrupt the registry.
After opening, the existing exact-root Serena readiness and Workspace Harbor
registration gates remain unchanged.

## Existing broad trust migration

The current PyCharm registry contains a broad `$USER_HOME$/Documents` trust
entry. Migration is deliberately separate from normal `allow` behavior:

1. Audit and record the broad entry.
2. Materialize exact entries for currently relevant open/recent repositories
   that fall inside the approved parents.
3. Verify managed opening of a fresh test repository without a prompt.
4. Remove only the exact broad `$USER_HOME$/Documents` entry after a backup.
5. Re-audit and verify unrelated trust entries remain byte-for-byte equivalent
   in meaning.

The helper does not remove broad entries automatically during ordinary opens.
Migration requires an explicit one-shot command or reviewed deployment step.

## Failure behavior

- Missing or malformed PyCharm trust state: fail closed and preserve the file.
- Lock timeout: fail closed without opening a new project.
- Ineligible root: return a distinct nonzero status with the rejected rule.
- Concurrent PyCharm write: lock, reread, and merge the latest state; atomic
  replacement prevents partial XML.
- PyCharm ignores an on-disk update while already running: do not loop, relaunch
  PyCharm, or disable security. Report the bounded failure and use a native
  application/plugin API in a follow-up design if live reload proves necessary.

## Verification

Automated tests use temporary configuration and repository directories and
cover:

- exact eligible Git root acceptance;
- idempotent repeated allowance;
- rejection of non-Git, nested, outside-parent, and symlink-escape paths;
- preservation of unrelated XML entries;
- malformed XML and lock-timeout fail-closed behavior;
- concurrent updates and private atomic output;
- opener ordering: trust before open, readiness before registration;
- trust failure prevents a new open;
- already-open behavior when trust state is unavailable;
- audit detection of the broad Documents entry.

Controlled real scenarios then open one fresh repository under
`/Users/Monsky/Documents/Codex`, confirm no trust prompt, confirm one PyCharm
process, and verify unrelated projects and trust entries are unchanged. The
broad-entry migration runs only after that scenario passes.

## Out of scope

- Trusting arbitrary user-selected directories.
- Disabling PyCharm project security globally.
- Trusting parent directories or glob patterns.
- Closing projects or changing cleanup policy.
- Editing other JetBrains products' trust stores.
- Automatically removing user-created exact trust entries.
