# Hybrid Clangd Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transparently route file-scoped C/C++ Serena semantic reads from the JetBrains tool surface to a persistent clangd-backed Serena LSP service.

**Architecture:** Add a pure routing module and a thin MCP SDK gateway, then extend the worktree broker to lease a secondary LSP service only for detected C/C++ projects. Preserve the existing single-service path for other repositories and use the normal JetBrains proxy whenever hybrid routing is explicitly disabled.

**Tech Stack:** Python 3.9+ standard library, Serena's installed Python/MCP SDK runtime, existing Serena JetBrains and LSP backends, clangd, `unittest`.

## Global Constraints

- Do not interact with installed plugins or live IDE processes while developing or testing repository code.
- Route only semantic reads; do not route rename, move, inline, safe delete, type hierarchy, or debugger calls.
- Activate only for project-doctor `cpp` coverage and an executable clangd, unless the operator kill switch disables the feature.
- Never journal source, symbols, raw tool arguments, MCP content, compiler diagnostics, or absolute project paths.
- A failed secondary backend must not take Java, JavaScript, TypeScript, or Python semantics offline.
- Use tests before each production behavior change and watch each focused test fail for the expected reason.

---

### Task 1: Pure native routing contract

**Files:**
- Create: `bin/serena_hybrid_routing.py`
- Create: `tests/python/test_serena_hybrid_routing.py`

**Interfaces:**
- Produces: `RoutingError`, `RoutedToolCall`, `is_native_relative_file(project_root, relative_path)`, and `route_tool_call(project_root, tool_name, arguments)`.
- `RoutedToolCall.backend` is `"primary"` or `"secondary"`; `tool_name` and `arguments` are ready for the selected upstream.

- [ ] **Step 1: Write failing classification tests**

Test real temporary files and assert that `.c`, `.h`, `.cpp`, and `.hpp` files route as native while absolute paths, `..` traversal, directories, missing files, TypeScript files, and `<ext...>` identifiers do not.

```python
def test_native_classifier_requires_safe_existing_file(self) -> None:
    native = self.root / "src/example.cpp"
    native.parent.mkdir()
    native.write_text("int main() {}\n", encoding="utf-8")
    self.assertTrue(routing.is_native_relative_file(self.root, "src/example.cpp"))
    for unsafe in ("/tmp/example.cpp", "../example.cpp", "src", "src/missing.cpp", "<ext>/x.cpp"):
        self.assertFalse(routing.is_native_relative_file(self.root, unsafe))
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python3 -m unittest tests.python.test_serena_hybrid_routing -v`

Expected: import failure for missing `serena_hybrid_routing.py`.

- [ ] **Step 3: Implement the classifier and immutable route result**

Use `Path.resolve(strict=True)` and `relative_to(project_root.resolve(strict=True))`; reject directories and non-native suffixes. Define the complete suffix allowlist from the design.

