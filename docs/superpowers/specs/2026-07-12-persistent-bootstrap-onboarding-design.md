# Persistent Bootstrap and Serena Onboarding Design

## Goal

Make a new Workspace Harbor worktree ready for Serena and IntelliJ without
reinstalling dependencies every time an agent reconnects. Preparation must be
deterministic, bounded, observable, and conservative: Harbor runs one selected
setup plan for each confirmed repository ecosystem, never every installer it
can find.

The same preparation phase resolves recurring Serena onboarding findings. It
adds confirmed supported languages by default, asks once when source evidence
is ambiguous, and remembers whether Serena configuration is shared through Git
or intentionally local.

## Product policy

Workspace Harbor uses three preparation sources, in descending priority:

1. A conventional `bootstrap` task in `.codex/tasks.toml`, or another named
   task selected by project integration config.
2. An explicit argv-form command in `.serena/codex-integration.yml` that has
   been approved on the local machine.
3. A versioned, documented Workspace Harbor recipe backed by an unambiguous
   manifest and lockfile.

Human prose in README or AGENTS files is evidence for an agent to review, not
machine-executable configuration. An agent may translate prose into an
explicit task or integration setting after normal repository review. Harbor
does not scrape prose and execute it.

Merely finding multiple manifests does not authorize multiple installers.
Harbor selects only confirmed root or declared-workspace dependency
boundaries. Conflicting package managers, fixture/example manifests, missing
lockfiles, and unsupported layouts produce a decision request or report; they
do not trigger speculative commands.

## Command surface

A new `workspace-harbor-bootstrap` helper owns dependency planning, consent,
fingerprints, execution, and cached status:

- `workspace-harbor-bootstrap status ROOT [--json]` is read-only.
- `workspace-harbor-bootstrap run ROOT [--json] [--force]` executes only an
  approved or deterministic plan and otherwise returns a structured
  `needs-decision` result.
- `workspace-harbor-bootstrap decide ROOT language LANGUAGE
  enable|ignore` records an unconfirmed-language decision.
- `workspace-harbor-bootstrap decide ROOT tracking shared|local` records the
  Serena-file policy.
- `workspace-harbor-bootstrap decide ROOT command approve|reject` records a
  decision for the exact current custom-command digest.

`serena-project-doctor ROOT` remains read-only and includes bootstrap and
onboarding status in its report. `serena-project-doctor --bootstrap ROOT`
performs additive language repair and invokes the bootstrap runner. The doctor
never stages or commits repository files.

`open-codex-project-in-intellij ROOT` invokes the idempotent bootstrap runner
before its IntelliJ-readiness shortcut. A new project is therefore prepared
before IntelliJ indexing, while an already-open unchanged project produces a
fast cache hit and runs no installer. An already-open project whose inputs
changed receives one new preparation run.

The Serena broker reads bootstrap status but never runs a long dependency
command in an MCP connection path. Bootstrap failure must not turn semantic
navigation into a new transport stall. The doctor and opener report degraded
dependency readiness while Serena continues to be available for diagnosis.

## Ecosystem detection and plan selection

Detection uses source evidence, manifests, lockfiles, and declared workspace
relationships. It prunes `.git`, `.idea`, `.serena`, virtual environments,
dependency directories, build output, caches, and vendor trees. Symlinks are
not followed.

The initial built-in recipe registry is:

| Ecosystem | Required evidence | Recipe |
| --- | --- | --- |
| npm | root/workspace `package.json` and `package-lock.json` | `npm ci` |
| pnpm | root/workspace `package.json` and `pnpm-lock.yaml` | `pnpm install --frozen-lockfile` |
| Yarn Berry | `package.json`, `yarn.lock`, and Berry evidence | `yarn install --immutable` |
| Yarn Classic | `package.json` and `yarn.lock` without Berry evidence | `yarn install --frozen-lockfile` |
| Bun | root/workspace `package.json` and `bun.lock` or `bun.lockb` | `bun install --frozen-lockfile` |
| uv | `pyproject.toml` and `uv.lock` | `uv sync --frozen` |
| Poetry | `pyproject.toml` with Poetry metadata and `poetry.lock` | `poetry install --sync --no-interaction` |
| Rust | Rust source, `Cargo.toml`, and `Cargo.lock` | `cargo fetch --locked` |
| Go | Go source, `go.mod`, and `go.sum` | `go mod download` |

