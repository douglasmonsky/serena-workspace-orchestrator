"""Contract tests for the IntelliJ-targeted Workspace Harbor plugin."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class IntelliJPluginTargetTests(unittest.TestCase):
    def test_plugin_metadata_uses_workspace_harbor_package(self):
        plugin_xml = (ROOT / "src/main/resources/META-INF/plugin.xml").read_text()
        self.assertIn("com.monsky.workspaceharbor.lifecycle", plugin_xml)
        self.assertNotIn("com.monsky.codex.pycharm", plugin_xml)

    def test_gradle_targets_configured_intellij_app(self):
        build = (ROOT / "build.gradle.kts").read_text()
        self.assertIn('environmentVariable("INTELLIJ_APP_PATH")', build)
        self.assertIn("Applications/IntelliJ IDEA.app", build)
        self.assertNotIn("/Applications/PyCharm.app", build)
        self.assertIn('version = "0.1.2"', build)

    def test_java_sources_live_in_product_neutral_package(self):
        expected_main = ROOT / "src/main/java/com/monsky/workspaceharbor/lifecycle"
        expected_test = ROOT / "src/test/java/com/monsky/workspaceharbor/lifecycle"
        self.assertTrue((expected_main / "LifecycleService.java").is_file())
        self.assertTrue((expected_test / "SafetyDecisionTest.java").is_file())
        old_package = ROOT / "src/main/java/com/monsky/codex/pycharm"
        self.assertEqual([], list(old_package.rglob("*.java")))


if __name__ == "__main__":
    unittest.main()
