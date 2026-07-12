"""Tests for atomic Workspace Harbor command deployment."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "bin/deploy-workspace-harbor"
EXECUTABLES = (
    "deploy-workspace-harbor",
    "open-codex-project-in-intellij",
    "intellij-project-trust",
    "intellij-project-reaper",
    "serena-codex",
    "serena-project-doctor",
    "serena-worktree-broker",
    "workspace-harbor-bootstrap",
)
MODULES = ("workspace_harbor_ide.py", "workspace_harbor_bootstrap.py")


class DeployWorkspaceHarborTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.source = self.base / "source"
        self.bin_source = self.source / "bin"
        self.bin_source.mkdir(parents=True)
        for name in (*EXECUTABLES, *MODULES):
            (self.bin_source / name).write_text(f"#!/usr/bin/env python3\n# {name}\n", encoding="utf-8")
        self.codex_home = self.base / "codex"
        self.serena_python = self.base / "serena-python"
        self.serena_python.write_text("fixture\n", encoding="utf-8")
        self.serena_python.chmod(0o755)
        self.environment = os.environ | {
            "CODEX_HOME": str(self.codex_home),
            "WORKSPACE_HARBOR_SOURCE_ROOT": str(self.source),
            "WORKSPACE_HARBOR_SERENA_PYTHON": str(self.serena_python),
        }

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_deploy(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(DEPLOY), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env=self.environment,
        )

    def test_dry_run_lists_complete_set_without_writing(self) -> None:
        result = self.run_deploy("--dry-run")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse((self.codex_home / "bin").exists())
        for name in (*EXECUTABLES, *MODULES):
            self.assertIn(name, result.stdout)

    def test_real_deploy_backs_up_and_installs_modes_and_interpreters(self) -> None:
        destination = self.codex_home / "bin"
        destination.mkdir(parents=True)
        existing = destination / "serena-project-doctor"
        existing.write_text("old doctor\n", encoding="utf-8")
        existing.chmod(0o755)

        result = self.run_deploy()

        self.assertEqual(0, result.returncode, result.stderr)
        backups = list((self.codex_home / "backups/workspace-harbor").glob("*/bin/serena-project-doctor"))
        self.assertEqual(1, len(backups))
        self.assertEqual("old doctor\n", backups[0].read_text(encoding="utf-8"))
        self.assertEqual(0o755, backups[0].stat().st_mode & 0o777)
        for name in EXECUTABLES:
            self.assertEqual(0o755, (destination / name).stat().st_mode & 0o777)
        for name in MODULES:
            self.assertEqual(0o644, (destination / name).stat().st_mode & 0o777)
        expected_shebang = "#!/usr/bin/env python3"
        self.assertEqual(expected_shebang, (destination / "serena-project-doctor").read_text().splitlines()[0])
        self.assertEqual(expected_shebang, (destination / "workspace-harbor-bootstrap").read_text().splitlines()[0])
        self.assertEqual(
            f"#!{self.serena_python.resolve()}",
            (destination / "serena-codex").read_text().splitlines()[0],
        )

    def test_missing_source_fails_before_destination_mutation(self) -> None:
        destination = self.codex_home / "bin"
        destination.mkdir(parents=True)
        existing = destination / "serena-codex"
        existing.write_text("keep me\n", encoding="utf-8")
        (self.bin_source / "workspace_harbor_bootstrap.py").unlink()

        result = self.run_deploy()

        self.assertEqual(2, result.returncode)
        self.assertEqual("keep me\n", existing.read_text(encoding="utf-8"))
        self.assertFalse((self.codex_home / "backups").exists())


if __name__ == "__main__":
    unittest.main()
