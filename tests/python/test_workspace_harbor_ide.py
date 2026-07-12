"""Tests for strict IntelliJ application and process discovery."""

import importlib.util
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "bin/workspace_harbor_ide.py"
SPEC = importlib.util.spec_from_file_location("workspace_harbor_ide", MODULE_PATH)
ide = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ide)


class IntelliJIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def make_app(self, version="2026.1.4"):
        app = self.root / "IntelliJ IDEA.app"
        contents = app / "Contents"
        executable = contents / "MacOS/idea"
        executable.parent.mkdir(parents=True)
        executable.write_text("")
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": version}, handle)
        return app

    def test_intellij_version_derives_exact_config_directory(self):
        app = self.make_app()
        with patch.object(ide, "account_home", return_value=self.home):
            self.assertEqual(
                self.home
                / "Library/Application Support/JetBrains/IntelliJIdea2026.1",
                ide.config_dir(app),
            )

    def test_configured_app_defaults_to_account_home_toolbox_install(self):
        with patch.object(ide, "account_home", return_value=self.home), patch.dict(
            os.environ, {}, clear=True
        ):
            self.assertEqual(
                self.home / "Applications/IntelliJ IDEA.app",
                ide.configured_app(),
            )

    def test_invalid_version_is_rejected(self):
        app = self.make_app("rolling")
        with self.assertRaisesRegex(ValueError, "invalid IntelliJ version"):
            ide.config_dir(app)

    def test_pycharm_command_is_rejected(self):
        app = self.make_app()
        self.assertTrue(
            ide.is_intellij_command(
                str(app / "Contents/MacOS/idea") + " --line 1", app
            )
        )
        self.assertFalse(
            ide.is_intellij_command(
                "/Applications/PyCharm.app/Contents/MacOS/pycharm", app
            )
        )

    def test_owned_port_requires_one_listener_from_configured_app(self):
        app = self.make_app()
        responses = [
            subprocess.CompletedProcess([], 0, "p123\n", ""),
            subprocess.CompletedProcess(
                [], 0, str(app / "Contents/MacOS/idea") + "\n", ""
            ),
        ]
        with patch.object(ide.subprocess, "run", side_effect=responses):
            self.assertTrue(ide.intellij_owned_port(24227, app))

    def test_owned_port_fails_closed_for_ambiguous_or_foreign_process(self):
        app = self.make_app()
        cases = [
            [subprocess.CompletedProcess([], 0, "p1\np2\n", "")],
            [subprocess.CompletedProcess([], 0, "", "")],
            [
                subprocess.CompletedProcess([], 0, "p123\n", ""),
                subprocess.CompletedProcess(
                    [], 0, "/Applications/PyCharm.app/Contents/MacOS/pycharm\n", ""
                ),
            ],
        ]
        for responses in cases:
            with self.subTest(responses=responses), patch.object(
                ide.subprocess, "run", side_effect=responses
            ):
                self.assertFalse(ide.intellij_owned_port(24227, app))

        with patch.object(
            ide.subprocess, "run", side_effect=subprocess.TimeoutExpired("lsof", 2)
        ):
            self.assertFalse(ide.intellij_owned_port(24227, app))


if __name__ == "__main__":
    unittest.main()
