# Subagent Serena Ownership Inheritance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve Codex subagents to their root parent task so they can lease the parent's brokered Serena service on the same canonical worktree.

**Architecture:** Add a bounded, fail-closed session metadata reader inside `serena-worktree-broker`. It derives an `OwnerResolution` from the explicit override, the current root thread, or validated subagent ancestry; the existing worktree/service keys continue enforcing root isolation. A read-only `owner --json` command exposes only the resolved identity contract for diagnostics and dogfooding.

**Tech Stack:** Python 3.9+, local Codex JSONL session metadata, `pathlib`, `json`, `argparse`, `unittest`, existing Workspace Harbor deployment helper.

## Global Constraints

- Never modify Codex session logs or read beyond the first bounded JSONL record.
- Preserve `WORKSPACE_HARBOR_OWNER_ID` as the highest-priority override.
- Require UUID-shaped Codex thread IDs and mutually consistent parent fields.
- Reject missing, malformed, ambiguous, cyclic, self-referential, or over-depth ancestry by retaining the current child ID.
- Keep canonical worktree, backend, and context service isolation unchanged.
- Do not expose prompts, session content, or unrelated thread IDs in diagnostics.
- Preserve Python 3.9 compatibility and existing broker fail-closed behavior.

---

### Task 1: Bounded Session Lineage Resolution

**Files:**
- Modify: `bin/serena-worktree-broker`
- Modify: `tests/python/test_serena_worktree_broker.py`

**Interfaces:**
- Produces immutable `OwnerResolution(thread_id: str | None, owner_id: str, source: str, reason: str | None)`.
- Produces `_owner_resolution() -> OwnerResolution` and retains `_owner_id() -> str` as a compatibility wrapper returning `.owner_id`.
- Consumes `SESSION_DIR`, defaulting to `$CODEX_HOME/sessions` and injectable with `WORKSPACE_HARBOR_SESSION_DIR`.

- [x] **Step 1: Write failing root, nested-child, override, and failure tests**

Add helpers that create session files named like Codex rollouts and write one `session_meta` line:

```python
ROOT_ID = "11111111-1111-4111-8111-111111111111"
PARENT_ID = "22222222-2222-4222-8222-222222222222"
CHILD_ID = "33333333-3333-4333-8333-333333333333"

def write_session(self, thread_id: str, parent_id: str | None = None) -> None:
    day = self.session_dir / "2026/07/14"
    day.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": thread_id,
        "parent_thread_id": parent_id,
        "source": (
            {"subagent": {"thread_spawn": {"parent_thread_id": parent_id, "depth": 1}}}
            if parent_id else {}
        ),
        "thread_source": "subagent" if parent_id else "cli",
    }
    path = day / f"rollout-2026-07-14T00-00-00-{thread_id}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n")
```

In `setUp`, create `self.session_dir` beside the isolated broker state and add
`SESSION_DIR=self.session_dir` to the existing `mock.patch.multiple` call.

Cover exact expectations:

```python
def test_nested_subagent_resolves_to_root_parent(self):
    self.write_session(ROOT_ID)
    self.write_session(PARENT_ID, ROOT_ID)
    self.write_session(CHILD_ID, PARENT_ID)
    with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": CHILD_ID}, clear=True):
        result = broker._owner_resolution()
    self.assertEqual(ROOT_ID, result.owner_id)
    self.assertEqual("subagent-lineage", result.source)

def test_invalid_lineage_falls_back_to_child(self):
    self.write_inconsistent_session(CHILD_ID, PARENT_ID, ROOT_ID)
    with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": CHILD_ID}, clear=True):
        result = broker._owner_resolution()
    self.assertEqual(CHILD_ID, result.owner_id)
    self.assertEqual("inconsistent-parent", result.reason)
```

Also assert a top-level thread returns `root-thread`; an explicit owner returns `explicit`; missing, malformed, oversized (more than 64 KiB before newline), duplicate filenames, self-link, cycle, invalid UUID, and nine-generation ancestry return the current child as owner with a stable concise reason.

- [x] **Step 2: Run the focused tests to verify RED**

Run:

