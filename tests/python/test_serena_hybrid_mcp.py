"""Tests for the transparent hybrid Serena MCP gateway."""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest


BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
GATEWAY_PATH = BIN_DIR / "serena-hybrid-mcp"
SERENA_PYTHON = Path.home() / ".local/share/uv/tools/serena-agent/bin/python"
sys.path.insert(0, str(BIN_DIR))
LOADER = importlib.machinery.SourceFileLoader("serena_hybrid_mcp", str(GATEWAY_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
gateway_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway_module
LOADER.exec_module(gateway_module)


class FakeSession:
    def __init__(self, tools: list[SimpleNamespace], result: object) -> None:
        self.tools = tools
        self.result = result
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=self.tools, nextCursor="cursor")

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        self.calls.append((name, arguments))
        return self.result


class SerenaHybridMcpTests(unittest.TestCase):
    @unittest.skipUnless(SERENA_PYTHON.is_file(), "Serena Python is not installed")
    def test_runtime_help_loads_with_serena_python(self) -> None:
        import_result = subprocess.run(
            [
                str(SERENA_PYTHON),
                "-c",
                (
                    "import pathlib,runpy,sys; "
                    "sys.path.insert(0, str(pathlib.Path(sys.argv[1]).parent)); "
                    "module=runpy.run_path(sys.argv[1], run_name='serena_hybrid_runtime_test'); "
                    "runtime=module['_load_mcp_runtime'](); "
                    "print(runtime.Server.__module__)"
                ),
                str(GATEWAY_PATH),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        result = subprocess.run(
            [str(SERENA_PYTHON), str(GATEWAY_PATH), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, import_result.returncode, import_result.stderr)
        self.assertEqual("mcp.server.lowlevel.server", import_result.stdout.strip())
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--primary-url", result.stdout)
        self.assertIn("--secondary-url", result.stdout)

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "src").mkdir()
        (self.root / "src/example.c").write_text("int answer(void) { return 42; }\n")
        (self.root / "src/example.ts").write_text("export const answer = 42;\n")
        self.primary_result = object()
        self.secondary_result = object()
        self.primary_tool = SimpleNamespace(
            name="jet_brains_get_symbols_overview",
            description="Return the symbol tree.",
            inputSchema={"type": "object"},
        )
        self.other_tool = SimpleNamespace(
            name="jet_brains_debugger_status",
            description="Return debugger status.",
            inputSchema={"type": "object"},
        )
        self.primary = FakeSession(
            [self.primary_tool, self.other_tool], self.primary_result
        )
        self.secondary = FakeSession([], self.secondary_result)

    def gateway(self, *, secondary: FakeSession | None = None, error: str | None = None):
        return gateway_module.HybridGateway(
            project_root=self.root,
            primary=self.primary,
            secondary=secondary,
            secondary_error=error,
        )

    def test_typescript_call_uses_primary_and_preserves_result(self) -> None:
        result = asyncio.run(
            self.gateway(secondary=self.secondary).call_tool(
                "jet_brains_get_symbols_overview",
                {"relative_path": "src/example.ts"},
            )
        )

        self.assertIs(self.primary_result, result)
        self.assertEqual(
            [("jet_brains_get_symbols_overview", {"relative_path": "src/example.ts"})],
            self.primary.calls,
        )
        self.assertEqual([], self.secondary.calls)

    def test_native_call_uses_secondary_and_preserves_result(self) -> None:
        result = asyncio.run(
            self.gateway(secondary=self.secondary).call_tool(
                "jet_brains_get_symbols_overview",
                {"relative_path": "src/example.c"},
            )
        )

        self.assertIs(self.secondary_result, result)
        self.assertEqual([], self.primary.calls)
        self.assertEqual(
            [("get_symbols_overview", {"relative_path": "src/example.c"})],
            self.secondary.calls,
        )

    def test_native_call_fails_explicitly_when_secondary_is_unavailable(self) -> None:
        with self.assertRaises(gateway_module.HybridGatewayError) as raised:
            asyncio.run(
                self.gateway(error="clangd-unavailable").call_tool(
                    "jet_brains_get_symbols_overview",
                    {"relative_path": "src/example.c"},
                )
            )

        self.assertEqual("clangd-unavailable", raised.exception.code)
        self.assertEqual([], self.primary.calls)

    def test_catalog_preserves_primary_shape_and_only_annotates_routed_tools(self) -> None:
        catalog = asyncio.run(self.gateway(secondary=self.secondary).list_tools())

        self.assertEqual("cursor", catalog.nextCursor)
        self.assertEqual([self.primary_tool.name, self.other_tool.name], [tool.name for tool in catalog.tools])
        self.assertEqual(self.primary_tool.inputSchema, catalog.tools[0].inputSchema)
        self.assertIn("C/C++", catalog.tools[0].description)
        self.assertIn("clangd", catalog.tools[0].description)
        self.assertEqual(self.other_tool.description, catalog.tools[1].description)
        self.assertEqual("Return the symbol tree.", self.primary_tool.description)


if __name__ == "__main__":
    unittest.main()
