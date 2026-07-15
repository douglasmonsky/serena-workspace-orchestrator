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
        self.assertIn('version = "0.1.8"', build)

    def test_java_sources_live_in_product_neutral_package(self):
        expected_main = ROOT / "src/main/java/com/monsky/workspaceharbor/lifecycle"
        expected_test = ROOT / "src/test/java/com/monsky/workspaceharbor/lifecycle"
        self.assertTrue((expected_main / "LifecycleService.java").is_file())
        self.assertTrue((expected_test / "SafetyDecisionTest.java").is_file())
        old_package = ROOT / "src/main/java/com/monsky/codex/pycharm"
        self.assertEqual([], list(old_package.rglob("*.java")))

    def test_runtime_discovery_uses_intellij_state_namespace(self):
        service = (ROOT / "src/main/java/com/monsky/workspaceharbor/lifecycle/LifecycleService.java").read_text()
        self.assertIn('"intellij-projects"', service)
        self.assertNotIn('"pycharm-projects"', service)

    def test_plugin_trusts_both_codex_workspace_parents(self):
        service = (ROOT / "src/main/java/com/monsky/workspaceharbor/lifecycle/LifecycleService.java").read_text()
        self.assertIn('home.resolve("Documents/Codex")', service)
        self.assertIn('home.resolve("Developer/Codex")', service)

    def test_gradle_projects_use_intellij_bundled_runtime_before_import(self):
        build = (ROOT / "build.gradle.kts").read_text()
        plugin_xml = (ROOT / "src/main/resources/META-INF/plugin.xml").read_text()
        provisioner = ROOT / "src/main/java/com/monsky/workspaceharbor/lifecycle/GradleModelProvisioner.java"
        runtime = ROOT / "src/main/java/com/monsky/workspaceharbor/lifecycle/IntelliJRuntimeProvisioner.java"
        startup = (ROOT / "src/main/java/com/monsky/workspaceharbor/lifecycle/LifecycleStartupActivity.java").read_text()
        self.assertIn('bundledPlugin("com.intellij.gradle")', build)
        self.assertIn('bundledPlugin("com.intellij.java")', build)
        self.assertIn("<depends>com.intellij.gradle</depends>", plugin_xml)
        self.assertIn("<depends>com.intellij.java</depends>", plugin_xml)
        self.assertTrue(provisioner.is_file())
        self.assertTrue(runtime.is_file())
        self.assertIn("ExternalSystemJdkUtil.USE_INTERNAL_JAVA", provisioner.read_text())
        self.assertIn("ExternalSystemUtil.refreshProject", provisioner.read_text())
        self.assertIn("withCallback", provisioner.read_text())
        self.assertIn("System.getProperty(\"java.home\")", runtime.read_text())
        self.assertIn("IntelliJRuntimeProvisioner.configure(project)", startup)
        self.assertIn("GradleModelProvisioner.configure(project)", startup)


if __name__ == "__main__":
    unittest.main()
