from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


TEST_FILE = Path(__file__).resolve()
ROOT = TEST_FILE.parent.parent if TEST_FILE.parent.name == "tests" else TEST_FILE.parents[2]
HELPER = ROOT / "bin/open-codex-project-in-intellij"
DEFAULT_TRUST_COMMAND = Path(tempfile.gettempdir()) / "open-codex-project-in-intellij-test-trust"
DEFAULT_TRUST_COMMAND.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
DEFAULT_TRUST_COMMAND.chmod(0o755)
os.environ.setdefault("INTELLIJ_PROJECT_TRUST_COMMAND", str(DEFAULT_TRUST_COMMAND))
DEFAULT_MODEL_READY_COMMAND = Path(tempfile.gettempdir()) / "open-codex-project-in-intellij-test-model-ready"
DEFAULT_MODEL_READY_COMMAND.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
DEFAULT_MODEL_READY_COMMAND.chmod(0o755)
os.environ.setdefault("INTELLIJ_MODEL_READY_COMMAND", str(DEFAULT_MODEL_READY_COMMAND))
DEFAULT_BOOTSTRAP_COMMAND = Path(tempfile.gettempdir()) / "open-codex-project-in-intellij-test-bootstrap"
DEFAULT_BOOTSTRAP_COMMAND.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
DEFAULT_BOOTSTRAP_COMMAND.chmod(0o755)
os.environ.setdefault("WORKSPACE_HARBOR_BOOTSTRAP_COMMAND", str(DEFAULT_BOOTSTRAP_COMMAND))


