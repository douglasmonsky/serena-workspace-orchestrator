import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pwd
import plistlib
import stat
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


TEST_FILE = Path(__file__).resolve()
ROOT = TEST_FILE.parent.parent if TEST_FILE.parent.name == "tests" else TEST_FILE.parents[2]
CLI = ROOT / "bin" / "intellij-project-trust"
LOADER = importlib.machinery.SourceFileLoader("intellij_project_trust", str(CLI))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
TRUST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TRUST)


class IntelliJProjectTrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.allowed = self.base / "allowed"
        self.allowed.mkdir()
        self.other = self.base / "other"
        self.other.mkdir()
        self.registry = self.base / "options" / "trusted-paths.xml"
        self.registry.parent.mkdir()
        self.registry.write_text("<application/>")

    def tearDown(self):
        self.tmp.cleanup()

    def git_repo(self, path):
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        return path

    def run_cli(self, *args, capture=False, env_extra=None):
        env = os.environ | {"INTELLIJ_TRUST_CONFIG_FILE": str(self.registry), "INTELLIJ_TRUST_ALLOWED_ROOTS": str(self.allowed)} | (env_extra or {})
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
        original = b"<application>"
        self.registry.write_bytes(original)
        self.assertEqual(2, self.run_cli("allow", repo))
        self.assertEqual(original, self.registry.read_bytes())

    def test_audit_reports_documents_as_broad_without_removing_it(self):
        repo = self.git_repo(self.allowed / "repo")
        self.registry.write_text('<application><component name="Trusted.Paths"><option name="TRUSTED_PATHS"><map><entry key="' + str(self.allowed) + '" value="true"/></map></option></component></application>')
        original = self.registry.read_bytes()
        output = self.run_cli("audit", capture=True)
        self.assertIn(str(self.allowed), json.loads(output)["broad"])
        self.assertEqual(original, self.registry.read_bytes())
        self.assertEqual(0, self.run_cli("allow", repo))

    def test_concurrent_allows_preserve_both_entries(self):
        first, second = self.git_repo(self.allowed / "one"), self.git_repo(self.allowed / "two")
        env = os.environ | {"INTELLIJ_TRUST_CONFIG_FILE": str(self.registry), "INTELLIJ_TRUST_ALLOWED_ROOTS": str(self.allowed)}
        processes = [subprocess.Popen([str(CLI), "allow", str(repo)], env=env) for repo in (first, second)]
        self.assertEqual([0, 0], [process.wait() for process in processes])
        entries = {node.get("key") for node in ET.parse(self.registry).findall(".//entry")}
        self.assertEqual({str(first.resolve()), str(second.resolve())}, entries)

    def test_allowed_override_cannot_target_live_registry(self):
        live_home = self.base / "home"
        live_registry = live_home / "Library/Application Support/JetBrains/IntelliJIdea2099.1/options/trusted-paths.xml"
        live_registry.parent.mkdir(parents=True)
        live_registry.write_text("<application/>")
        with mock.patch.object(TRUST, "_account_home", return_value=live_home), mock.patch.dict(
            os.environ, {"INTELLIJ_TRUST_CONFIG_FILE": str(live_registry), "INTELLIJ_TRUST_ALLOWED_ROOTS": str(self.other)}, clear=False
        ):
            self.assertTrue(TRUST._is_live_registry(live_registry))
            self.assertEqual(TRUST._default_allowed_roots(), TRUST._allowed(live_registry))

    def test_live_registry_rejects_pycharm_namespace(self):
        pycharm = (
            self.base
            / "home/Library/Application Support/JetBrains/PyCharm2026.1/options/trusted-paths.xml"
        )
        pycharm.parent.mkdir(parents=True)
        pycharm.write_text("<application/>")
        with mock.patch.object(TRUST, "_account_home", return_value=self.base / "home"):
            self.assertFalse(TRUST._is_live_registry(pycharm))

    def test_allowed_override_cannot_target_default_registry(self):
        live_home = self.base / "default-home"
        live_registry = live_home / "Library/Application Support/JetBrains/IntelliJIdea2099.1/options/trusted-paths.xml"
        live_registry.parent.mkdir(parents=True)
        live_registry.write_text("<application/>")
        app = self.base / "DefaultIntelliJ IDEA.app"
        info = app / "Contents/Info.plist"
        info.parent.mkdir(parents=True)
        with info.open("wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": "2099.1.2"}, handle)
        with mock.patch.object(TRUST, "_account_home", return_value=live_home), mock.patch.dict(
            os.environ, {"INTELLIJ_TRUST_ALLOWED_ROOTS": str(self.other), "INTELLIJ_APP_PATH": str(app)}, clear=False
        ):
            self.assertEqual(live_registry, TRUST._config())
            self.assertEqual(TRUST._default_allowed_roots(), TRUST._allowed(live_registry))

    def test_default_allowed_roots_use_authenticated_account_home(self):
        account_home = self.base / "account-home"
        with mock.patch.object(TRUST, "_account_home", return_value=account_home), mock.patch.dict(
            os.environ, {"HOME": str(self.base / "spoofed-home")}, clear=False
        ):
            self.assertEqual(
                (account_home / "Documents/Codex", account_home / ".codex/src"),
                TRUST._default_allowed_roots(),
            )

    def test_isolated_config_can_use_allowed_override(self):
        repo = self.git_repo(self.other / "isolated")
        self.assertEqual(0, self.run_cli("allow", repo, env_extra={"INTELLIJ_TRUST_ALLOWED_ROOTS": str(self.other)}))
        self.assertEqual("trusted", self.run_cli("status", repo, capture=True, env_extra={"INTELLIJ_TRUST_ALLOWED_ROOTS": str(self.other)}))

    def test_multiple_writes_create_distinct_backups(self):
        first, second = self.git_repo(self.allowed / "one"), self.git_repo(self.allowed / "two")
        self.registry.write_text('<application><component name="Trusted.Paths"><option name="TRUSTED_PATHS"><map/></option></component></application>')
        self.assertEqual(0, self.run_cli("allow", first))
        self.assertEqual(0, self.run_cli("allow", second))
        self.assertEqual(2, len(list(self.registry.parent.glob("trusted-paths.xml.bak-*"))))

    def test_unidentifiable_app_fails_closed_with_multiple_registries(self):
        live_home = self.base / "ambiguous-home"
        for version in ("IntelliJIdea2098.1", "IntelliJIdea2099.1"):
            registry = live_home / "Library/Application Support/JetBrains" / version / "options/trusted-paths.xml"
            registry.parent.mkdir(parents=True)
            registry.write_text("<application/>")
        with mock.patch.object(TRUST, "_account_home", return_value=live_home), mock.patch.dict(
            os.environ, {}, clear=True
        ):
            with self.assertRaises(RuntimeError):
                TRUST._config()

    def test_active_app_version_selects_matching_registry(self):
        live_home = self.base / "versioned-home"
        registries = {}
        for version in ("2024.3", "2025.1", "2026.1"):
            registry = live_home / "Library/Application Support/JetBrains" / ("IntelliJIdea" + version) / "options/trusted-paths.xml"
            registry.parent.mkdir(parents=True)
            registry.write_text("<application/>")
            registries[version] = registry
        app = self.base / "IntelliJ IDEA.app"
        info = app / "Contents/Info.plist"
        info.parent.mkdir(parents=True)
        with info.open("wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": "2026.1.4"}, handle)
        with mock.patch.object(TRUST, "_account_home", return_value=live_home), mock.patch.dict(
            os.environ, {"INTELLIJ_APP_PATH": str(app)}, clear=True
        ):
            self.assertEqual(registries["2026.1"], TRUST._config())

    def test_invalid_app_metadata_or_missing_registry_fails_closed(self):
        live_home = self.base / "invalid-app-home"
        registry = live_home / "Library/Application Support/JetBrains/IntelliJIdea2026.1/options/trusted-paths.xml"
        registry.parent.mkdir(parents=True)
        registry.write_text("<application/>")
        app = self.base / "InvalidIntelliJ IDEA.app"
        info = app / "Contents/Info.plist"
        info.parent.mkdir(parents=True)

        with mock.patch.object(TRUST, "_account_home", return_value=live_home), mock.patch.dict(
            os.environ, {"INTELLIJ_APP_PATH": str(app)}, clear=True
        ):
            with info.open("wb") as handle:
                plistlib.dump({"CFBundleShortVersionString": "2026.1evil"}, handle)
            with self.assertRaises(RuntimeError):
                TRUST._config()

            with info.open("wb") as handle:
                plistlib.dump({"CFBundleShortVersionString": "2027.1.2"}, handle)
            with self.assertRaises(RuntimeError):
                TRUST._config()

            info.write_bytes(b"not a plist")
            with self.assertRaises(RuntimeError):
                TRUST._config()

    def test_missing_live_and_isolated_registries_fail_closed(self):
        isolated = self.base / "missing" / "trusted-paths.xml"
        repo = self.git_repo(self.allowed / "missing-state")
        self.assertEqual(2, self.run_cli("allow", repo, env_extra={"INTELLIJ_TRUST_CONFIG_FILE": str(isolated)}))
        self.assertFalse(isolated.exists())

        live_home = self.base / "missing-live-home"
        live = live_home / "Library/Application Support/JetBrains/IntelliJIdea2099.1/options/trusted-paths.xml"
        result = subprocess.run(
            [str(CLI), "audit"],
            env=os.environ | {"HOME": str(live_home), "INTELLIJ_TRUST_CONFIG_FILE": str(live)},
        )
        self.assertEqual(2, result.returncode)
        self.assertFalse(live.exists())

    def test_missing_audit_override_is_unavailable(self):
        self.registry.unlink()
        result = subprocess.run(
            [str(CLI), "audit"],
            env=os.environ | {"INTELLIJ_TRUST_CONFIG_FILE": str(self.registry), "INTELLIJ_TRUST_ALLOWED_ROOTS": str(self.allowed)},
            text=True,
            stdout=subprocess.PIPE,
        )
        self.assertEqual(2, result.returncode)
        self.assertTrue(json.loads(result.stdout)["malformed"])

    def test_registry_disappearance_before_write_fails_closed(self):
        repo = self.git_repo(self.allowed / "vanishing")
        original_load = TRUST.load_entries

        def load_then_remove(path):
            result = original_load(path)
            path.unlink()
            return result

        with mock.patch.object(TRUST, "load_entries", side_effect=load_then_remove):
            with self.assertRaises(OSError):
                TRUST.allow(repo.resolve(), self.registry)
        self.assertFalse(self.registry.exists())

    def test_allow_preserves_unrelated_comments(self):
        repo = self.git_repo(self.allowed / "commented")
        self.registry.write_text('<application><!-- keep this --><component name="Other" marker="yes"/></application>')
        self.assertEqual(0, self.run_cli("allow", repo))
        self.assertIn("<!-- keep this -->", self.registry.read_text())
        tree = ET.parse(self.registry)
        self.assertEqual("yes", tree.find('.//component[@name="Other"]').get("marker"))

    def test_allow_preserves_processing_instructions(self):
        repo = self.git_repo(self.allowed / "processing-instruction")
        self.registry.write_text('<application><?keep this-value?><component name="Other"/></application>')
        self.assertEqual(0, self.run_cli("allow", repo))
        self.assertIn("<?keep this-value?>", self.registry.read_text())

    def test_spoofed_home_cannot_make_live_registry_honor_override(self):
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        live = account_home / "Library/Application Support/JetBrains/IntelliJIdea2099.1/options/trusted-paths.xml"
        with mock.patch.dict(os.environ, {"HOME": str(self.base / "spoofed-home"), "INTELLIJ_TRUST_ALLOWED_ROOTS": str(self.other)}):
            self.assertTrue(TRUST._is_live_registry(live))
            self.assertEqual(TRUST._default_allowed_roots(), TRUST._allowed(live))

    def test_audit_is_evaluated_once(self):
        payload = {"exact": [], "broad": [], "outsideAllowed": [], "malformed": False}
        environment = {"INTELLIJ_TRUST_CONFIG_FILE": str(self.registry), "INTELLIJ_TRUST_ALLOWED_ROOTS": str(self.allowed)}
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(TRUST, "audit", return_value=payload) as audit_call:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, TRUST.main(["audit"]))
        audit_call.assert_called_once()

    def test_live_trust_posts_exact_root_to_authenticated_plugin(self):
        runtime = self.base / "plugin-runtime.json"
        runtime.write_text(json.dumps({"schemaVersion": 1, "port": 43123, "token": "secret"}))
        repo = self.git_repo(self.allowed / "live")
        response = mock.MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        with mock.patch.object(TRUST, "urlopen", return_value=response) as opened:
            self.assertTrue(TRUST.activate_live(repo.resolve(), runtime))
        request = opened.call_args.args[0]
        self.assertEqual("POST", request.method)
        self.assertEqual("Bearer secret", request.get_header("Authorization"))
        self.assertEqual("http://127.0.0.1:43123/v1/projects/trust?root=%2F" + str(repo.resolve()).lstrip("/").replace("/", "%2F"), request.full_url)

    def test_absent_runtime_is_a_safe_noop(self):
        self.assertFalse(TRUST.activate_live(self.allowed, self.base / "missing.json"))


if __name__ == "__main__":
    unittest.main()