Unpinned requirements files, Pipenv, Ruby, PHP, .NET, Swift, and other
ecosystems are detected and reported initially but require an explicit task or
command until a deterministic recipe is implemented and tested. Gradle and
Maven are treated as IntelliJ-native model imports by default; Harbor does not
run a second CLI dependency resolution unless the repository explicitly
configures one.

Berry evidence is a compatible `packageManager` field or `.yarnrc.yml`. If two
JavaScript lockfile families claim the same boundary, no built-in plan is
selected. Independent confirmed root ecosystems, such as a Python service and
a declared Node dashboard workspace, may each contribute one plan. A named
task or custom command is a whole-repository setup plan and suppresses all
built-in recipes unless its configuration explicitly delegates boundaries
back to the built-in registry.

Built-in commands run with a sanitized inherited environment and the
repository root or declared workspace as the working directory. They must not
change their input manifests or lockfiles. Harbor hashes those inputs before
and after execution; a change fails the run and is reported without reverting
user files.

## Structured project configuration

`.serena/codex-integration.yml` may configure:

- whether bootstrap is enabled;
- a named Codex task or argv-form custom command, but not both;
- explicit fingerprint inputs and environment markers for a custom command;
- included or ignored dependency boundaries;
- per-language shared enable/ignore policy; and
- whether Serena files are intended to be shared or local.

Shell strings are not accepted. Custom commands are argv arrays and may not
contain likely secrets or inline credentials. A tracked project file describes
the desired command but does not authorize it to execute. The local approval
store records a digest of the exact command, working directory, and declared
inputs. Any change invalidates approval and produces one new decision request.

Global `$CODEX_HOME/serena-integration.yml` supplies defaults and may disable
bootstrap or additive language repair. The repository config may opt out.
Local decisions never weaken an explicit repository opt-out.

## Language confirmation and consent

Language support and dependency installation are separate decisions.

- Source plus a matching, unambiguous dependency boundary confirms a language.
  Harbor adds a missing supported language to `.serena/project.yml` when
  additive repair is enabled.
- Source without a matching manifest/lock boundary is unconfirmed. Harbor
  asks the agent to obtain one decision: enable Serena language support only,
  configure a bootstrap task/command, or ignore the language.
- A confirmed language without a deterministic lockfile may still be enabled
  for Serena, but dependency setup remains `needs-decision`.

For example, `.rs` files plus `Cargo.toml` and `Cargo.lock` confirm Rust and
select `cargo fetch --locked`. Rust source without a Cargo boundary asks once;
it does not silently run Cargo or repeatedly warn every agent.

Language decisions are stored per Git repository identity and ecosystem. A
repository identity is derived from the canonical absolute Git common
directory, so sibling worktrees share the decision while unrelated clones do
not. Decisions are re-requested only when the relevant evidence class,
configured command, or policy changes.

## Shared versus local Serena files

When `.serena/project.yml` or curated memories are untracked and no policy is
known, the doctor returns one `needs-decision` onboarding item. The choices
are:

- `shared`: the repository is expected to track `project.yml`,
  `.serena/.gitignore`, `codex-integration.yml`, and curated memories; or
- `local`: those files intentionally remain machine-local.

The decision is remembered locally immediately. Selecting `shared` may update
the integration setting, but Harbor never runs `git add`, creates a commit, or
rewrites ignore rules automatically. Selecting `local` downgrades future
tracking findings to informational policy status instead of repeating
warnings.

Shared configuration is portable policy, not execution consent. Custom
commands still require local approval on each machine.

## Persistent state, fingerprints, and concurrency

Private state lives under
`$CODEX_HOME/state/workspace-harbor/bootstrap/`. Repository approvals and
worktree execution records are separate:

- repository approvals are keyed by Git common-directory identity;
- successful bootstrap records are keyed by canonical worktree root; and
- a per-worktree advisory lock serializes planning, execution, and the final
  atomic record update.

