from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
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
os.environ.setdefault(
    "WORKSPACE_HARBOR_OPENER_QUEUE_COMMAND",
    str(ROOT / "bin/workspace_harbor_opener_queue.py"),
)


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
            service_ready = project / "service-ready"

            serena_codex = bin_dir / "serena-codex"
            serena_codex.write_text(
                "#!/bin/sh\n"
                f"count=$(cat '{state_file}' 2>/dev/null || echo 0)\n"
                "count=$((count + 1))\n"
                f"printf '%s' \"$count\" > '{state_file}'\n"
                f"[ -f '{service_ready}' ]\n",
                encoding="utf-8",
            )
            serena_codex.chmod(0o755)

            fake_open = root / "open"
            fake_open.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$@\" > '{open_log}'\n"
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
            self.assertIn("readiness-deadline", result.stderr)

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
            state_dir.chmod(0o700)
            lock_dir.chmod(0o700)
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
            owner.chmod(0o600)
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
            (root / "state").chmod(0o700)
            lock_dir.chmod(0o700)
            owner = lock_dir / "owner"
            owner.write_text("not a valid owner record\n", encoding="utf-8")
            owner.chmod(0o600)
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
            self.assertIn("legacy-state", result.stderr)
            self.assertTrue(owner.exists())
            self.assertEqual("not a valid owner record\n", owner.read_text())

    def test_live_lock_owner_is_not_removed_when_waiter_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            fake_home = root / "home"
            bin_dir = fake_home / ".codex/bin"
            bin_dir.mkdir(parents=True)
            serena_codex = bin_dir / "serena-codex"
            serena_codex.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            serena_codex.chmod(0o755)
            state_dir = root / "state"
            lock_dir = state_dir / "opener.lock"
            lock_dir.mkdir(parents=True)
            state_dir.chmod(0o700)
            lock_dir.chmod(0o700)
            live_owner = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(2)",
                    "open-codex-project-in-intellij",
                ]
            )
            started = subprocess.run(
                ["ps", "-p", str(live_owner.pid), "-o", "lstart="],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            owner = lock_dir / "owner"
            owner.write_text(
                f"pid={live_owner.pid}\n"
                f"process_started={started}\n"
                f"project_root={project.resolve()}\n",
                encoding="utf-8",
            )
            owner.chmod(0o600)
            owner_before = owner.read_text(encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(fake_home),
                    "INTELLIJ_OPENER_STATE_DIR": str(state_dir),
                    "INTELLIJ_OPENER_OPERATION_TIMEOUT": "0.05",
                    "INTELLIJ_OPENER_QUEUE_TIMEOUT": "0.05",
                    "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                    "INTELLIJ_SERENA_READY_TIMEOUT": "0.05",
                }
            )
            try:
                result = subprocess.run(
                    [str(HELPER), str(project)], capture_output=True, text=True,
                    timeout=5, env=environment, check=False,
                )

                self.assertEqual(1, result.returncode)
                self.assertIn("operation-deadline", result.stderr)
                self.assertEqual(owner_before, owner.read_text(encoding="utf-8"))
            finally:
                live_owner.terminate()
                live_owner.wait(timeout=2)

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
            reaper_log = root / "reaper-log"
            service_ready = project / "service-ready"

            serena_codex = bin_dir / "serena-codex"
            serena_codex.write_text(
                "#!/bin/sh\n"
                f"[ -f '{service_ready}' ]\n",
                encoding="utf-8",
            )
            serena_codex.chmod(0o755)
            reaper = bin_dir / "intellij-project-reaper"
            reaper.write_text(
                "#!/bin/sh\n"
                "[ \"$1\" = is-open ] && exit 0\n"
                f"printf '%s\\n' \"$1\" >> '{reaper_log}'\n",
                encoding="utf-8",
            )
            reaper.chmod(0o755)
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
                "joined existing worktree open" in first_stdout + second_stdout
            )
            self.assertEqual(1, reaper_log.read_text().splitlines().count("register"))
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
            first_ready = first_project / "allow-ready"
            second_ready = second_project / "allow-ready"

            serena_codex = bin_dir / "serena-codex"
            serena_codex.write_text(
                "#!/bin/sh\n"
                "[ -f \"$2/allow-ready\" ]\n",
                encoding="utf-8",
            )
            serena_codex.chmod(0o755)
            fake_open = root / "open"
            fake_open.write_text(
                "#!/bin/sh\n"
                f"if ! mkdir '{active_dir}' 2>/dev/null; then printf 'overlap\\n' >> '{overlap_log}'; fi\n"
                "for project; do :; done\n"
                f"printf '%s\\n' \"$project\" >> '{open_log}'\n"
                "touch \"$project/launched\"\n"
                "sleep 0.1\n"
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
                    "INTELLIJ_SERENA_READY_TIMEOUT": "3",
                }
            )

            first = subprocess.Popen(
                [str(HELPER), str(first_project)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=environment,
            )
            deadline = time.time() + 2
            while time.time() < deadline and not (first_project / "launched").exists():
                time.sleep(0.01)
            self.assertTrue((first_project / "launched").exists())
            second = subprocess.Popen(
                [str(HELPER), str(second_project)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=environment,
            )
            deadline = time.time() + 1
            while time.time() < deadline and not (second_project / "launched").exists():
                time.sleep(0.01)
            second_launched_while_first_waited = (second_project / "launched").exists()
            first_ready.touch()
            second_ready.touch()
            first_stdout, first_stderr = first.communicate(timeout=5)
            second_stdout, second_stderr = second.communicate(timeout=5)

            self.assertEqual([0, 0], sorted([first.returncode, second.returncode]), first_stderr + second_stderr)
            self.assertTrue(
                second_launched_while_first_waited,
                "second launch was blocked by first root readiness",
            )
            self.assertFalse(overlap_log.exists())
            self.assertEqual(2, len(open_log.read_text().splitlines()))

    def test_three_different_projects_launch_in_fifo_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            projects = [root / f"project-{index}" for index in range(3)]
            for project in projects:
                project.mkdir()
            app = root / "IntelliJ IDEA.app"
            app.mkdir()
            home = root / "home"
            bin_dir = home / ".codex/bin"
            bin_dir.mkdir(parents=True)
            state_dir = root / "state"
            trust_started = root / "trust-started"
            release_first = root / "release-first"
            trust_log = root / "trust-log"
            open_log = root / "open-log"

            (bin_dir / "serena-codex").write_text(
                "#!/bin/sh\n[ -f \"$2/ready\" ]\n", encoding="utf-8"
            )
            (bin_dir / "serena-codex").chmod(0o755)
            (bin_dir / "intellij-project-reaper").write_text(
                "#!/bin/sh\nexit 0\n", encoding="utf-8"
            )
            (bin_dir / "intellij-project-reaper").chmod(0o755)
            trust = root / "trust"
            trust.write_text(
                "#!/bin/sh\n"
                f"if [ \"$2\" = '{projects[0].resolve()}' ]; then\n"
                f"  touch '{trust_started}'\n"
                f"  while [ ! -f '{release_first}' ]; do sleep 0.01; done\n"
                "fi\n"
                f"printf '%s\\n' \"$2\" >> '{trust_log}'\n",
                encoding="utf-8",
            )
            trust.chmod(0o755)
            opener = root / "open"
            opener.write_text(
                "#!/bin/sh\n"
                "for project; do :; done\n"
                f"printf '%s\\n' \"$project\" >> '{open_log}'\n"
                "touch \"$project/ready\"\n",
                encoding="utf-8",
            )
            opener.chmod(0o755)
            environment = os.environ | {
                "HOME": str(home),
                "INTELLIJ_APP_PATH": str(app),
                "INTELLIJ_OPEN_COMMAND": str(opener),
                "INTELLIJ_PROJECT_TRUST_COMMAND": str(trust),
                "INTELLIJ_OPENER_STATE_DIR": str(state_dir),
                "INTELLIJ_OPENER_LOCK_TIMEOUT": "0.01",
                "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                "INTELLIJ_SERENA_READY_TIMEOUT": "2",
            }

            processes = [
                subprocess.Popen(
                    [str(HELPER), str(project)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                )
                for project in projects[:1]
            ]
            deadline = time.time() + 2
            while time.time() < deadline and not trust_started.exists():
                time.sleep(0.01)
            self.assertTrue(trust_started.exists())

            for project in projects[1:]:
                processes.append(
                    subprocess.Popen(
                        [str(HELPER), str(project)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=environment,
                    )
                )
                deadline = time.time() + 2
                while time.time() < deadline:
                    queue_path = state_dir / "launch-queue.json"
                    if queue_path.exists() and str(project.resolve()) in queue_path.read_text():
                        break
                    time.sleep(0.01)
                self.assertIn(str(project.resolve()), queue_path.read_text())

            release_first.touch()
            outputs = [process.communicate(timeout=5) for process in processes]

            self.assertEqual([0, 0, 0], [process.returncode for process in processes])
            self.assertEqual(
                [str(project.resolve()) for project in projects],
                trust_log.read_text().splitlines(),
            )
            self.assertEqual(
                [str(project.resolve()) for project in projects],
                open_log.read_text().splitlines(),
            )
            self.assertEqual("", "".join(stderr for _, stderr in outputs))

    def test_signal_cleanup_removes_only_the_callers_coordination_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            app = root / "IntelliJ IDEA.app"
            app.mkdir()
            home = root / "home"
            bin_dir = home / ".codex/bin"
            bin_dir.mkdir(parents=True)
            state_dir = root / "state"
            launched = project / "launched"
            (bin_dir / "serena-codex").write_text(
                "#!/bin/sh\nexit 1\n", encoding="utf-8"
            )
            (bin_dir / "serena-codex").chmod(0o755)
            (bin_dir / "intellij-project-reaper").write_text(
                "#!/bin/sh\nexit 0\n", encoding="utf-8"
            )
            (bin_dir / "intellij-project-reaper").chmod(0o755)
            opener = root / "open"
            opener.write_text(
                f"#!/bin/sh\ntouch '{launched}'\n", encoding="utf-8"
            )
            opener.chmod(0o755)
            environment = os.environ | {
                "HOME": str(home),
                "INTELLIJ_APP_PATH": str(app),
                "INTELLIJ_OPEN_COMMAND": str(opener),
                "INTELLIJ_OPENER_STATE_DIR": str(state_dir),
                "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                "INTELLIJ_SERENA_READY_TIMEOUT": "5",
            }
            process = subprocess.Popen(
                [str(HELPER), str(project)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            deadline = time.time() + 2
            while time.time() < deadline and not launched.exists():
                time.sleep(0.01)
            self.assertTrue(launched.exists())

            process.terminate()
            process.communicate(timeout=3)

            queue_state = json.loads(
                (state_dir / "launch-queue.json").read_text(encoding="utf-8")
            )
            self.assertIsNone(queue_state["owner"])
            self.assertEqual([], queue_state["waiting"])
            self.assertEqual([], list((state_dir / "operations").glob("*.json")))

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

    def test_hung_trust_is_bounded_and_releases_launch_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            home = root / "home"
            bin_dir = home / ".codex/bin"
            bin_dir.mkdir(parents=True)
            app = root / "IntelliJ IDEA.app"
            app.mkdir()
            leaked = root / "trust-leaked"
            open_log = root / "open-log"
            state_dir = root / "state"
            (bin_dir / "serena-codex").write_text("#!/bin/sh\nexit 1\n")
            (bin_dir / "serena-codex").chmod(0o755)
            trust = root / "trust"
            trust.write_text(
                f"#!/bin/sh\nsleep 0.5\ntouch '{leaked}'\n", encoding="utf-8"
            )
            trust.chmod(0o755)
            opener = root / "open"
            opener.write_text(
                f"#!/bin/sh\nprintf 'open\n' > '{open_log}'\n", encoding="utf-8"
            )
            opener.chmod(0o755)

            result = subprocess.run(
                [str(HELPER), str(project)],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                env=os.environ
                | {
                    "HOME": str(home),
                    "INTELLIJ_APP_PATH": str(app),
                    "INTELLIJ_OPEN_COMMAND": str(opener),
                    "INTELLIJ_PROJECT_TRUST_COMMAND": str(trust),
                    "INTELLIJ_OPENER_STATE_DIR": str(state_dir),
                    "INTELLIJ_OPENER_LAUNCH_COMMAND_TIMEOUT": "0.1",
                    "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                    "INTELLIJ_SERENA_READY_TIMEOUT": "1",
                },
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("trust-failed", result.stdout)
            self.assertFalse(open_log.exists())
            time.sleep(0.6)
            self.assertFalse(leaked.exists())
            queue_state = json.loads(
                (state_dir / "launch-queue.json").read_text(encoding="utf-8")
            )
            self.assertIsNone(queue_state["owner"])
            self.assertEqual([], queue_state["waiting"])

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

    def test_every_bootstrap_degradation_keeps_exact_root_open_available(self) -> None:
        cases = (
            (
                1,
                {
                    "status": "failed",
                    "failure_kind": "permission-denied",
                    "operation": "bootstrap-state",
                    "error": "[Errno 1] Operation not permitted " + ("x" * 2000),
                },
                "permission-denied",
            ),
            (
                2,
                {"status": "invalid", "error": "invalid fixture configuration"},
                "validation failed",
            ),
            (3, {"status": "needs-decision"}, "needs a repository decision"),
            (9, None, "result=unreadable"),
            (None, None, "dependency bootstrap command unavailable"),
        )
        for bootstrap_exit, payload, expected_message in cases:
            with self.subTest(bootstrap_exit=bootstrap_exit), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                project = root / "project"
                project.mkdir()
                home = root / "home"
                bin_dir = home / ".codex/bin"
                bin_dir.mkdir(parents=True)
                app = root / "IntelliJ IDEA.app"
                app.mkdir()
                ready = project / "ready"
                log = root / "log"
                bootstrap_command = root / "bootstrap"
                if bootstrap_exit is not None:
                    rendered = json.dumps(payload) if payload is not None else "not-json"
                    bootstrap_command.write_text(
                        "#!/bin/sh\n"
                        f"printf '%s\\n' '{rendered}'\n"
                        f"exit {bootstrap_exit}\n",
                        encoding="utf-8",
                    )
                    bootstrap_command.chmod(0o755)
                (bin_dir / "serena-codex").write_text(
                    f"#!/bin/sh\n[ -f '{ready}' ]\n",
                    encoding="utf-8",
                )
                (bin_dir / "serena-codex").chmod(0o755)
                (bin_dir / "intellij-project-reaper").write_text(
                    "#!/bin/sh\nexit 0\n",
                    encoding="utf-8",
                )
                (bin_dir / "intellij-project-reaper").chmod(0o755)
                opener = root / "open"
                opener.write_text(
                    f"#!/bin/sh\nprintf 'open\\n' >> '{log}'\ntouch '{ready}'\n",
                    encoding="utf-8",
                )
                opener.chmod(0o755)
                result = subprocess.run(
                    [str(HELPER), str(project)],
                    capture_output=True,
                    text=True,
                    check=False,
                    env=os.environ
                    | {
                        "HOME": str(home),
                        "INTELLIJ_APP_PATH": str(app),
                        "INTELLIJ_OPEN_COMMAND": str(opener),
                        "WORKSPACE_HARBOR_BOOTSTRAP_COMMAND": str(bootstrap_command),
                        "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                        "INTELLIJ_SERENA_READY_TIMEOUT": "1",
                    },
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("open", log.read_text(encoding="utf-8").splitlines())
                self.assertIn(expected_message, result.stderr)
                if bootstrap_exit == 1:
                    self.assertLessEqual(len(result.stderr.encode("utf-8")), 1024)

    def test_hung_bootstrap_is_bounded_and_does_not_block_intellij(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            home = root / "home"
            bin_dir = home / ".codex/bin"
            bin_dir.mkdir(parents=True)
            app = root / "IntelliJ IDEA.app"
            app.mkdir()
            ready = project / "ready"
            open_log = root / "open-log"
            leaked = root / "bootstrap-leaked"

            bootstrap_command = root / "bootstrap"
            bootstrap_command.write_text(
                f"#!/bin/sh\nsleep 0.5\ntouch '{leaked}'\n", encoding="utf-8"
            )
            bootstrap_command.chmod(0o755)
            (bin_dir / "serena-codex").write_text(
                f"#!/bin/sh\n[ -f '{ready}' ]\n", encoding="utf-8"
            )
            (bin_dir / "serena-codex").chmod(0o755)
            (bin_dir / "intellij-project-reaper").write_text(
                "#!/bin/sh\nexit 0\n", encoding="utf-8"
            )
            (bin_dir / "intellij-project-reaper").chmod(0o755)
            opener = root / "open"
            opener.write_text(
                f"#!/bin/sh\nprintf 'open\\n' > '{open_log}'\ntouch '{ready}'\n",
                encoding="utf-8",
            )
            opener.chmod(0o755)

            try:
                result = subprocess.run(
                    [str(HELPER), str(project)],
                    capture_output=True,
                    text=True,
                    timeout=4,
                    check=False,
                    env=os.environ
                    | {
                        "HOME": str(home),
                        "INTELLIJ_APP_PATH": str(app),
                        "INTELLIJ_OPEN_COMMAND": str(opener),
                        "WORKSPACE_HARBOR_BOOTSTRAP_COMMAND": str(bootstrap_command),
                        "WORKSPACE_HARBOR_BOOTSTRAP_OPENER_TIMEOUT_SECONDS": "0.1",
                        "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                        "INTELLIJ_SERENA_READY_TIMEOUT": "1",
                    },
                )
            except subprocess.TimeoutExpired:
                self.fail("hung dependency bootstrap blocked IntelliJ project opening")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("open\n", open_log.read_text(encoding="utf-8"))
            self.assertIn("timed out", result.stderr)
            time.sleep(0.6)
            self.assertFalse(leaked.exists(), "timed-out bootstrap child escaped cleanup")

    def test_bootstrap_capture_does_not_use_unbounded_temporary_storage(self) -> None:
        script = HELPER.read_text(encoding="utf-8")

        self.assertNotIn("TemporaryFile", script)
        self.assertIn("BOOTSTRAP_OUTPUT_LIMIT_BYTES", script)

    def test_successful_bootstrap_cannot_leave_a_pipe_holding_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            home = root / "home"
            bin_dir = home / ".codex/bin"
            bin_dir.mkdir(parents=True)
            app = root / "IntelliJ IDEA.app"
            app.mkdir()
            ready = project / "ready"
            leaked = root / "bootstrap-leaked"

            bootstrap_command = root / "bootstrap"
            bootstrap_command.write_text(
                f"#!/bin/sh\n(sleep 0.3; touch '{leaked}') &\nexit 0\n",
                encoding="utf-8",
            )
            bootstrap_command.chmod(0o755)
            (bin_dir / "serena-codex").write_text(
                f"#!/bin/sh\n[ -f '{ready}' ]\n", encoding="utf-8"
            )
            (bin_dir / "serena-codex").chmod(0o755)
            (bin_dir / "intellij-project-reaper").write_text(
                "#!/bin/sh\nexit 0\n", encoding="utf-8"
            )
            (bin_dir / "intellij-project-reaper").chmod(0o755)
            opener = root / "open"
            opener.write_text(
                f"#!/bin/sh\ntouch '{ready}'\n", encoding="utf-8"
            )
            opener.chmod(0o755)

            try:
                result = subprocess.run(
                    [str(HELPER), str(project)],
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                    check=False,
                    env=os.environ
                    | {
                        "HOME": str(home),
                        "INTELLIJ_APP_PATH": str(app),
                        "INTELLIJ_OPEN_COMMAND": str(opener),
                        "WORKSPACE_HARBOR_BOOTSTRAP_COMMAND": str(bootstrap_command),
                        "INTELLIJ_SERENA_READY_INTERVAL": "0.01",
                        "INTELLIJ_SERENA_READY_TIMEOUT": "1",
                    },
                )
            except subprocess.TimeoutExpired:
                self.fail("successful bootstrap descendant blocked opener completion")

            self.assertEqual(0, result.returncode, result.stderr)
            time.sleep(0.4)
            self.assertFalse(leaked.exists(), "bootstrap descendant escaped cleanup")


if __name__ == "__main__":
    unittest.main()
