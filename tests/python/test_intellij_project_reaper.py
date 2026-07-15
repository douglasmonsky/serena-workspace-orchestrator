import importlib.machinery
import importlib.util
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch
from pathlib import Path

TEST_FILE = Path(__file__).resolve()
ROOT = TEST_FILE.parent.parent if TEST_FILE.parent.name == "tests" else TEST_FILE.parents[2]
SCRIPT = str(ROOT / "bin" / "intellij-project-reaper")
loader = importlib.machinery.SourceFileLoader("reaper", SCRIPT)
spec = importlib.util.spec_from_loader(loader.name, loader)
reaper = importlib.util.module_from_spec(spec)
loader.exec_module(reaper)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "managed.json"

    def tearDown(self): self.tmp.cleanup()

    def test_defaults_never_read_pycharm_state(self):
        with patch.dict(os.environ, {}, clear=True):
            config = reaper.environment()
        self.assertIn("intellij-projects", str(config["state"]))
        self.assertNotIn("pycharm-projects", str(config["state"]))

    def test_register_deduplicates_canonical_root_and_writes_private_atomically(self):
        root = Path(self.tmp.name) / "project"; root.mkdir()
        reaper.register_root(str(root), self.state, now="2026-01-01T00:00:00Z")
        reaper.register_root(str(root / ".." / "project"), self.state, now="2026-01-01T00:01:00Z")
        data = json.loads(self.state.read_text())
        self.assertEqual(1, len(data["projects"]))
        self.assertEqual(str(root.resolve()), data["projects"][0]["root"])
        self.assertEqual(0o600, stat.S_IMODE(self.state.stat().st_mode))

    def test_touch_only_existing_and_corruption_is_quarantined(self):
        self.assertFalse(reaper.touch_root("/absent", self.state, now="2026-01-01T00:00:00Z"))
        self.state.write_text("not json")
        registry, corrupt = reaper.load_registry(self.state)
        self.assertTrue(corrupt); self.assertEqual([], registry["projects"])
        self.assertEqual(1, len(list(self.state.parent.glob("managed.corrupt-*.json"))))

    def test_unregister_removes_only_the_requested_managed_root(self):
        first = Path(self.tmp.name) / "first"; first.mkdir()
        second = Path(self.tmp.name) / "second"; second.mkdir()
        reaper.register_root(str(first), self.state, now="2026-01-01T00:00:00Z")
        reaper.register_root(str(second), self.state, now="2026-01-01T00:00:00Z")
        first.rmdir()
        config = {"state": self.state, "runtime": self.state.parent / "runtime.json",
                  "broker": self.state.parent / "broker", "idle": 1800, "cap": 4}
        with patch.object(reaper, "environment", return_value=config), \
                patch.object(reaper, "runtime_client", return_value={"token": "t"}), \
                patch.object(reaper, "plugin_inventory", return_value={}), \
                patch.object(reaper, "broker_live_roots", return_value=set()):
            self.assertEqual(0, reaper.main_with_environment(["unregister", str(first)]))
        projects = json.loads(self.state.read_text())["projects"]
        self.assertEqual([str(second.resolve())], [item["root"] for item in projects])

    def test_unregister_fails_closed_for_live_existing_or_unknown_roots(self):
        root = Path(self.tmp.name) / "live"; root.mkdir()
        reaper.register_root(str(root), self.state, now="2026-01-01T00:00:00Z")
        config = {"state": self.state, "runtime": self.state.parent / "runtime.json",
                  "broker": self.state.parent / "broker", "idle": 1800, "cap": 4}
        with patch.object(reaper, "environment", return_value=config), \
                patch.object(reaper, "runtime_client", return_value={"token": "t"}), \
                patch.object(reaper, "plugin_inventory", return_value={}), \
                patch.object(reaper, "broker_live_roots", return_value=set()):
            self.assertEqual(2, reaper.main_with_environment(["unregister", str(root)]))
        self.assertEqual(1, len(json.loads(self.state.read_text())["projects"]))

    def test_unregister_rejects_unknown_inventory_and_active_lease(self):
        root = Path(self.tmp.name) / "removed"; root.mkdir()
        reaper.register_root(str(root), self.state, now="2026-01-01T00:00:00Z")
        root.rmdir(); canonical = str(root.resolve())
        config = {"state": self.state, "runtime": self.state.parent / "runtime.json",
                  "broker": self.state.parent / "broker", "idle": 1800, "cap": 4}
        cases = [(None, set()), ({canonical: {"safe": True}}, set()), ({}, None), ({}, {canonical})]
        for plugins, leases in cases:
            with self.subTest(plugins=plugins, leases=leases), \
                    patch.object(reaper, "environment", return_value=config), \
                    patch.object(reaper, "runtime_client", return_value={"token": "t"}), \
                    patch.object(reaper, "plugin_inventory", return_value=plugins), \
                    patch.object(reaper, "broker_live_roots", return_value=leases):
                self.assertEqual(2, reaper.main_with_environment(["unregister", canonical]))
        self.assertEqual(1, len(json.loads(self.state.read_text())["projects"]))

    def test_python39_cleanup_quarantines_malformed_isolated_registry(self):
        self.state.write_text("not json")
        environment = os.environ | {
            "INTELLIJ_PROJECT_REAPER_STATE_FILE": str(self.state),
            "INTELLIJ_PROJECT_REAPER_RUNTIME_FILE": str(Path(self.tmp.name) / "missing-runtime.json"),
            "INTELLIJ_PROJECT_REAPER_BROKER_COMMAND": str(Path(self.tmp.name) / "missing-broker"),
        }
        completed = subprocess.run(
            ["/usr/bin/python3", SCRIPT, "cleanup"],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(1, len(list(self.state.parent.glob("managed.corrupt-*.json"))))

    def test_python39_cleanup_quarantines_structurally_invalid_registries(self):
        invalid_registries = [
            [],
            {},
            {"schemaVersion": 1, "projects": [None]},
            {"schemaVersion": 1, "projects": [[]]},
        ]
        for registry in invalid_registries:
            with self.subTest(registry=registry):
                self.state.write_text(json.dumps(registry))
                environment = os.environ | {
                    "INTELLIJ_PROJECT_REAPER_STATE_FILE": str(self.state),
                    "INTELLIJ_PROJECT_REAPER_RUNTIME_FILE": str(Path(self.tmp.name) / "invalid-runtime.json"),
                    "INTELLIJ_PROJECT_REAPER_BROKER_COMMAND": str(Path(self.tmp.name) / "missing-broker"),
                }
                completed = subprocess.run(
                    ["/usr/bin/python3", SCRIPT, "cleanup"],
                    capture_output=True,
                    text=True,
                    env=environment,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertFalse(self.state.exists())
                self.assertTrue(list(self.state.parent.glob("managed.corrupt-*.json")))
                report = json.loads(completed.stdout)
                self.assertTrue(report["reportOnly"])
                self.assertEqual([], report["selected"])

    def test_load_registry_quarantines_invalid_project_records(self):
        root = str(Path(self.tmp.name).resolve())
        timestamp = "2026-01-01T00:00:00Z"
        invalid_records = [
            {"root": True, "registeredAt": timestamp, "lastReadyAt": timestamp},
            {"root": root, "lastReadyAt": timestamp},
            {"root": "", "registeredAt": timestamp, "lastReadyAt": timestamp},
            {"root": "relative", "registeredAt": timestamp, "lastReadyAt": timestamp},
            {"root": root + "/.", "registeredAt": timestamp, "lastReadyAt": timestamp},
            {"root": root, "registeredAt": "bad", "lastReadyAt": timestamp},
            {"root": root, "registeredAt": timestamp, "lastReadyAt": "bad"},
        ]
        for record in invalid_records:
            with self.subTest(record=record):
                self.state.write_text(json.dumps({"schemaVersion": 1, "projects": [record]}))
                registry, corrupt = reaper.load_registry(self.state)
                self.assertTrue(corrupt)
                self.assertEqual([], registry["projects"])
                self.assertFalse(self.state.exists())

    def test_python39_cleanup_quarantines_invalid_project_records(self):
        root = str(Path(self.tmp.name).resolve())
        timestamp = "2026-01-01T00:00:00Z"
        invalid_records = [
            {"root": True, "registeredAt": timestamp, "lastReadyAt": timestamp},
            {"root": root, "lastReadyAt": timestamp},
            {"root": "", "registeredAt": timestamp, "lastReadyAt": timestamp},
            {"root": "relative", "registeredAt": timestamp, "lastReadyAt": timestamp},
            {"root": root + "/.", "registeredAt": timestamp, "lastReadyAt": timestamp},
            {"root": root, "registeredAt": "bad", "lastReadyAt": timestamp},
            {"root": root, "registeredAt": timestamp, "lastReadyAt": "bad"},
        ]
        for record in invalid_records:
            with self.subTest(record=record):
                self.state.write_text(json.dumps({"schemaVersion": 1, "projects": [record]}))
                environment = os.environ | {
                    "INTELLIJ_PROJECT_REAPER_STATE_FILE": str(self.state),
                    "INTELLIJ_PROJECT_REAPER_RUNTIME_FILE": str(Path(self.tmp.name) / "invalid-runtime.json"),
                    "INTELLIJ_PROJECT_REAPER_BROKER_COMMAND": str(Path(self.tmp.name) / "missing-broker"),
                }
                completed = subprocess.run(
                    ["/usr/bin/python3", SCRIPT, "cleanup"],
                    capture_output=True,
                    text=True,
                    env=environment,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertFalse(self.state.exists())
                report = json.loads(completed.stdout)
                self.assertTrue(report["reportOnly"])
                self.assertEqual([], report["selected"])

    def test_unregistered_is_unmanaged(self):
        decision = reaper.classify("/not-registered", {}, {}, now=2000)
        self.assertEqual("unmanaged", decision.classification)


class PolicyTests(unittest.TestCase):
    def test_status_contract_includes_unmanaged_open_roots_and_protects_malformed_safety(self):
        body = {"projects": [{"root": "/registered", "safeToClose": True, "reasons": [], "known": {"unsavedDocuments": True, "indexing": True, "run": True, "terminal": True, "debugger": True, "modal": True, "closing": True}, "counts": {"unsavedDocuments": 0, "run": 0, "terminal": 0, "debugger": 0}, "active": {"indexing": False, "modal": False, "closing": False}}, {"root": "/unmanaged", "safeToClose": False, "reasons": ["unsaved-documents"], "known": {"unsavedDocuments": True, "indexing": True, "run": True, "terminal": True, "debugger": True, "modal": True, "closing": True}, "counts": {"unsavedDocuments": 1, "run": 0, "terminal": 0, "debugger": 0}, "active": {"indexing": False, "modal": False, "closing": False}}]}
        plugins = reaper.project_status(body)
        self.assertEqual({reaper.canonical("/registered"), reaper.canonical("/unmanaged")}, set(plugins))
        self.assertFalse(plugins[reaper.canonical("/unmanaged")]["safe"])
        with self.assertRaises(ValueError): reaper.project_status({"projects": [body["projects"][0] | {"safeToClose": True, "reasons": ["unsaved-documents"]}]})

    def test_invalid_integration_overrides_fail_closed_before_requests(self):
        for name, value in [("INTELLIJ_PROJECT_REAPER_CAP", "-1"), ("INTELLIJ_PROJECT_REAPER_IDLE_SECONDS", "-1"), ("INTELLIJ_PROJECT_REAPER_IDLE_SECONDS", "bad"), ("INTELLIJ_PROJECT_REAPER_STATE_FILE", "relative"), ("INTELLIJ_PROJECT_REAPER_RUNTIME_FILE", "relative"), ("INTELLIJ_PROJECT_REAPER_BROKER_COMMAND", "relative")]:
            with patch.dict(os.environ, {name: value}, clear=False), patch.object(reaper, "http_request") as request:
                self.assertEqual(2, reaper.main_with_environment(["cleanup"]))
                request.assert_not_called()

    def test_zero_cap_and_idle_overrides_are_valid_for_isolated_one_shot_cleanup(self):
        with patch.dict(os.environ, {"INTELLIJ_PROJECT_REAPER_CAP": "0", "INTELLIJ_PROJECT_REAPER_IDLE_SECONDS": "0"}, clear=True):
            config = reaper.environment()
        self.assertEqual(0, config["cap"])
        self.assertEqual(0, config["idle"])
    def test_fail_closed_and_ordering_and_cap(self):
        records = {r: {"root": r, "lastReadyAt": "1970-01-01T00:00:00Z"} for r in ["/old", "/new", "/third", "/unsafe", "/leased", "/missing"]}
        records["/new"]["lastReadyAt"] = "1970-01-01T00:16:00Z"
        plugins = {"/unsafe": {"safe": False}, "/old": {"safe": True}, "/new": {"safe": True}, "/third": {"safe": True}, "/missing": {"safe": True}}
        selected, decisions = reaper.select_cleanup(records, {"/leased"}, plugins, now=2000, cap=4)
        self.assertEqual(["/old"], selected)
        self.assertEqual("protected", decisions["/unsafe"].classification)
        self.assertEqual("protected", decisions["/leased"].classification)
        self.assertIn("plugin-unknown", reaper.classify("/new", records, {}, now=2000, plugins={}).reasons)

    def test_open_count_missing_first_existing_lru_and_noop_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing_old = reaper.canonical(Path(tmp) / "existing-old"); Path(existing_old).mkdir()
            existing_new = reaper.canonical(Path(tmp) / "existing-new"); Path(existing_new).mkdir()
            missing = reaper.canonical(Path(tmp) / "missing")
            records = {r: {"root": r, "lastReadyAt": t} for r, t in [(existing_old, "1970-01-01T00:00:00Z"), (existing_new, "1970-01-01T00:01:00Z"), (missing, "1970-01-01T00:02:00Z"), ("/closed", "1970-01-01T00:00:00Z")]}
            plugins = {r: {"safe": True} for r in (existing_old, existing_new, missing)}
            selected, decisions = reaper.select_cleanup(records, set(), plugins, now=3000, cap=1)
            self.assertEqual([missing, existing_old], selected)
            self.assertEqual([], reaper.select_cleanup(records, set(), plugins, now=3000, cap=3)[0])
            records[existing_new]["lastReadyAt"] = "bad"
            self.assertIn("timestamp-unknown", reaper.classify(existing_new, records, set(), now=3000, plugins=plugins).reasons)
            records[existing_old]["lastReadyAt"] = "1970-01-01T00:10:00Z"
            self.assertIn("idle-under-1800", reaper.classify(existing_old, records, set(), now=2000, plugins=plugins).reasons)


class ClientTests(unittest.TestCase):
    def test_recycle_closes_only_a_managed_safe_exact_project(self):
        root = reaper.canonical("/workspace")
        record = {
            "root": root,
            "registeredAt": "2026-01-01T00:00:00Z",
            "lastReadyAt": "2026-01-01T00:00:00Z",
        }
        item = {"root": root, "safe": True, "reasons": [], "known": {}, "counts": {}, "active": {}}
        config = {
            "state": Path("/state"),
            "runtime": Path("/runtime"),
            "broker": Path("/broker"),
            "idle": 1800,
            "cap": 4,
            "app": Path("/Applications/IntelliJ IDEA.app"),
        }
        output = io.StringIO()
        with patch.object(reaper, "environment", return_value=config), patch.object(
            reaper, "runtime_client", return_value={"token": "t"}
        ), patch.object(
            reaper, "inspect_project", return_value=(item, True)
        ), patch.object(
            reaper, "load_registry", return_value=({"projects": [record]}, False)
        ), patch.object(
            reaper, "close_verified", return_value=True
        ) as close, patch("sys.stdout", output):
            status = reaper.main_with_environment(["recycle", root, "--json"])

        self.assertEqual(0, status)
        self.assertEqual({"root": root, "status": "closed"}, json.loads(output.getvalue()))
        close.assert_called_once_with(root, {"token": "t"})

    def test_recycle_refuses_unmanaged_or_unsafe_projects(self):
        root = reaper.canonical("/workspace")
        config = {
            "state": Path("/state"),
            "runtime": Path("/runtime"),
            "broker": Path("/broker"),
            "idle": 1800,
            "cap": 4,
            "app": Path("/Applications/IntelliJ IDEA.app"),
        }
        unsafe = {
            "root": root,
            "safe": False,
            "reasons": ["unsaved-documents"],
            "known": {},
            "counts": {},
            "active": {},
        }
        for records, item in (([], unsafe), ([{"root": root}], unsafe)):
            with self.subTest(records=records), patch.object(
                reaper, "environment", return_value=config
            ), patch.object(
                reaper, "runtime_client", return_value={"token": "t"}
            ), patch.object(
                reaper, "inspect_project", return_value=(item, True)
            ), patch.object(
                reaper, "load_registry", return_value=({"projects": records}, False)
            ), patch.object(reaper, "close_verified") as close, patch(
                "sys.stdout", new_callable=io.StringIO
            ):
                self.assertEqual(
                    1, reaper.main_with_environment(["recycle", root, "--json"])
                )
                close.assert_not_called()

    def test_hung_restart_requires_exact_process_and_repeated_unresponsiveness(self):
        runtime = {
            "pid": 101,
            "processStartInstant": "2026-01-01T00:00:00Z",
            "pluginVersion": reaper.HUNG_RESTART_PLUGIN_VERSION,
            "token": "t",
        }
        app = Path("/Applications/IntelliJ IDEA.app")
        executable = str(app / "Contents/MacOS/idea")
        with patch.object(
            reaper, "process_start", side_effect=[runtime["processStartInstant"], runtime["processStartInstant"], None]
        ), patch.object(
            reaper, "process_command", return_value=executable
        ), patch.object(
            reaper, "runtime_responds", side_effect=[False, False, False]
        ) as responds, patch.object(reaper.os, "kill") as kill, patch.object(
            reaper.time, "sleep"
        ):
            result = reaper.restart_hung_intellij(runtime, app, probe_interval=0)

        self.assertEqual("stopped-term", result["status"])
        self.assertEqual(3, responds.call_count)
        kill.assert_called_once_with(101, signal.SIGTERM)

        with patch.object(
            reaper, "process_start", return_value=runtime["processStartInstant"]
        ), patch.object(
            reaper, "process_command", return_value=executable
        ), patch.object(
            reaper, "runtime_responds", return_value=True
        ), patch.object(reaper.os, "kill") as kill:
            result = reaper.restart_hung_intellij(runtime, app, probe_interval=0)
        self.assertEqual("responsive", result["status"])
        kill.assert_not_called()

        with patch.object(
            reaper, "process_start", return_value=runtime["processStartInstant"]
        ), patch.object(
            reaper, "process_command", return_value="/Applications/Other.app/other"
        ), patch.object(reaper, "runtime_responds") as responds, patch.object(
            reaper.os, "kill"
        ) as kill:
            result = reaper.restart_hung_intellij(runtime, app, probe_interval=0)
        self.assertEqual("identity-mismatch", result["status"])
        responds.assert_not_called()
        kill.assert_not_called()

        legacy_runtime = runtime | {"pluginVersion": "0.1.7"}
        with patch.object(reaper, "process_start") as started, patch.object(
            reaper, "process_command"
        ) as command, patch.object(reaper, "runtime_responds") as responds, patch.object(
            reaper.os, "kill"
        ) as kill:
            result = reaper.restart_hung_intellij(
                legacy_runtime, app, probe_interval=0
            )
        self.assertEqual("identity-mismatch", result["status"])
        started.assert_not_called()
        command.assert_not_called()
        responds.assert_not_called()
        kill.assert_not_called()

        with patch.object(
            reaper,
            "process_start",
            side_effect=[
                runtime["processStartInstant"],
                runtime["processStartInstant"],
                runtime["processStartInstant"],
                None,
            ],
        ), patch.object(
            reaper, "process_command", return_value=executable
        ), patch.object(
            reaper, "runtime_responds", side_effect=[False, False, False]
        ), patch.object(reaper.os, "kill") as kill, patch.object(
            reaper.time, "sleep"
        ):
            result = reaper.restart_hung_intellij(
                runtime,
                app,
                probe_interval=0,
                term_timeout=-1,
                kill_timeout=1,
            )
        self.assertEqual("stopped-kill", result["status"])
        self.assertEqual(
            [call(101, signal.SIGTERM), call(101, signal.SIGKILL)],
            kill.call_args_list,
        )

    def test_runtime_responsiveness_uses_the_authenticated_ide_health_probe(self):
        runtime = {"token": "t"}
        with patch.object(
            reaper, "http_request", return_value=(200, {"responsive": True})
        ) as request:
            self.assertTrue(reaper.runtime_responds(runtime))
        request.assert_called_once_with("GET", "/v1/health", runtime)
        for response in (
            (202, {"responsive": False}),
            (200, {"responsive": "yes"}),
            (500, {}),
        ):
            with self.subTest(response=response), patch.object(
                reaper, "http_request", return_value=response
            ):
                self.assertFalse(reaper.runtime_responds(runtime))

    def test_inspect_reports_exact_project_indexing_and_modal_state(self):
        root = reaper.canonical("/workspace")
        item = {
            "root": root,
            "safeToClose": False,
            "reasons": ["indexing", "modal-active"],
            "known": {
                "unsavedDocuments": True,
                "indexing": True,
                "run": True,
                "terminal": True,
                "debugger": True,
                "modal": True,
                "closing": True,
            },
            "counts": {
                "unsavedDocuments": 0,
                "run": 0,
                "terminal": 0,
                "debugger": 0,
            },
            "active": {"indexing": True, "modal": True, "closing": False},
        }
        output = io.StringIO()
        with patch.object(
            reaper, "environment", return_value={
                "state": Path("/state"),
                "runtime": Path("/runtime"),
                "broker": Path("/broker"),
                "idle": 1800,
                "cap": 4,
            }
        ), patch.object(
            reaper, "runtime_client", return_value={"token": "t"}
        ), patch.object(
            reaper, "http_request", return_value=(200, {"projects": [item]})
        ) as request, patch("sys.stdout", output):
            status = reaper.main_with_environment(["inspect", root, "--json"])

        self.assertEqual(0, status)
        self.assertEqual(
            {
                "root": root,
                "safe": False,
                "reasons": ["indexing", "modal-active"],
                "known": item["known"],
                "counts": item["counts"],
                "active": item["active"],
            },
            json.loads(output.getvalue()),
        )
        request.assert_called_once_with(
            "GET", "/v1/projects/status", {"token": "t"}
        )

    def test_inspect_distinguishes_missing_project_from_unknown_runtime(self):
        root = reaper.canonical("/workspace")
        config = {
            "state": Path("/state"),
            "runtime": Path("/runtime"),
            "broker": Path("/broker"),
            "idle": 1800,
            "cap": 4,
        }
        with patch.object(reaper, "environment", return_value=config), patch.object(
            reaper, "runtime_client", return_value={"token": "t"}
        ), patch.object(
            reaper, "http_request", return_value=(200, {"projects": []})
        ):
            self.assertEqual(
                1, reaper.main_with_environment(["inspect", root, "--json"])
            )
        with patch.object(reaper, "environment", return_value=config), patch.object(
            reaper, "runtime_client", return_value=None
        ):
            self.assertEqual(
                2, reaper.main_with_environment(["inspect", root, "--json"])
            )

    def test_model_ready_is_authenticated_and_fail_closed(self):
        runtime = {"token": "t"}
        with patch.object(reaper, "http_request", return_value=(200, {"ready": True})) as request:
            self.assertTrue(reaper.model_ready("/workspace", runtime))
            request.assert_called_once_with("GET", "/v1/projects/model", runtime, "/workspace")
        for response in ((202, {"ready": False}), (200, {"ready": "yes"}), (500, {})):
            with self.subTest(response=response), patch.object(reaper, "http_request", return_value=response):
                self.assertFalse(reaper.model_ready("/workspace", runtime))

    def test_is_open_uses_authenticated_status_inventory_and_fails_closed(self):
        root = reaper.canonical("/open")
        item = {"root": root, "safeToClose": True, "reasons": [], "known": {"unsavedDocuments": True, "indexing": True, "run": True, "terminal": True, "debugger": True, "modal": True, "closing": True}, "counts": {"unsavedDocuments": 0, "run": 0, "terminal": 0, "debugger": 0}, "active": {"indexing": False, "modal": False, "closing": False}}
        with patch.object(reaper, "runtime_client", return_value={"token": "t"}), patch.object(reaper, "http_request", return_value=(200, {"projects": [item]})) as request:
            self.assertEqual(0, reaper.main_with_environment(["is-open", root]))
            request.assert_called_once_with("GET", "/v1/projects/status", {"token": "t"})
        with patch.object(reaper, "runtime_client", return_value={"token": "t"}), patch.object(reaper, "http_request", return_value=(200, {"projects": []})):
            self.assertEqual(1, reaper.main_with_environment(["is-open", root]))
        for runtime, response in [(None, None), ({"token": "t"}, (500, {})), ({"token": "t"}, (200, {"projects": [{}]}))]:
            with patch.object(reaper, "runtime_client", return_value=runtime), patch.object(reaper, "http_request", return_value=response):
                self.assertEqual(2, reaper.main_with_environment(["is-open", root]))

    def test_status_json_reports_open_unmanaged_and_missing_managed_as_protected(self):
        managed, opened = reaper.canonical("/managed"), reaper.canonical("/opened")
        records = [{"root": managed, "lastReadyAt": "1970-01-01T00:00:00Z"}]
        item = {"root": opened, "safeToClose": True, "reasons": [], "known": {"unsavedDocuments": True, "indexing": True, "run": True, "terminal": True, "debugger": True, "modal": True, "closing": True}, "counts": {"unsavedDocuments": 0, "run": 0, "terminal": 0, "debugger": 0}, "active": {"indexing": False, "modal": False, "closing": False}}
        output = io.StringIO()
        with patch.object(reaper, "load_registry", return_value=({"projects": records}, False)), patch.object(reaper, "runtime_client", return_value={"token": "t"}), patch.object(reaper, "http_request", return_value=(200, {"projects": [item]})), patch.object(reaper, "broker_live_roots", return_value=set()), patch("sys.stdout", output):
            self.assertEqual(0, reaper.main_with_environment(["status", "--json"]))
        result = json.loads(output.getvalue())
        self.assertFalse(result["reportOnly"])
        self.assertEqual("unmanaged", result["decisions"][opened]["classification"])
        self.assertEqual("protected", result["decisions"][managed]["classification"])
        self.assertIn("plugin-unknown", result["decisions"][managed]["reasons"])

    def test_status_uses_configured_idle_threshold(self):
        root = reaper.canonical("/managed")
        records = [{"root": root, "lastReadyAt": reaper.utc_now()}]
        plugin = {root: {"safe": True}}
        output = io.StringIO()
        with patch.object(reaper, "load_registry", return_value=({"projects": records}, False)), \
                patch.object(reaper, "runtime_client", return_value={"token": "t"}), \
                patch.object(reaper, "plugin_inventory", return_value=plugin), \
                patch.object(reaper, "broker_live_roots", return_value=set()), \
                patch.dict(os.environ, {"INTELLIJ_PROJECT_REAPER_IDLE_SECONDS": "0"}, clear=True), \
                patch("sys.stdout", output):
            self.assertEqual(0, reaper.main_with_environment(["status", "--json"]))
        result = json.loads(output.getvalue())
        self.assertEqual("eligible", result["decisions"][root]["classification"])
    def test_broker_snapshot_accepts_idle_and_rejects_failures_or_malformed_roots(self):
        root = reaper.canonical("/managed")
        valid = {root: {"project_root": root, "backend": "JetBrains", "live_leases": 0, "idle_since": None, "last_used_at": "2026-01-01T00:00:00Z"}}
        with patch.object(reaper.subprocess, "run", return_value=type("Done", (), {"returncode": 0, "stdout": json.dumps(valid), "stderr": ""})()):
            self.assertEqual(set(), reaper.broker_live_roots())
        for stdout, returncode in [("not json", 0), (json.dumps({"/wrong": valid[root]}), 0), ("", 1)]:
            with patch.object(reaper.subprocess, "run", return_value=type("Done", (), {"returncode": returncode, "stdout": stdout, "stderr": "failed"})()):
                self.assertIsNone(reaper.broker_live_roots())

    def test_broker_snapshot_rejects_boolean_leases_and_invalid_timestamps(self):
        root = reaper.canonical("/managed")
        valid = {"project_root": root, "backend": "JetBrains", "live_leases": 0, "idle_since": None, "last_used_at": "2026-01-01T00:00:00Z"}
        for field, value in [("live_leases", False), ("idle_since", {}), ("idle_since", "bad"), ("last_used_at", []), ("last_used_at", "bad")]:
            item = valid | {field: value}
            with patch.object(reaper.subprocess, "run", return_value=type("Done", (), {"returncode": 0, "stdout": json.dumps({root: item}), "stderr": ""})()):
                self.assertIsNone(reaper.broker_live_roots(), field)

    def test_cleanup_main_uses_valid_snapshot_and_fails_closed_on_unknown_broker(self):
        root = reaper.canonical("/managed-0")
        records = [{"root": reaper.canonical(f"/managed-{index}"), "lastReadyAt": f"1970-01-01T00:0{index}:00Z"} for index in range(5)]
        valid = {root: {"project_root": root, "backend": "JetBrains", "live_leases": 0, "idle_since": None, "last_used_at": "2026-01-01T00:00:00Z"}}

        def run_cleanup(stdout, returncode=0):
            close = unittest.mock.Mock(return_value=True)
            completed = type("Done", (), {"returncode": returncode, "stdout": stdout, "stderr": "failed"})()
            output = io.StringIO()
            def request(method, path, runtime):
                item = {"safeToClose": True, "reasons": [], "known": {"unsavedDocuments": True, "indexing": True, "run": True, "terminal": True, "debugger": True, "modal": True, "closing": True}, "counts": {"unsavedDocuments": 0, "run": 0, "terminal": 0, "debugger": 0}, "active": {"indexing": False, "modal": False, "closing": False}}
                return 200, {"projects": [{"root": record["root"]} | item for record in records]}
            with patch.object(reaper, "load_registry", return_value=({"projects": records}, False)), patch.object(reaper, "runtime_client", return_value={"token": "t"}), patch.object(reaper, "http_request", request), patch.object(reaper.subprocess, "run", return_value=completed), patch.object(reaper, "close_verified", close), patch.object(reaper, "remove_root"), patch.object(sys, "argv", ["reaper", "cleanup"]), patch("sys.stdout", output):
                self.assertEqual(0, reaper.main())
            return json.loads(output.getvalue()), close

        result, close = run_cleanup(json.dumps(valid))
        self.assertEqual([root], result["selected"]); self.assertFalse(result["reportOnly"]); close.assert_called_once()
        live = valid | {root: valid[root] | {"live_leases": 1}}
        result, close = run_cleanup(json.dumps(live))
        self.assertNotIn(root, result["selected"]); self.assertFalse(result["reportOnly"]); close.assert_called_once()
        malformed = ["not json", json.dumps({root: valid[root] | {"live_leases": False}}), json.dumps({root: valid[root] | {"idle_since": {}}}), json.dumps({root: valid[root] | {"last_used_at": []}})]
        for stdout in malformed:
            result, close = run_cleanup(stdout)
            self.assertTrue(result["reportOnly"]); self.assertEqual([], result["selected"]); close.assert_not_called()
        result, close = run_cleanup("", returncode=1)
        self.assertTrue(result["reportOnly"]); close.assert_not_called()

    def test_cleanup_rechecks_broker_lease_immediately_before_close(self):
        root = reaper.canonical("/managed")
        record = {"root": root, "registeredAt": "1970-01-01T00:00:00Z", "lastReadyAt": "1970-01-01T00:00:00Z"}
        plugin = {root: {"safe": True}}
        close = unittest.mock.Mock(return_value=True)
        output = io.StringIO()
        with patch.object(reaper, "load_registry", return_value=({"projects": [record]}, False)), \
                patch.object(reaper, "runtime_client", return_value={"token": "t"}), \
                patch.object(reaper, "plugin_inventory", return_value=plugin), \
                patch.object(reaper, "broker_live_roots", side_effect=[set(), {root}]), \
                patch.object(reaper, "close_verified", close), \
                patch.dict(os.environ, {"INTELLIJ_PROJECT_REAPER_CAP": "0", "INTELLIJ_PROJECT_REAPER_IDLE_SECONDS": "0"}, clear=True), \
                patch("sys.stdout", output):
            self.assertEqual(0, reaper.main_with_environment(["cleanup"]))
        close.assert_not_called()

    def test_cleanup_rechecks_plugin_safety_immediately_before_close(self):
        root = reaper.canonical("/managed")
        record = {"root": root, "registeredAt": "1970-01-01T00:00:00Z", "lastReadyAt": "1970-01-01T00:00:00Z"}
        close = unittest.mock.Mock(return_value=True)
        with patch.object(reaper, "load_registry", return_value=({"projects": [record]}, False)), \
                patch.object(reaper, "runtime_client", return_value={"token": "t"}), \
                patch.object(reaper, "plugin_inventory", side_effect=[{root: {"safe": True}}, {root: {"safe": False}}]), \
                patch.object(reaper, "broker_live_roots", return_value=set()), \
                patch.object(reaper, "close_verified", close), \
                patch.dict(os.environ, {"INTELLIJ_PROJECT_REAPER_CAP": "0", "INTELLIJ_PROJECT_REAPER_IDLE_SECONDS": "0"}, clear=True), \
                patch("sys.stdout", io.StringIO()):
            self.assertEqual(0, reaper.main_with_environment(["cleanup"]))
        close.assert_not_called()

    def test_cleanup_preserves_root_reregistered_while_close_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "managed.json"
            root = reaper.canonical("/managed")
            selected_at = "1970-01-01T00:00:00Z"
            refreshed_at = "1970-01-01T00:10:00Z"
            reaper.register_root(root, state, now=selected_at)

            def close_and_reregister(*_args, **_kwargs):
                reaper.register_root(root, state, now=refreshed_at)
                return True

            plugin = {root: {"safe": True}}
            with patch.object(reaper, "runtime_client", return_value={"token": "t"}), \
                    patch.object(reaper, "plugin_inventory", return_value=plugin), \
                    patch.object(reaper, "broker_live_roots", return_value=set()), \
                    patch.object(reaper, "close_verified", side_effect=close_and_reregister), \
                    patch.dict(os.environ, {"INTELLIJ_PROJECT_REAPER_STATE_FILE": str(state), "INTELLIJ_PROJECT_REAPER_CAP": "0", "INTELLIJ_PROJECT_REAPER_IDLE_SECONDS": "0"}, clear=True), \
                    patch("sys.stdout", io.StringIO()):
                self.assertEqual(0, reaper.main_with_environment(["cleanup"]))
            projects = reaper.load_registry(state)[0]["projects"]
            self.assertEqual(refreshed_at, projects[0]["registeredAt"])

    def test_dry_run_never_closes_and_202_requires_disappearance(self):
        calls = []
        runtime = {"schemaVersion": 1, "port": 1234, "token": "t", "pid": os.getpid(), "processStartInstant": reaper.process_start(os.getpid())}
        def request(method, path, runtime, root=None):
            calls.append((method, path)); return (202, {}) if method == "POST" else (200, {"projects": []})
        self.assertTrue(reaper.close_verified("/r", runtime, request, dry_run=True))
        self.assertEqual([], calls)
        self.assertTrue(reaper.close_verified("/r", runtime, request, dry_run=False, sleep=lambda _: None))
        self.assertEqual("POST", calls[0][0])

    def test_plugin_string_payload_and_refusals_are_fail_closed(self):
        runtime = {"port": 1, "token": "t"}
        replies = iter([(202, {}), (200, {"projects": ["/other"]})])
        self.assertTrue(reaper.close_verified("/target", runtime, lambda *args: next(replies), sleep=lambda _: None))
        for response in [lambda *args: (_ for _ in ()).throw(TimeoutError()), lambda *args: (409, {}), lambda *args: (200, {"projects": [None]})]:
            self.assertFalse(reaper.close_verified("/target", runtime, response, sleep=lambda _: None))

    def test_runtime_contract_tolerance_and_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.json"
            instant = "2026-07-12T12:00:00.456Z"
            path.write_text(json.dumps({"schemaVersion": 1, "port": 1, "token": "t", "pid": 42, "processStartInstant": instant})); path.chmod(0o600)
            with patch.object(reaper, "process_start", return_value="2026-07-12T12:00:00Z"):
                self.assertIsNotNone(reaper.runtime_client(path))
            for field, value in [("schemaVersion", 2), ("token", ""), ("pid", "bad"), ("processStartInstant", "bad")]:
                payload = json.loads(path.read_text()); payload[field] = value; path.write_text(json.dumps(payload))
                with patch.object(reaper, "process_start", return_value="2026-07-12T12:00:00Z"):
                    self.assertIsNone(reaper.runtime_client(path))
            path.chmod(0o644)
            self.assertIsNone(reaper.runtime_client(path))

    def test_latest_state_removal_preserves_concurrent_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "managed.json"
            reaper.register_root("/first", state, now="1970-01-01T00:00:00Z")
            reaper.register_root("/second", state, now="1970-01-01T00:00:00Z")
            self.assertTrue(reaper.remove_root("/first", state))
            self.assertEqual([reaper.canonical("/second")], [p["root"] for p in reaper.load_registry(state)[0]["projects"]])

if __name__ == "__main__": unittest.main()