A bootstrap fingerprint includes:

- the canonical worktree root;
- recipe/schema version;
- selected task or command identity;
- hashes of manifests, lockfiles, integration config, and declared inputs;
- package-manager executable path and version; and
- relevant runtime identity.

A successful record is written atomically only after every selected plan
succeeds, its inputs remain unchanged, and declared environment markers pass.
Missing, malformed, failed, or mismatched state is never a cache hit. State
directories use mode `0700`; locks and records use `0600`.

An unchanged agent restart or sibling agent using the same worktree acquires
the lock, observes the matching success record, and exits without invoking a
package manager. A changed lockfile, changed command/configuration, missing
required environment marker, recipe-version change, prior failure, or
`--force` causes one new run.

## Execution and reporting

Execution is non-interactive and bounded. Output is captured rather than
streamed into ordinary agent context. Harbor returns a compact status packet
containing the selected source, cache result, elapsed time, exit status, and a
sanitized failure tail. It never logs environment variables, credentials, or
private dependency URLs. Persistent raw installer output is not retained.

Possible overall states are:

- `ready`: every required plan is a cache hit or completed successfully;
- `pending`: every required plan is deterministic/approved, but no matching
  success record exists yet;
- `not-needed`: no supported dependency boundary requires setup;
- `needs-decision`: ambiguity, unconfirmed language, local approval, or Serena
  tracking policy requires user input;
- `failed`: a selected setup command failed or changed protected inputs; and
- `disabled`: global or repository policy opted out.

CLI exit status is `0` for `ready`, `pending`, `not-needed`, or `disabled`;
`3` for `needs-decision`; `1` for a failed selected plan; and `2` for invalid
input, configuration, or state. JSON mode always emits one result object
before returning the corresponding status. `run` never returns `pending`: it
executes the selected plan and returns `ready` or `failed`.

Bootstrap failure does not close IntelliJ, launch a second IDE, or repeatedly
retry through Serena. The opener continues to make the exact trusted project
available, reports degraded setup once, and lets the agent use Serena/native
diagnostics to resolve the failure. A later explicit run may retry.

## Deployment and guidance

Deployment installs the new helper alongside the existing Workspace Harbor
commands, then updates the opener, doctor, and broker as one compatible unit.
The deployment script backs up the prior commands and verifies help/status
output before replacing live files.

Global AGENTS guidance tells agents to use the opener/doctor preparation path,
ask only for structured `needs-decision` results, and avoid hand-running every
detected installer. Existing tasks need only re-read the updated guidance; an
unchanged prepared worktree requires no reinstall or repeated activation.

## Verification

Automated tests must prove:

- priority of named tasks, approved custom commands, and built-in recipes;
- ambiguity rejection and exclusion of nested examples, caches, and vendors;
- each built-in recipe's evidence and argv;
- no shell-string execution or approval through tracked configuration alone;
- first run, unchanged cache hit, lockfile/config/runtime invalidation,
  `--force`, corrupt state, and missing environment markers;
- concurrent callers execute one installer;
- failure and protected-input mutation never write success state;
- confirmed language auto-repair and one-time unconfirmed-language decisions;
- shared/local Serena tracking policy without automatic Git staging;
- doctor read-only behavior and explicit `--bootstrap` behavior;
- opener preparation ordering and degraded-but-available failure behavior;
- broker status integration without long bootstrap execution; and
- private permissions and sanitized bounded reporting.

Live dogfood covers a Python-plus-Node repository, an unchanged second agent
run, a synthetic ambiguous-language repository, and the active
`r11-compression-detectors` worktree. The latter should retain healthy IntelliJ
semantics, list Python and TypeScript, and stop repeating `.serena` tracking
warnings after a local/shared policy decision.

## Out of scope

- Parsing arbitrary prose into executable setup commands.
- Running every package manager found below the repository root.
- Automatically editing, staging, or committing dependency lockfiles.
- Automatically staging or committing Serena configuration or memories.
- Installing global runtimes, package managers, marketplace plugins, or paid
  tools.
- Treating dependency bootstrap success as a replacement for repository tests,
  lint, type checking, or IntelliJ inspections.