class OpenCodexProjectInIntellijTests(unittest.TestCase):
    def test_default_launcher_is_only_the_toolbox_intellij_app(self) -> None:
        script = HELPER.read_text(encoding="utf-8")
        self.assertIn('$HOME/Applications/IntelliJ IDEA.app', script)
        self.assertNotIn("command -v charm", script)
        self.assertNotIn("command -v intellij", script)
        self.assertNotIn(":-/Applications/IntelliJ IDEA.app", script)
        self.assertIn('"$model_ready_command" model-ready "$dir"', script)

    def test_registers_only_after_new_open_is_ready_and_fails_actionably(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory); project = root / "project"; project.mkdir()
            app = root / "IntelliJ IDEA.app"; app.mkdir(); home = root / "home"; bin_dir = home / ".codex/bin"; bin_dir.mkdir(parents=True)
            log = root / "log"; ready = project / "ready"
            (bin_dir / "serena-codex").write_text(f"#!/bin/sh\n[ -f '{ready}' ]\n"); (bin_dir / "serena-codex").chmod(0o755)
            (bin_dir / "intellij-project-reaper").write_text(f"#!/bin/sh\nprintf '%s %s\\n' \"$1\" \"$2\" >> '{log}'\n"); (bin_dir / "intellij-project-reaper").chmod(0o755)
            opener = root / "open"; opener.write_text(f"#!/bin/sh\ntouch '{ready}'\n"); opener.chmod(0o755)
            env = os.environ | {"HOME": str(home), "INTELLIJ_APP_PATH": str(app), "INTELLIJ_OPEN_COMMAND": str(opener), "INTELLIJ_SERENA_READY_INTERVAL": "0.01", "INTELLIJ_SERENA_READY_TIMEOUT": "1"}
            result = subprocess.run([str(HELPER), str(project)], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(0, result.returncode, result.stderr); self.assertEqual(f"is-open {project.resolve()}\nregister {project.resolve()}\n", log.read_text())

    def test_registration_waits_for_native_model_after_serena_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory); project = root / "project"; project.mkdir()
            app = root / "IntelliJ IDEA.app"; app.mkdir(); home = root / "home"
            bin_dir = home / ".codex/bin"; bin_dir.mkdir(parents=True)
            count = root / "model-count"
            (bin_dir / "serena-codex").write_text("#!/bin/sh\nexit 0\n"); (bin_dir / "serena-codex").chmod(0o755)
            (bin_dir / "intellij-project-reaper").write_text("#!/bin/sh\nexit 0\n"); (bin_dir / "intellij-project-reaper").chmod(0o755)
            model = root / "model-ready"
            model.write_text(
                "#!/bin/sh\n"
                f"n=$(cat '{count}' 2>/dev/null || echo 0)\n"
                "n=$((n + 1))\n"
                f"printf '%s' \"$n\" > '{count}'\n"
                "[ \"$n\" -ge 3 ]\n"
            ); model.chmod(0o755)
            opener = root / "open"; opener.write_text("#!/bin/sh\nexit 0\n"); opener.chmod(0o755)
            env = os.environ | {"HOME": str(home), "INTELLIJ_APP_PATH": str(app),
                "INTELLIJ_OPEN_COMMAND": str(opener), "INTELLIJ_MODEL_READY_COMMAND": str(model),
                "INTELLIJ_SERENA_READY_INTERVAL": "0.01", "INTELLIJ_SERENA_READY_TIMEOUT": "1"}
            result = subprocess.run([str(HELPER), str(project)], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertGreaterEqual(int(count.read_text()), 3)

    def test_initially_ready_root_only_touches_and_unmanaged_touch_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory); project = root / "project"; project.mkdir(); home = root / "home"; bin_dir = home / ".codex/bin"; bin_dir.mkdir(parents=True); log = root / "log"
            (bin_dir / "serena-codex").write_text("#!/bin/sh\nexit 0\n"); (bin_dir / "serena-codex").chmod(0o755)
            (bin_dir / "intellij-project-reaper").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> '{log}'\nexit 1\n"); (bin_dir / "intellij-project-reaper").chmod(0o755)
            result = subprocess.run([str(HELPER), str(project)], capture_output=True, text=True, env=os.environ | {"HOME": str(home)}, check=False)
            self.assertEqual(0, result.returncode, result.stderr); self.assertEqual(f"is-open\n", log.read_text())
    def test_reuses_intellij_application_and_waits_for_exact_serena_service(self) -> None:
        script = HELPER.read_text(encoding="utf-8")
        self.assertIn("INTELLIJ_APP_PATH", script, "helper lacks an injectable app path")
        self.assertIn("INTELLIJ_OPEN_COMMAND", script, "helper lacks an injectable open command")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            fake_app = root / "IntelliJ IDEA.app"
            fake_app.mkdir()
            fake_home = root / "home"
            bin_dir = fake_home / ".codex/bin"
            bin_dir.mkdir(parents=True)
            state_file = root / "status-count"
            open_log = root / "open-args"

            serena_codex = bin_dir / "serena-codex"
            serena_codex.write_text(
                "#!/bin/sh\n"
                f"count=$(cat '{state_file}' 2>/dev/null || echo 0)\n"
                "count=$((count + 1))\n"
                f"printf '%s' \"$count\" > '{state_file}'\n"
                "[ \"$count\" -ge 3 ]\n",
                encoding="utf-8",
            )
            serena_codex.chmod(0o755)

            fake_open = root / "open"
            fake_open.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$@\" > '{open_log}'\n",
                encoding="utf-8",
            )
            fake_open.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(fake_home),
                    "INTELLIJ_APP_PATH": str(fake_app),
                    "INTELLIJ_OPEN_COMMAND": str(fake_open),
                    "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                    "INTELLIJ_SERENA_READY_TIMEOUT": "2",
                }
            )
            result = subprocess.run(
                [str(HELPER), str(project)],
                capture_output=True,
                text=True,
                timeout=5,
                env=environment,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("Serena service and native project model ready", result.stdout)
            self.assertGreaterEqual(int(state_file.read_text()), 3)
            self.assertEqual(
                ["-a", str(fake_app), str(project.resolve())],
                open_log.read_text().splitlines(),
            )

    def test_reaper_absent_or_unknown_blocks_registration_without_registering(self) -> None:
        for confirmation, expected in [("exit 1", "not present"), ("exit 2", "unavailable")]:
            with self.subTest(confirmation=confirmation), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory); project = root / "project"; project.mkdir(); app = root / "IntelliJ IDEA.app"; app.mkdir(); home = root / "home"; bin_dir = home / ".codex/bin"; bin_dir.mkdir(parents=True); log = root / "log"; ready = project / "ready"
                (bin_dir / "serena-codex").write_text(f"#!/bin/sh\n[ -f '{ready}' ]\n"); (bin_dir / "serena-codex").chmod(0o755)
                (bin_dir / "intellij-project-reaper").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> '{log}'\n[ \"$1\" = is-open ] && {confirmation}\nexit 0\n"); (bin_dir / "intellij-project-reaper").chmod(0o755)
                opener = root / "open"; opener.write_text(f"#!/bin/sh\ntouch '{ready}'\n"); opener.chmod(0o755)
                result = subprocess.run([str(HELPER), str(project)], capture_output=True, text=True, env=os.environ | {"HOME": str(home), "INTELLIJ_APP_PATH": str(app), "INTELLIJ_OPEN_COMMAND": str(opener), "INTELLIJ_SERENA_READY_INTERVAL": "0.01", "INTELLIJ_SERENA_READY_TIMEOUT": "1"}, check=False)
                self.assertEqual(1, result.returncode); self.assertIn(expected, result.stderr); self.assertEqual("is-open\n", log.read_text())

    def test_launcher_never_forces_a_new_macos_application_instance(self) -> None:
        script = HELPER.read_text(encoding="utf-8")

        self.assertNotIn('"$intellij_open_command" -n', script)

    def test_times_out_when_serena_service_never_becomes_ready(self) -> None:
        script = HELPER.read_text(encoding="utf-8")
        self.assertIn("INTELLIJ_APP_PATH", script, "helper lacks an injectable app path")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            fake_app = root / "IntelliJ IDEA.app"
            fake_app.mkdir()
            fake_home = root / "home"
            bin_dir = fake_home / ".codex/bin"
            bin_dir.mkdir(parents=True)

            serena_codex = bin_dir / "serena-codex"
            serena_codex.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            serena_codex.chmod(0o755)
            fake_open = root / "open"
            fake_open.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_open.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(fake_home),
                    "INTELLIJ_APP_PATH": str(fake_app),
                    "INTELLIJ_OPEN_COMMAND": str(fake_open),
                    "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                    "INTELLIJ_SERENA_READY_TIMEOUT": "0.05",
                }
            )
            result = subprocess.run(
                [str(HELPER), str(project)],
                capture_output=True,
                text=True,
                timeout=5,
                env=environment,
                check=False,
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("timed out waiting for Serena service", result.stderr)

    def test_reclaims_lock_owned_by_a_dead_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            fake_app = root / "IntelliJ IDEA.app"
            fake_app.mkdir()
            fake_home = root / "home"
            bin_dir = fake_home / ".codex/bin"
            bin_dir.mkdir(parents=True)
            state_dir = root / "state"
            lock_dir = state_dir / "opener.lock"
            lock_dir.mkdir(parents=True)
            owner = lock_dir / "owner"
            dead_helper = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
            dead_helper.wait(timeout=5)
            with self.assertRaises(ProcessLookupError):
                os.kill(dead_helper.pid, 0)
            owner.write_text(
                f"pid={dead_helper.pid}\n"
                "process_started=Thu Jan  1 00:00:00 1970\n"
                f"project_root={project.resolve()}\n",
                encoding="utf-8",
            )
            open_log = root / "open-log"
            service_ready = project / "service-ready"

            serena_codex = bin_dir / "serena-codex"
            serena_codex.write_text(
                "#!/bin/sh\n"
                f"[ -f '{service_ready}' ]\n",
                encoding="utf-8",
            )
            serena_codex.chmod(0o755)
            fake_open = root / "open"
            fake_open.write_text(
                "#!/bin/sh\n"
                f"printf 'open\\n' >> '{open_log}'\n"
                f"touch '{service_ready}'\n",
                encoding="utf-8",
            )
            fake_open.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(fake_home),
                    "INTELLIJ_APP_PATH": str(fake_app),
                    "INTELLIJ_OPEN_COMMAND": str(fake_open),
                    "INTELLIJ_OPENER_STATE_DIR": str(state_dir),
                    "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                    "INTELLIJ_SERENA_READY_TIMEOUT": "2",
                }
            )

            result = subprocess.run(
                [str(HELPER), str(project)], capture_output=True, text=True,
                timeout=5, env=environment, check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(1, len(open_log.read_text().splitlines()))
            self.assertFalse(lock_dir.exists())

    def test_malformed_lock_owner_fails_closed_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            lock_dir = root / "state/opener.lock"
            lock_dir.mkdir(parents=True)
            owner = lock_dir / "owner"
            owner.write_text("not a valid owner record\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "INTELLIJ_OPENER_STATE_DIR": str(root / "state"),
                    "INTELLIJ_OPENER_LOCK_TIMEOUT": "0.2",
                }
            )

            result = subprocess.run(
                [str(HELPER), str(project)], capture_output=True, text=True,
                timeout=5, env=environment, check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertIn("timed out waiting for opener lock", result.stderr)
            self.assertTrue(owner.exists())
            self.assertEqual("not a valid owner record\n", owner.read_text())

    def test_live_lock_owner_is_not_removed_when_waiter_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            fake_app = root / "IntelliJ IDEA.app"
            fake_app.mkdir()
            fake_home = root / "home"
            bin_dir = fake_home / ".codex/bin"
            bin_dir.mkdir(parents=True)
            probe_count = root / "probe-count"
            probe_blocked = root / "probe-blocked"
            open_log = root / "open-log"

            serena_codex = bin_dir / "serena-codex"
            serena_codex.write_text(
                "#!/bin/sh\n"
                f"count=$(cat '{probe_count}' 2>/dev/null || echo 0)\n"
                "count=$((count + 1))\n"
                f"printf '%s' \"$count\" > '{probe_count}'\n"
                "if [ \"$count\" -eq 2 ]; then\n"
                f"  touch '{probe_blocked}'\n"
                "  sleep 1\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            serena_codex.chmod(0o755)
            fake_open = root / "open"
            fake_open.write_text(
                "#!/bin/sh\n"
                f"printf 'open\\n' >> '{open_log}'\n",
                encoding="utf-8",
            )
            fake_open.chmod(0o755)
            state_dir = root / "state"
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(fake_home),
                    "INTELLIJ_APP_PATH": str(fake_app),
                    "INTELLIJ_OPEN_COMMAND": str(fake_open),
                    "INTELLIJ_OPENER_STATE_DIR": str(state_dir),
                    "INTELLIJ_OPENER_LOCK_TIMEOUT": "0.2",
                    "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                    "INTELLIJ_SERENA_READY_TIMEOUT": "0.2",
                }
            )

            first = subprocess.Popen(
                [str(HELPER), str(project)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=environment,
            )
            for _ in range(50):
                if probe_blocked.exists():
                    break
                subprocess.run(["sleep", "0.01"], check=True)
            self.assertTrue(probe_blocked.exists(), "first helper did not reach readiness probe")
            owner = state_dir / "opener.lock/owner"
            owner_before = owner.read_text(encoding="utf-8")

            result = subprocess.run(
                [str(HELPER), str(project)], capture_output=True, text=True,
                timeout=5, env=environment, check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertIn("timed out waiting for opener lock", result.stderr)
            self.assertEqual(owner_before, owner.read_text(encoding="utf-8"))
            first_stdout, first_stderr = first.communicate(timeout=5)
            self.assertEqual(1, first.returncode, first_stdout + first_stderr)
            self.assertEqual(1, len(open_log.read_text().splitlines()))
            lock_dir = state_dir / "opener.lock"
            self.assertFalse(lock_dir.exists())

    def test_concurrent_requests_for_the_same_project_open_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            fake_app = root / "IntelliJ IDEA.app"
            fake_app.mkdir()
            fake_home = root / "home"
            bin_dir = fake_home / ".codex/bin"
            bin_dir.mkdir(parents=True)
            open_log = root / "open-log"
            service_ready = project / "service-ready"

            serena_codex = bin_dir / "serena-codex"
            serena_codex.write_text(
                "#!/bin/sh\n"
                f"[ -f '{service_ready}' ]\n",
                encoding="utf-8",
            )
            serena_codex.chmod(0o755)
            fake_open = root / "open"
            fake_open.write_text(
                "#!/bin/sh\n"
                "sleep 0.1\n"
                f"printf '%s\\n' \"$@\" | tail -n 1 >> '{open_log}'\n"
                f"touch '{service_ready}'\n",
                encoding="utf-8",
            )
            fake_open.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(fake_home),
                    "INTELLIJ_APP_PATH": str(fake_app),
                    "INTELLIJ_OPEN_COMMAND": str(fake_open),
                    "INTELLIJ_OPENER_STATE_DIR": str(root / "state"),
                    "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                    "INTELLIJ_SERENA_READY_TIMEOUT": "2",
                }
            )

            first = subprocess.Popen(
                [str(HELPER), str(project)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=environment,
            )
            second = subprocess.Popen(
                [str(HELPER), str(project)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=environment,
            )
            first_stdout, first_stderr = first.communicate(timeout=5)
            second_stdout, second_stderr = second.communicate(timeout=5)

            self.assertEqual([0, 0], sorted([first.returncode, second.returncode]))
            self.assertEqual([str(project.resolve())], open_log.read_text().splitlines())
            self.assertTrue(
                "already open in IntelliJ" in first_stdout + second_stdout
                or "Serena service ready" in first_stdout + second_stdout
            )
            self.assertEqual("", first_stderr + second_stderr)

    def test_concurrent_requests_for_different_projects_do_not_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_project = root / "first-project"
            second_project = root / "second-project"
            first_project.mkdir()
            second_project.mkdir()
            fake_app = root / "IntelliJ IDEA.app"
            fake_app.mkdir()
            fake_home = root / "home"
            bin_dir = fake_home / ".codex/bin"
            bin_dir.mkdir(parents=True)
            open_log = root / "open-log"
            overlap_log = root / "overlap-log"
            active_dir = root / "open-active"

            serena_codex = bin_dir / "serena-codex"
            serena_codex.write_text(
                "#!/bin/sh\n"
                "[ -f \"$2/service-ready\" ]\n",
                encoding="utf-8",
            )
            serena_codex.chmod(0o755)
            fake_open = root / "open"
            fake_open.write_text(
                "#!/bin/sh\n"
                f"if ! mkdir '{active_dir}' 2>/dev/null; then printf 'overlap\\n' >> '{overlap_log}'; fi\n"
                "for project; do :; done\n"
                f"printf '%s\\n' \"$project\" >> '{open_log}'\n"
                "sleep 0.1\n"
                "touch \"$project/service-ready\"\n"
                f"rmdir '{active_dir}' 2>/dev/null || true\n",
                encoding="utf-8",
            )
            fake_open.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(fake_home),
                    "INTELLIJ_APP_PATH": str(fake_app),
                    "INTELLIJ_OPEN_COMMAND": str(fake_open),
                    "INTELLIJ_OPENER_STATE_DIR": str(root / "state"),
                    "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                    "INTELLIJ_SERENA_READY_TIMEOUT": "2",
                }
            )

            first = subprocess.Popen(
                [str(HELPER), str(first_project)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=environment,
            )
            second = subprocess.Popen(
                [str(HELPER), str(second_project)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=environment,
            )
            first_stdout, first_stderr = first.communicate(timeout=5)
            second_stdout, second_stderr = second.communicate(timeout=5)

            self.assertEqual([0, 0], sorted([first.returncode, second.returncode]), first_stderr + second_stderr)
            self.assertFalse(overlap_log.exists())
            self.assertEqual(2, len(open_log.read_text().splitlines()))

    def test_new_open_trusts_exact_root_before_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory); project = root / "project"; project.mkdir()
            home = root / "home"; bin_dir = home / ".codex/bin"; bin_dir.mkdir(parents=True)
            app = root / "IntelliJ IDEA.app"; app.mkdir(); log = root / "log"
            (bin_dir / "serena-codex").write_text("#!/bin/sh\n[ -f \"$2/ready\" ]\n")
            (bin_dir / "serena-codex").chmod(0o755)
            (bin_dir / "intellij-project-reaper").write_text(f"#!/bin/sh\n[ \"$1\" = is-open ] && exit 0\nprintf 'register\\n' >> '{log}'\n")
            (bin_dir / "intellij-project-reaper").chmod(0o755)
            trust = root / "trust"; trust.write_text(f"#!/bin/sh\nprintf 'trust %s %s\\n' \"$1\" \"$2\" >> '{log}'\n"); trust.chmod(0o755)
            opener = root / "open"; opener.write_text(f"#!/bin/sh\nprintf 'open\\n' >> '{log}'\ntouch \"$3/ready\"\n"); opener.chmod(0o755)
            env = os.environ | {"HOME": str(home), "INTELLIJ_APP_PATH": str(app), "INTELLIJ_OPEN_COMMAND": str(opener), "INTELLIJ_PROJECT_TRUST_COMMAND": str(trust), "INTELLIJ_SERENA_READY_INTERVAL": "0.01", "INTELLIJ_SERENA_READY_TIMEOUT": "1"}
            result = subprocess.run([str(HELPER), str(project)], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([f"trust allow {project.resolve()}", "open", "register"], log.read_text().splitlines())

    def test_trust_failure_prevents_new_open_and_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory); project = root / "project"; project.mkdir()
            home = root / "home"; bin_dir = home / ".codex/bin"; bin_dir.mkdir(parents=True)
            app = root / "IntelliJ IDEA.app"; app.mkdir(); log = root / "log"
            (bin_dir / "serena-codex").write_text("#!/bin/sh\nexit 1\n"); (bin_dir / "serena-codex").chmod(0o755)
            (bin_dir / "intellij-project-reaper").write_text(f"#!/bin/sh\nprintf 'reaper\\n' >> '{log}'\n"); (bin_dir / "intellij-project-reaper").chmod(0o755)
            trust = root / "trust"; trust.write_text(f"#!/bin/sh\nprintf 'trust %s %s\\n' \"$1\" \"$2\" >> '{log}'\nexit 2\n"); trust.chmod(0o755)
            opener = root / "open"; opener.write_text(f"#!/bin/sh\nprintf 'open\\n' >> '{log}'\n"); opener.chmod(0o755)
            env = os.environ | {"HOME": str(home), "INTELLIJ_APP_PATH": str(app), "INTELLIJ_OPEN_COMMAND": str(opener), "INTELLIJ_PROJECT_TRUST_COMMAND": str(trust)}
            result = subprocess.run([str(HELPER), str(project)], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(1, result.returncode)
            self.assertEqual([f"trust allow {project.resolve()}"], log.read_text().splitlines())

    def test_already_open_root_does_not_require_trust_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory); project = root / "project"; project.mkdir()
            home = root / "home"; bin_dir = home / ".codex/bin"; bin_dir.mkdir(parents=True); log = root / "log"
            (bin_dir / "serena-codex").write_text("#!/bin/sh\nexit 0\n"); (bin_dir / "serena-codex").chmod(0o755)
            trust = root / "trust"; trust.write_text(f"#!/bin/sh\nprintf 'trust\\n' >> '{log}'\nexit 2\n"); trust.chmod(0o755)
            env = os.environ | {"HOME": str(home), "INTELLIJ_PROJECT_TRUST_COMMAND": str(trust)}
            result = subprocess.run([str(HELPER), str(project)], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(log.exists())

    def test_bootstrap_runs_before_already_open_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory); project = root / "project"; project.mkdir()
            home = root / "home"; bin_dir = home / ".codex/bin"; bin_dir.mkdir(parents=True); log = root / "log"
            bootstrap = root / "bootstrap"
            bootstrap.write_text(f"#!/bin/sh\nprintf 'bootstrap %s %s %s\\n' \"$1\" \"$2\" \"$3\" >> '{log}'\n", encoding="utf-8"); bootstrap.chmod(0o755)
            (bin_dir / "serena-codex").write_text(f"#!/bin/sh\nprintf 'service\\n' >> '{log}'\nexit 0\n", encoding="utf-8"); (bin_dir / "serena-codex").chmod(0o755)
            (bin_dir / "intellij-project-reaper").write_text(f"#!/bin/sh\nprintf 'reaper %s\\n' \"$1\" >> '{log}'\nexit 0\n", encoding="utf-8"); (bin_dir / "intellij-project-reaper").chmod(0o755)
            result = subprocess.run(
                [str(HELPER), str(project)], capture_output=True, text=True, check=False,
                env=os.environ | {"HOME": str(home), "WORKSPACE_HARBOR_BOOTSTRAP_COMMAND": str(bootstrap)},
            )
            self.assertEqual(0, result.returncode, result.stderr)
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(f"bootstrap run {project.resolve()} --json", lines[0])
            self.assertIn("service", lines[1:])

    def test_bootstrap_failure_is_degraded_but_invalid_configuration_blocks_open(self) -> None:
        for bootstrap_exit, expected_exit, should_open in ((1, 0, True), (3, 0, True), (2, 2, False)):
            with self.subTest(bootstrap_exit=bootstrap_exit), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory); project = root / "project"; project.mkdir()
                home = root / "home"; bin_dir = home / ".codex/bin"; bin_dir.mkdir(parents=True)
                app = root / "IntelliJ IDEA.app"; app.mkdir(); ready = project / "ready"; log = root / "log"
                bootstrap = root / "bootstrap"
                bootstrap.write_text(f"#!/bin/sh\nprintf 'bootstrap\\n' >> '{log}'\nexit {bootstrap_exit}\n", encoding="utf-8"); bootstrap.chmod(0o755)
                (bin_dir / "serena-codex").write_text(f"#!/bin/sh\n[ -f '{ready}' ]\n", encoding="utf-8"); (bin_dir / "serena-codex").chmod(0o755)
                (bin_dir / "intellij-project-reaper").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8"); (bin_dir / "intellij-project-reaper").chmod(0o755)
                opener = root / "open"; opener.write_text(f"#!/bin/sh\nprintf 'open\\n' >> '{log}'\ntouch '{ready}'\n", encoding="utf-8"); opener.chmod(0o755)
                result = subprocess.run(
                    [str(HELPER), str(project)], capture_output=True, text=True, check=False,
                    env=os.environ | {"HOME": str(home), "INTELLIJ_APP_PATH": str(app), "INTELLIJ_OPEN_COMMAND": str(opener), "WORKSPACE_HARBOR_BOOTSTRAP_COMMAND": str(bootstrap), "INTELLIJ_SERENA_READY_INTERVAL": "0.01", "INTELLIJ_SERENA_READY_TIMEOUT": "1"},
                )
                self.assertEqual(expected_exit, result.returncode, result.stderr)
                self.assertEqual(should_open, "open" in log.read_text(encoding="utf-8").splitlines())
                if bootstrap_exit in {1, 3}:
                    self.assertIn("dependency bootstrap", result.stderr)


if __name__ == "__main__":
    unittest.main()