```python
@dataclass(frozen=True)
class RoutedToolCall:
    backend: Literal["primary", "secondary"]
    tool_name: str
    arguments: dict[str, Any]
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `python3 -m unittest tests.python.test_serena_hybrid_routing -v`

Expected: classification tests pass.

- [ ] **Step 5: Write failing tests for all six mappings**

Cover the exact name mappings, default argument filtering, pathless/directory calls remaining primary, `search_deps=True` rejection for native `find_symbol`, and `inspection_names` rejection for native diagnostics.

```python
route = routing.route_tool_call(
    self.root,
    "jet_brains_get_symbols_overview",
    {"relative_path": "src/example.cpp", "depth": 1, "max_answer_chars": 9000},
)
self.assertEqual("secondary", route.backend)
self.assertEqual("get_symbols_overview", route.tool_name)
```

- [ ] **Step 6: Run mapping tests and verify RED**

Run: `python3 -m unittest tests.python.test_serena_hybrid_routing.HybridRoutingTests -v`

Expected: missing `route_tool_call` behavior or primary routing where secondary is required.

- [ ] **Step 7: Implement minimal mapping and diagnostics conversion**

Map one-based `start_line`/`end_line` to zero-based values and convert `ERROR`, `WARNING`, `WEAK_WARNING`, and `INFO` to LSP severities `1`, `2`, `3`, and `3`. Raise `RoutingError("unsupported-native-argument", ...)` for non-default arguments that cannot be represented.

- [ ] **Step 8: Verify task tests and commit**

Run: `python3 -m unittest tests.python.test_serena_hybrid_routing -v`

Expected: all routing tests pass.

Commit: `feat: add native semantic routing contract`

---

### Task 2: MCP hybrid gateway

**Files:**
- Create: `bin/serena-hybrid-mcp`
- Create: `tests/python/test_serena_hybrid_mcp.py`
- Modify: `bin/deploy-workspace-harbor`
- Modify: `tests/python/test_deploy_workspace_harbor.py`

**Interfaces:**
- Consumes: `route_tool_call` from Task 1.
- Produces: executable `serena-hybrid-mcp --project-root ROOT --primary-url URL [--secondary-url URL] [--secondary-error CODE]`.
- `HybridGateway.list_tools()` returns the primary tool catalog with routing notes appended to the six routed descriptions.
- `HybridGateway.call_tool(name, arguments)` calls exactly one upstream session and preserves its result object.

- [ ] **Step 1: Write failing gateway behavior tests with complete fake sessions**

Use fake sessions that implement the real `list_tools()` and `call_tool(name, arguments)` shape. Assert the returned result object identity, not mock invocation alone. Cover TypeScript-to-primary, C-to-secondary, secondary-unavailable errors, and catalog preservation.

```python
result = asyncio.run(gateway.call_tool(
    "jet_brains_get_symbols_overview", {"relative_path": "src/example.c"}
))
self.assertIs(secondary.result, result)
self.assertEqual([], primary.calls)
```

- [ ] **Step 2: Run the gateway test and verify RED**

Run: `python3 -m unittest tests.python.test_serena_hybrid_mcp -v`

Expected: import failure for missing `serena-hybrid-mcp`.

- [ ] **Step 3: Implement the dependency-free gateway core**

Keep MCP SDK imports inside the runtime entrypoint so system-Python unit tests can load `HybridGateway`. The core accepts session-like objects and delegates routing without parsing or logging content.

- [ ] **Step 4: Run gateway unit tests and verify GREEN**

Run: `python3 -m unittest tests.python.test_serena_hybrid_mcp -v`

Expected: all in-memory routing tests pass.

- [ ] **Step 5: Write a failing subprocess contract test for runtime startup**

Invoke the gateway with Serena's Python and `--help`, then assert the executable can import the installed MCP SDK without modifying `PYTHONPATH`.

- [ ] **Step 6: Implement the MCP SDK stdio/HTTP adapter**

Use `mcp.client.streamable_http.streamablehttp_client`, `ClientSession`, `mcp.server.lowlevel.Server`, and `mcp.server.stdio.stdio_server`. Initialize the primary and optional secondary sessions once, register `list_tools` and `call_tool` handlers, and close both through `AsyncExitStack`.

- [ ] **Step 7: Add deployment coverage and verify RED**

Add `serena-hybrid-mcp` to executable deployment and `serena_hybrid_routing.py` to module deployment. Add `serena-hybrid-mcp` to `SERENA_RUNTIME_COMMANDS` so its installed shebang uses the validated Serena Python.

Run: `python3 -m unittest tests.python.test_deploy_workspace_harbor -v`

Expected before deploy changes: the new command/module are absent.

- [ ] **Step 8: Implement deployment changes and verify task tests**

Run:

```sh
python3 -m unittest tests.python.test_serena_hybrid_mcp -v
python3 -m unittest tests.python.test_deploy_workspace_harbor -v
```

Expected: all tests pass.

Commit: `feat: add hybrid Serena MCP gateway`

---

### Task 3: Broker dual-backend lifecycle

**Files:**
- Modify: `bin/serena-worktree-broker`
- Modify: `bin/workspace_harbor_bridge.py`
- Modify: `tests/python/test_serena_worktree_broker.py`
- Modify: `tests/python/test_workspace_harbor_bridge.py`

**Interfaces:**
- Consumes: deployed gateway path and project-doctor language JSON.
- Produces: `_hybrid_cpp_activation(project_root, doctor_payload)`, dual backend leases for eligible projects, optional-secondary gateway invocation, and privacy-safe hybrid journal stages.

- [ ] **Step 1: Write failing activation tests**

Cover configured/detected `cpp`, executable lookup, the four false-like kill-switch values, non-native projects, and malformed doctor payloads. Tests patch only executable discovery and environment boundaries.

- [ ] **Step 2: Run activation tests and verify RED**

Run: `python3 -m unittest tests.python.test_serena_worktree_broker.SerenaWorktreeBrokerTests -v`

Expected: missing activation helper.

- [ ] **Step 3: Implement activation and return doctor JSON**

Change `_auto_repair_project_languages` to return a parsed dictionary when valid and `{}` when successful output is not a JSON object. Hybrid activation requires `cpp` in configured or detected languages, `shutil.which("clangd")`, and an enabled kill switch.

- [ ] **Step 4: Write failing dual-service lifecycle tests**

Use real in-memory broker state and minimal process-boundary patches. Assert backend-sensitive service keys, both leases while the gateway runs, both leases removed afterward, LSP reuse, and primary-only operation when disabled.

- [ ] **Step 5: Run lifecycle tests and verify RED**

Run the named broker tests and confirm they fail because only one service/lease exists.

- [ ] **Step 6: Implement dual service and shared lease cleanup**

Refactor lease insertion/removal into helpers used by both backends. Start JetBrains authoritatively; start LSP best-effort. When the project is eligible, always invoke the gateway: pass `--secondary-url` after successful LSP startup or `--secondary-error clangd-unavailable` when it fails. If the gateway file itself is missing, fall back to the primary proxy and journal the stable reason `hybrid-gateway-missing`.

- [ ] **Step 7: Add privacy-safe journal stages**

Allow only `secondary-service-started`, `secondary-service-reused`, `secondary-service-unavailable`, and `hybrid-proxy-started`. Tests must reject free-form reasons and confirm no tool arguments or source enter journal records.

- [ ] **Step 8: Run broker and bridge suites and commit**

Run:

```sh
python3 -m unittest tests.python.test_serena_worktree_broker -v
python3 -m unittest tests.python.test_workspace_harbor_bridge -v
```

Expected: all tests pass.

Commit: `feat: route native Serena reads through clangd`

---

### Task 4: Documentation, deployment, and acceptance

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-07-19-hybrid-clangd-routing.md` only to mark completed checkboxes if the repository convention requires it.