```bash
python3 -m unittest -v \
  tests.python.test_serena_worktree_broker.SerenaWorktreeBrokerTests.test_nested_subagent_resolves_to_root_parent \
  tests.python.test_serena_worktree_broker.SerenaWorktreeBrokerTests.test_invalid_lineage_falls_back_to_child
```

Expected: FAIL because `_owner_resolution` and `SESSION_DIR` do not exist.

- [x] **Step 3: Implement the immutable result and bounded metadata reader**

Add:

```python
from dataclasses import dataclass
import re

SESSION_DIR = Path(os.environ.get("WORKSPACE_HARBOR_SESSION_DIR", CODEX_HOME / "sessions"))
THREAD_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_SESSION_META_BYTES = 65_536
MAX_LINEAGE_DEPTH = 8

@dataclass(frozen=True)
class OwnerResolution:
    thread_id: str | None
    owner_id: str
    source: str
    reason: str | None = None
```

Implement `_session_meta(thread_id) -> tuple[dict[str, Any] | None, str | None]` by globbing only `SESSION_DIR/*/*/*/*-{thread_id}.jsonl`, requiring exactly one regular file, opening it in binary mode, calling `readline(MAX_SESSION_META_BYTES + 1)`, rejecting a line over the limit or without a newline, parsing JSON, and requiring `type == "session_meta"`, a mapping payload, and `payload.id == thread_id`.

Implement `_lineage_owner(thread_id) -> OwnerResolution` with a visited set and at most eight parent transitions. A root record has no parent, must not claim `thread_source == "subagent"`, and returns the last thread with source `root-thread` for a top-level caller or `subagent-lineage` after one or more transitions. A child record must claim `thread_source == "subagent"` and its top-level and nested parent values must be identical valid thread IDs. Any failure returns the original caller ID and the stable failure reason.

- [x] **Step 4: Integrate resolution without changing ownership enforcement**

Replace `_owner_id` with:

```python
def _owner_resolution() -> OwnerResolution:
    explicit = os.environ.get("WORKSPACE_HARBOR_OWNER_ID", "").strip()
    thread = os.environ.get("CODEX_THREAD_ID", "").strip()
    if explicit:
        _validate_owner_value(explicit)
        return OwnerResolution(thread or None, explicit, "explicit")
    if thread:
        if not THREAD_ID_PATTERN.fullmatch(thread):
            _validate_owner_value(thread)
            return OwnerResolution(thread, thread, "root-thread", "invalid-thread-id")
        return _lineage_owner(thread)
    fallback = f"manual-pid-{os.getpid()}"
    return OwnerResolution(None, fallback, "process-fallback")

def _owner_id() -> str:
    return _owner_resolution().owner_id
```

Keep `_connect`, `_assert_root_owner`, service keys, and lease records unchanged except that the lease receives the resolved root owner through `_owner_id()`.

- [x] **Step 5: Run the broker suite and syntax check to verify GREEN**

Run:

```bash
python3 -m unittest -v tests.python.test_serena_worktree_broker
python3 -m py_compile bin/serena-worktree-broker
git diff --check
```

Expected: all broker tests PASS with no syntax or whitespace errors.

- [x] **Step 6: Commit Task 1**

```bash
git add -- bin/serena-worktree-broker tests/python/test_serena_worktree_broker.py
git commit -m "feat: inherit Serena ownership across subagents"
```

---

### Task 2: Owner Diagnostics, Documentation, Deployment, and Dogfood

**Files:**
- Modify: `bin/serena-worktree-broker`
- Modify: `tests/python/test_serena_worktree_broker.py`
- Modify: `README.md`
- Modify: `/Users/Monsky/.codex/AGENTS.md`
- Deploy reviewed copy to: `/Users/Monsky/.codex/bin/serena-worktree-broker`

**Interfaces:**
- Consumes `_owner_resolution() -> OwnerResolution` from Task 1.
- Produces `serena-worktree-broker owner [--json]` with JSON keys `thread_id`, `owner_id`, `source`, and optional `reason`.

- [x] **Step 1: Write failing owner-command contract tests**

Add:

