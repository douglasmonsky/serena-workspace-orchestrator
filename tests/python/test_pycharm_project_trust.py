import json
import os
import stat
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "bin" / "pycharm-project-trust"


class PyCharmProjectTrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.allowed = self.base / "allowed"
        self.allowed.mkdir()
        self.other = self.base / "other"
        self.other.mkdir()
        self.registry = self.base / "options" / "trusted-paths.xml"

    def tearDown(self):
        self.tmp.cleanup()

    def git_repo(self, path):
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        return path

    def run_cli(self, *args, capture=False):
        env = os.environ | {"PYCHARM_TRUST_CONFIG_FILE": str(self.registry), "PYCHARM_TRUST_ALLOWED_ROOTS": str(self.allowed)}
        result = subprocess.run([str(CLI), *map(str, args)], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout.strip() if capture else result.returncode

    def test_allow_accepts_only_exact_git_root(self):
        repo = self.git_repo(self.allowed / "repo")
        self.assertEqual(0, self.run_cli("allow", repo))
        self.assertEqual("trusted", self.run_cli("status", repo, capture=True))
        nested = repo / "nested"; nested.mkdir()
        self.assertNotEqual(0, self.run_cli("allow", nested))
        self.assertNotEqual(0, self.run_cli("allow", self.base / "missing"))
        plain = self.allowed / "plain"; plain.mkdir()
        self.assertNotEqual(0, self.run_cli("allow", plain))
        self.assertNotEqual(0, self.run_cli("allow", self.git_repo(self.other / "outside")))
        escaped_repo = self.git_repo(self.other / "escape-repo")
        escape = self.allowed / "escape"; escape.symlink_to(escaped_repo, target_is_directory=True)
        self.assertNotEqual(0, self.run_cli("allow", escape))

    def test_allow_preserves_unrelated_entries_and_is_idempotent(self):
        repo = self.git_repo(self.allowed / "repo")
        self.registry.parent.mkdir()
        fixture = b'<application><component name="Other"><option name="x" value="y"/></component><component name="Trusted.Paths"><option name="TRUSTED_PATHS"><map><entry key="/unrelated" value="true"/></map></option></component></application>'
        self.registry.write_bytes(fixture)
        self.assertEqual(0, self.run_cli("allow", repo))
        once = self.registry.read_bytes()
        self.assertEqual(0, self.run_cli("allow", repo))
        self.assertEqual(once, self.registry.read_bytes())
        tree = ET.parse(self.registry)
        entries = tree.findall('.//component[@name="Trusted.Paths"]//entry')
        self.assertEqual({"/unrelated", str(repo.resolve())}, {e.get("key").replace("$USER_HOME$", str(Path.home())) for e in entries})
        self.assertIsNotNone(tree.find('.//component[@name="Other"]'))
        self.assertEqual(0o600, stat.S_IMODE(self.registry.stat().st_mode))

    def test_malformed_xml_is_not_replaced(self):
        repo = self.git_repo(self.allowed / "repo")
        self.registry.parent.mkdir()
        original = b"<application>"
        self.registry.write_bytes(original)
        self.assertEqual(2, self.run_cli("allow", repo))
        self.assertEqual(original, self.registry.read_bytes())

    def test_audit_reports_documents_as_broad_without_removing_it(self):
        repo = self.git_repo(self.allowed / "repo")
        self.registry.parent.mkdir()
        self.registry.write_text('<application><component name="Trusted.Paths"><option name="TRUSTED_PATHS"><map><entry key="' + str(self.allowed) + '" value="true"/></map></option></component></application>')
        original = self.registry.read_bytes()
        output = self.run_cli("audit", capture=True)
        self.assertIn(str(self.allowed), json.loads(output)["broad"])
        self.assertEqual(original, self.registry.read_bytes())
        self.assertEqual(0, self.run_cli("allow", repo))

    def test_concurrent_allows_preserve_both_entries(self):
        first, second = self.git_repo(self.allowed / "one"), self.git_repo(self.allowed / "two")
        env = os.environ | {"PYCHARM_TRUST_CONFIG_FILE": str(self.registry), "PYCHARM_TRUST_ALLOWED_ROOTS": str(self.allowed)}
        processes = [subprocess.Popen([str(CLI), "allow", str(repo)], env=env) for repo in (first, second)]
        self.assertEqual([0, 0], [process.wait() for process in processes])
        entries = {node.get("key") for node in ET.parse(self.registry).findall(".//entry")}
        self.assertEqual({str(first.resolve()), str(second.resolve())}, entries)


if __name__ == "__main__":
    unittest.main()