**Interfaces:**
- Documents: activation, transparent tool list, file-scoped limitation, compile database expectations, kill switch, degraded behavior, and operator verification.

- [ ] **Step 1: Write documentation assertions where existing deploy/status tests support them**

Extend status assertions to show both backend records without adding source or paths beyond existing status behavior.

- [ ] **Step 2: Update README**

Add a `Hybrid C/C++ semantics` section stating that clangd-backed routing applies only to recognized file paths, global searches and semantic edits stay JetBrains-backed, `compile_commands.json` remains repository-owned, and `SERENA_HYBRID_CPP_ENABLED=0` disables the feature.

- [ ] **Step 3: Run the full Python suite**

Run: `python3 -m unittest discover -s tests/python -p 'test_*.py' -v`

Expected: zero failures and zero errors.

- [ ] **Step 4: Run static and repository checks**

Run:

```sh
python3 -m py_compile bin/serena-hybrid-mcp bin/serena_hybrid_routing.py bin/serena-worktree-broker
git diff --check
```

Expected: exit code 0.

- [ ] **Step 5: Deploy atomically**

Run: `python3 bin/deploy-workspace-harbor --dry-run`, inspect the exact command/module set, then run `python3 bin/deploy-workspace-harbor`.

Expected: atomic deployment succeeds and reports a backup when replacing existing commands.

- [ ] **Step 6: Run Gate C hybrid acceptance**

Connect a bounded MCP client through the installed broker for `/Users/Monsky/Developer/Codex/codex-ui-lab-gate-c`. Call `jet_brains_get_symbols_overview` for `spikes/gate-c/native/gatec_native.c` and assert at least one returned symbol. Call the same exposed tool for `spikes/gate-c/src/session-owner.ts` and assert the JetBrains result remains nonempty. Do not mutate Gate C or its IDE state.

- [ ] **Step 7: Final review and commit**

Review status, diff stat, full diff, staged files, and targeted secret/private-data patterns. Commit only orchestrator documentation and implementation files.

Commit: `docs: explain hybrid C++ semantics`
