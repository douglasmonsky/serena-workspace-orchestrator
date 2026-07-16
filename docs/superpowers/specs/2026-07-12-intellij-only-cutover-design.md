# IntelliJ-Only Workspace Harbor Cutover Design

## Goal

Make IntelliJ IDEA Ultimate the sole IDE managed by Workspace Harbor and the
sole JetBrains backend expected by the local Serena integration. Remove
PyCharm compatibility code from the project and deployed command chain while
leaving the PyCharm application and its user settings intact.

## Product policy

- IntelliJ IDEA is the only managed IDE.
- PyCharm is not opened, trusted, inventoried, closed, or otherwise managed by
  Workspace Harbor after migration.
- The supported IntelliJ application is the active Toolbox installation at
  `$HOME/Applications/IntelliJ IDEA.app`, with an environment override for
  isolated tests.
- Java, Kotlin, JavaScript, TypeScript, SQL, and bundled web support come from
  IntelliJ Ultimate. Official JetBrains Python, Go, and Rust plugins are part
  of the local baseline.
- Additional language plugins are installed only when a repository actually
  requires them. Language configuration repair remains additive and opt-out.
- PyCharm remains installed. Disabling or uninstalling its Serena and
  Workspace Harbor plugins is a separate live migration step with an
  action-time confirmation.

## Command surface

The source-controlled and deployed commands are:

- `open-codex-project-in-intellij`
- `intellij-project-trust`
- `intellij-project-reaper`
- `serena-codex`
- `serena-project-doctor`
- `serena-worktree-broker`

The PyCharm-named opener, trust helper, reaper, and their tests are removed.
There are no compatibility shims. Git history is the rollback source.

## IntelliJ application identity

One small Python module, `bin/workspace_harbor_ide.py`, owns IntelliJ product
discovery and identity. It exposes pure functions for:

- resolving the configured application bundle;
- reading and validating `CFBundleShortVersionString`;
- deriving `IntelliJIdea<major>.<minor>` configuration paths;
- recognizing the IntelliJ executable and bundle identifier; and
- mapping a Serena listening port to the owning process on macOS.

All helpers consume this identity rather than reimplementing app/version
logic. Production defaults fail closed when the configured app, version,
registry, executable, or process ownership cannot be established. Tests use
explicit environment paths and synthetic plists.

## Exact-root trust

`intellij-project-trust` preserves the existing exact-Git-root allowlist,
locking, backup, merge, and atomic-write behavior. Its only live registry is:

`$HOME/Library/Application Support/JetBrains/IntelliJIdea<major>.<minor>/options/trusted-paths.xml`

It never reads or writes a `PyCharm*` registry. Live-registry recognition
requires the exact authenticated account home, the `IntelliJIdea` prefix, and
the expected relative path. Existing broad entries are reported but never
removed automatically.

When IntelliJ is already running, a registry write alone does not update its
in-memory trust state. The helper therefore calls Workspace Harbor's
authenticated loopback `POST /v1/projects/trust` endpoint after exact-root
validation and the atomic registry update. The plugin applies IntelliJ's
native `TrustedProjects` API before the opener proceeds. Missing runtime state
is acceptable before the first IntelliJ launch; malformed or rejected live
state fails closed.

## Managed opener

> Superseded concurrency model: steps 3–7 below describe the original
> long-held global opener lock. The approved replacement is
> [Queued IntelliJ Opener Design](2026-07-16-queued-intellij-opener-design.md),
> which uses canonical-worktree single flight plus a short durable FIFO launch
> queue and waits for different roots concurrently.

`open-codex-project-in-intellij` serializes opens in a new
`~/.codex/state/intellij-projects` namespace. For an exact canonical Git root
it performs this sequence:

1. Apply optional GitHub-remote gating.
2. Check for an IntelliJ-owned Serena service for the exact root.
3. Acquire the opener lock and repeat the check.
4. Add only the exact root to IntelliJ's trust registry.
5. Reuse the existing IntelliJ application process to open the root in its own
   project window.
6. Wait for an exact-root Serena service whose listening port belongs to the
   configured IntelliJ executable.
7. Register the project with the IntelliJ reaper only after readiness.

A matching Serena service owned by another process or IDE is ambiguous, not
ready. The opener reports it once and exits without launching another IDE.
It never launches PyCharm and never closes a project during the open path.