```python
import io

def test_owner_json_reports_resolution_without_session_content(self):
    resolution = broker.OwnerResolution(CHILD_ID, ROOT_ID, "subagent-lineage")
    with mock.patch.object(broker, "_owner_resolution", return_value=resolution), \
         mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
        status = broker.main(["owner", "--json"])
    payload = json.loads(stdout.getvalue())
    self.assertEqual(
        {"owner_id": ROOT_ID, "source": "subagent-lineage", "thread_id": CHILD_ID},
        payload,
    )
    self.assertNotIn("payload", stdout.getvalue())
```

Also cover text output and inclusion of only a concise `reason` when lineage fails closed.

- [x] **Step 2: Run the command tests to verify RED**

Run:

```bash
python3 -m unittest -v \
  tests.python.test_serena_worktree_broker.SerenaWorktreeBrokerTests.test_owner_json_reports_resolution_without_session_content
```

Expected: FAIL because the `owner` subcommand does not exist.

- [x] **Step 3: Implement the read-only owner command**

Add `_owner(args)` that converts `OwnerResolution` into a mapping, omits `reason` when it is `None`, prints sorted JSON for `--json`, and otherwise prints `thread=<value-or-> owner=<owner_id> source=<source>` plus `reason=<reason>` when present. Register:

```python
owner = subparsers.add_parser("owner", help="show resolved logical task owner")
owner.add_argument("--json", action="store_true")
owner.set_defaults(handler=_owner)
```

The command must not acquire broker state, open IntelliJ, connect Serena, or modify a session file.

- [x] **Step 4: Document automatic inheritance and recovery**

Update README ownership documentation and global Serena guidance with these operational rules:

- Parent and child ownership is derived automatically from validated Codex session metadata.
- No capsule, prompt token, or manual environment export is required for ordinary Codex subagents.
- Explicit `WORKSPACE_HARBOR_OWNER_ID` remains supported.
- Different canonical worktrees always use different services.
- `serena-worktree-broker owner --json` diagnoses resolution; a fail-closed reason means the child remains isolated rather than sharing incorrectly.

- [x] **Step 5: Run complete verification**

Run:

```bash
python3 -m unittest discover -s tests/python -p 'test_*.py' -v
python3 -m py_compile bin/serena-worktree-broker
JAVA_HOME="$HOME/Applications/IntelliJ IDEA.app/Contents/jbr/Contents/Home" \
  ./gradlew test buildPlugin verifyPlugin --console=plain
git diff --check
```

Expected: no Python failures, Gradle `BUILD SUCCESSFUL`, and no whitespace errors.

- [x] **Step 6: Commit Task 2 and deploy the reviewed set**

```bash
git add -- bin/serena-worktree-broker tests/python/test_serena_worktree_broker.py README.md
git commit -m "docs: operate inherited Serena ownership"
bin/deploy-workspace-harbor --dry-run
bin/deploy-workspace-harbor
```

Do not stage `/Users/Monsky/.codex/AGENTS.md`. Verify the deployed broker hash matches the deployment dry-run packet.

- [x] **Step 7: Dogfood one real parent and child**

Run deployed `serena-worktree-broker owner --json` in the parent and record its owner ID. Spawn one bounded read-only child with no inherited turns and assign it the same source checkout. The child runs the same owner command, `serena-codex jetbrains-service-status ROOT`, and one doctor semantic probe. Assert:

- child `thread_id` differs from the parent;
- child `owner_id` equals the parent's owner ID;
- source is `subagent-lineage` in the child;
- broker status contains no additional service for the canonical root;
- the IntelliJ-owned Serena service and semantic probe are healthy; and
- no new IntelliJ window is opened.

Then use isolated session fixtures to assert a different top-level thread remains a different owner and `_assert_root_owner` rejects it for the parent's worktree.

- [x] **Step 8: Final review**

Inspect `git status --short --branch`, `git diff --stat`, committed diffs, deployed/source hashes, and staged files for secrets or private session content. Confirm no session JSONL path or contents entered a commit. Report commits, checks, dogfood results, skipped tests, and remaining compatibility risk from Codex session-format changes.
