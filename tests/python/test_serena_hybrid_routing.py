from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BIN_DIR = REPO_ROOT / "bin"
sys.path.insert(0, str(BIN_DIR))

import serena_hybrid_routing as routing


class SerenaHybridRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "main.cpp").write_text("int main() { return 0; }\n")
        (self.root / "src" / "main.ts").write_text("export const answer = 42;\n")

    def test_native_classifier_requires_safe_existing_file(self) -> None:
        self.assertTrue(routing.is_native_relative_file(self.root, "src/main.cpp"))
        self.assertFalse(routing.is_native_relative_file(self.root, "/tmp/main.cpp"))
        self.assertFalse(routing.is_native_relative_file(self.root, "../main.cpp"))
        self.assertFalse(routing.is_native_relative_file(self.root, "src"))
        self.assertFalse(routing.is_native_relative_file(self.root, "src/missing.cpp"))
        self.assertFalse(routing.is_native_relative_file(self.root, "src/main.ts"))

    def test_supported_native_suffixes(self) -> None:
        suffixes = (
            ".c",
            ".h",
            ".i",
            ".ii",
            ".cc",
            ".cpp",
            ".cxx",
            ".c++",
            ".cppm",
            ".ccm",
            ".cxxm",
            ".c++m",
            ".hpp",
            ".hh",
            ".hxx",
            ".h++",
            ".inl",
            ".ipp",
            ".ixx",
            ".tpp",
            ".txx",
            ".ino",
        )
        for index, suffix in enumerate(suffixes):
            relative_path = f"src/native_{index}{suffix}"
            (self.root / relative_path).write_text("// native\n")
            with self.subTest(suffix=suffix):
                self.assertTrue(routing.is_native_relative_file(self.root, relative_path))

    def test_non_native_file_stays_on_primary_backend(self) -> None:
        call = routing.route_tool_call(
            self.root,
            "jet_brains_get_symbols_overview",
            {"relative_path": "src/main.ts", "depth": 1},
        )

        self.assertEqual("primary", call.backend)
        self.assertEqual("jet_brains_get_symbols_overview", call.tool_name)
        self.assertEqual({"relative_path": "src/main.ts", "depth": 1}, call.arguments)

    def test_native_symbol_tools_map_to_lsp_equivalents(self) -> None:
        cases = (
            (
                "jet_brains_get_symbols_overview",
                {"relative_path": "src/main.cpp", "depth": 1, "max_answer_chars": 4096, "include_file_documentation": False},
                "get_symbols_overview",
                {"relative_path": "src/main.cpp", "depth": 1, "max_answer_chars": 4096},
            ),
            (
                "jet_brains_find_symbol",
                {"name_path_pattern": "main", "relative_path": "src/main.cpp", "depth": 1, "include_body": True, "include_info": True, "search_deps": False, "max_matches": 2, "max_answer_chars": 4096},
                "find_symbol",
                {"name_path_pattern": "main", "relative_path": "src/main.cpp", "depth": 1, "include_body": True, "include_info": True, "max_matches": 2, "max_answer_chars": 4096},
            ),
            (
                "jet_brains_find_referencing_symbols",
                {"name_path": "main", "relative_path": "src/main.cpp", "max_answer_chars": 4096},
                "find_referencing_symbols",
                {"name_path": "main", "relative_path": "src/main.cpp", "max_answer_chars": 4096},
            ),
            (
                "jet_brains_find_declaration",
                {"relative_path": "src/main.cpp", "regex": "main", "include_body": True},
                "find_declaration",
                {"relative_path": "src/main.cpp", "regex": "main", "include_body": True},
            ),
            (
                "jet_brains_find_implementations",
                {"relative_path": "src/main.cpp", "name_path": "main"},
                "find_implementations",
                {"relative_path": "src/main.cpp", "name_path": "main"},
            ),
        )

        for source_name, source_args, target_name, target_args in cases:
            with self.subTest(tool=source_name):
                call = routing.route_tool_call(self.root, source_name, source_args)
                self.assertEqual("secondary", call.backend)
                self.assertEqual(target_name, call.tool_name)
                self.assertEqual(target_args, call.arguments)

    def test_native_inspections_map_lines_and_severity_to_lsp_diagnostics(self) -> None:
        call = routing.route_tool_call(
            self.root,
            "jet_brains_run_inspections",
            {"relative_path": "src/main.cpp", "min_severity": "WARNING", "inspection_names": [], "start_line": 2, "end_line": 8, "max_answer_chars": 4096},
        )

        self.assertEqual("secondary", call.backend)
        self.assertEqual("get_diagnostics_for_file", call.tool_name)
        self.assertEqual(
            {"relative_path": "src/main.cpp", "min_severity": 2, "start_line": 1, "end_line": 7, "max_answer_chars": 4096},
            call.arguments,
        )

    def test_native_inspections_use_full_file_defaults(self) -> None:
        call = routing.route_tool_call(
            self.root,
            "jet_brains_run_inspections",
            {"relative_path": "src/main.cpp"},
        )

        self.assertEqual(
            {"relative_path": "src/main.cpp", "min_severity": 4, "start_line": 0, "end_line": -1},
            call.arguments,
        )

    def test_unsupported_native_options_fail_explicitly(self) -> None:
        cases = (
            ("jet_brains_get_symbols_overview", {"relative_path": "src/main.cpp", "include_file_documentation": True}),
            ("jet_brains_find_symbol", {"relative_path": "src/main.cpp", "search_deps": True}),
            ("jet_brains_run_inspections", {"relative_path": "src/main.cpp", "inspection_names": ["ClangTidy"]}),
            ("jet_brains_run_inspections", {"relative_path": "src/main.cpp", "start_line": 0}),
        )

        for tool_name, arguments in cases:
            with self.subTest(tool=tool_name, arguments=arguments):
                with self.assertRaises(routing.RoutingError) as raised:
                    routing.route_tool_call(self.root, tool_name, arguments)
                self.assertEqual("unsupported-native-option", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