## Serena ownership and health

`serena-codex jetbrains-service-status` gains IntelliJ ownership validation.
It scans matching Serena services, identifies the listening process for each
port, and succeeds only when exactly one matching service belongs to the
configured IntelliJ application and no matching foreign service exists.

The doctor uses that result before its semantic probe. Recovery text names the
IntelliJ opener. Its probe preference is based on languages supported by the
installed IntelliJ language plugins, not the former PyCharm-specific ordering.
One timeout, plugin error, or stale result still triggers a single bounded
doctor run and native diagnostics fallback.

The broker invokes the IntelliJ opener and uses the ownership-aware health
check. It keeps one persistent service per canonical worktree, backend, and
context. It never starts a direct persistent Serena process outside broker
ownership.

The canonical worktree is also the ownership boundary. A parent agent and its
subagents share one explicit `WORKSPACE_HARBOR_OWNER_ID`; another logical task
cannot acquire that same root. Independent tasks may own different worktrees
or repositories concurrently. All project windows may live in the same
IntelliJ application process because ownership attaches to roots and leases,
not to the macOS application process.

## Reaper and plugin

The Java package moves from `com.monsky.codex.pycharm.lifecycle` to
`com.monsky.workspaceharbor.lifecycle`. The plugin ID and display name remain
`com.monsky.workspaceharbor` and `Workspace Harbor`.

The Gradle build compiles and verifies against the configured IntelliJ IDEA
application and JBR. Plugin compatibility remains build `261.*` until a
separate compatibility change is tested.

`intellij-project-reaper` uses new files under
`~/.codex/state/intellij-projects`. It does not import PyCharm registry or
runtime records. A project may close only when all existing fail-closed checks
pass: exact plugin inventory, no unsaved documents, no indexing, run,
terminal, debugger, modal, or closing activity, no broker lease, sufficient
idle time, and capacity pressure. Unknown or malformed state protects the
project.

## Native project-model import

Workspace Harbor does not synthesize IDE project files. IntelliJ opens the
exact repository root and its native importers handle Gradle, Maven, Node,
Python, Go, Rust, and other recognized models. The doctor distinguishes
`indexReady` from a usable project model: semantic navigation may be healthy
while inspections are not authoritative until the repository's native model
is linked. The doctor reports this condition once rather than retrying.
Gradle verification and local automation use IntelliJ's bundled JBR, avoiding
a separate machine-wide JDK dependency.

## Deployment and migration

Source changes are verified before live deployment. Deployment then occurs as
one reviewed unit:

1. Back up the current deployed helpers.
2. Install the IntelliJ opener, trust helper, reaper, launcher, doctor, and
   broker together.
3. Build and install Workspace Harbor into IntelliJ IDEA.
4. Verify the exact repository through the IntelliJ-owned Serena service.
5. Update global agent guidance to use IntelliJ commands.
6. With action-time confirmation, disable or uninstall Serena and Workspace
   Harbor from PyCharm and close only the duplicate Workspace Harbor PyCharm
   project if the IDE reports no unsaved state.
7. Remove obsolete deployed PyCharm helper commands and scheduled references.

Any failure before step 5 restores the backed-up command set. A failure during
live plugin migration leaves PyCharm installed and reports the remaining
manual action; it never kills IDE processes broadly.

## Verification

Automated tests must prove:

- IntelliJ app/version/config discovery and rejection of PyCharm registries;
- exact-root trust preservation, locking, and atomic writes;
- opener ordering, serialization, readiness ownership, and foreign-service
  rejection;
- reaper isolation from PyCharm state and all existing safety classifications;
- doctor and broker use of the IntelliJ opener;
- Java package/plugin metadata and IntelliJ-targeted Gradle verification; and
- no source or test command retains a functional PyCharm dependency.

Live dogfood gates verify one IntelliJ process, one IntelliJ-owned Serena
service for the exact root, Java and Python overview/find-symbol operations,
IDE inspections after native model import, and unchanged Git status. Go and
Rust semantic probes run only in real repositories containing those languages,
not in excluded temporary directories.

## Out of scope

- Uninstalling PyCharm itself or deleting its settings.
- Supporting Rider or CLion, which the Serena plugin currently lists as
  unsupported.
- Automatic installation of every marketplace language plugin.
- Generalizing Workspace Harbor to multiple IDE products in this cutover.
