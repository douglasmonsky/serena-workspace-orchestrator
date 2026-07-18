"""Tests for the Developer-only Codex project workspace policy."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "bin/codex_developer_workspace_guard.py"
SPEC = importlib.util.spec_from_file_location("codex_developer_workspace_guard", MODULE_PATH)
assert SPEC and SPEC.loader
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)


class WorkspaceGuardPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path("/Users/Monsky")

    def classify(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        cwd: str = "/Users/Monsky",
    ) -> GUARD.Decision:
        return GUARD.classify_event(
            {
                "session_id": "test",
                "tool_name": tool_name,
                "tool_input": tool_input,
                "cwd": cwd,
            },
            home=self.home,
        )

    def assert_blocked(self, decision: GUARD.Decision) -> None:
        self.assertFalse(decision.allowed)
        self.assertEqual("documents-project-write-blocked", decision.reason)

    def test_denies_git_init_below_documents(self) -> None:
        self.assert_blocked(
            self.classify(
                "Bash",
                {"cmd": "git init", "workdir": "/Users/Monsky/Documents/new-app"},
            )
        )

    def test_denies_git_clone_targeting_documents(self) -> None:
        self.assert_blocked(
            self.classify(
                "Bash",
                {"cmd": "git clone https://example.invalid/repo.git '/Users/Monsky/Documents/new-app'"},
            )
        )

    def test_denies_git_worktree_targeting_documents(self) -> None:
        self.assert_blocked(
            self.classify(
                "Bash",
                {"cmd": "git worktree add '/Users/Monsky/Documents/new-app' feature"},
            )
        )

    def test_denies_scaffolding_and_dependency_installs_in_documents(self) -> None:
        cases = (
            "npm create vite@latest app",
            "npm install",
            "uv init app",
            "cargo new app",
            "python -m pip install -r requirements.txt",
        )
        for command in cases:
            with self.subTest(command=command):
                self.assert_blocked(
                    self.classify(
                        "Bash",
                        {"cmd": command, "workdir": "/Users/Monsky/Documents/project"},
                    )
                )

    def test_denies_mutating_git_command_in_documents_checkout(self) -> None:
        for command in ("git add -- main.py", "git commit -m 'change'", "git switch -c feature"):
            with self.subTest(command=command):
                self.assert_blocked(
                    self.classify(
                        "Bash",
                        {"cmd": command, "workdir": "/Users/Monsky/Documents/project"},
                    )
                )

    def test_denies_source_patch_below_documents(self) -> None:
        patch = (
            "*** Begin Patch\n"
            "*** Add File: /Users/Monsky/Documents/new-app/main.py\n"
            "+print('x')\n"
            "*** End Patch"
        )
        self.assert_blocked(self.classify("apply_patch", {"patch": patch}))

    def test_denies_source_redirection_below_documents(self) -> None:
        self.assert_blocked(
            self.classify(
                "Bash",
                {"cmd": "printf 'x' > '/Users/Monsky/Documents/new-app/main.py'"},
            )
        )

    def test_denies_relative_traversal_into_documents(self) -> None:
        self.assert_blocked(
            self.classify(
                "Bash",
                {
                    "cmd": "git clone https://example.invalid/repo.git ../../../Documents/new-app",
                    "workdir": "/Users/Monsky/Developer/Codex/project",
                },
            )
        )

    def test_allows_read_only_inspection_below_documents(self) -> None:
        cases = (
            "git status --short --branch",
            "git log -1 --oneline",
            "find . -type f -maxdepth 2",
            "lsof +D .",
            "rg -n pattern .",
        )
        for command in cases:
            with self.subTest(command=command):
                decision = self.classify(
                    "Bash",
                    {"cmd": command, "workdir": "/Users/Monsky/Documents/old-app"},
                )
                self.assertTrue(decision.allowed)

    def test_allows_migration_from_documents_into_developer(self) -> None:
        for command in (
            "mv '/Users/Monsky/Documents/old-app' '/Users/Monsky/Developer/Codex/old-app'",
            "cp -R '/Users/Monsky/Documents/old-app' '/Users/Monsky/Developer/Codex/old-app'",
        ):
            with self.subTest(command=command):
                self.assertTrue(self.classify("Bash", {"cmd": command}).allowed)

    def test_allows_project_mutation_below_approved_roots(self) -> None:
        for workdir in (
            "/Users/Monsky/Developer/Codex/new-app",
            "/Users/Monsky/.codex/src/tooling",
        ):
            with self.subTest(workdir=workdir):
                self.assertTrue(
                    self.classify("Bash", {"cmd": "git init", "workdir": workdir}).allowed
                )

    def test_allows_ordinary_office_document_tools(self) -> None:
        decision = self.classify(
            "documents:documents",
            {"path": "/Users/Monsky/Documents/report.docx", "operation": "write"},
        )
        self.assertTrue(decision.allowed)

    def test_allows_documents_lookalikes(self) -> None:
        for path in (
            "/Users/Monsky/Documents-old/new-app",
            "/Users/Monsky/Developer/CodexBackup/new-app",
        ):
            with self.subTest(path=path):
                self.assertTrue(
                    self.classify("Bash", {"cmd": "git init", "workdir": path}).allowed
                )

    def test_allows_plain_directory_creation_without_project_evidence(self) -> None:
        decision = self.classify(
            "Bash",
            {"cmd": "mkdir '/Users/Monsky/Documents/Meeting Notes'"},
        )
        self.assertTrue(decision.allowed)


if __name__ == "__main__":
    unittest.main()
