#!/usr/bin/env python3
"""Exercise checkout ownership and validation records through development commands."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.capture import read_lines
from support.checkpoints import FifoCheckpoint
from support.events import Events
from support.normalization import code
from support.requirements import PROCESS_EVENTS

ROOT = Path(__file__).resolve().parent.parent


class RCacheTests(unittest.TestCase):
    def test_restored_libraries_retain_only_complete_package_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            packages = cache / "renv"
            packages.mkdir()
            package = packages / "jsonlite"
            package.mkdir()
            libraries = cache / "ir/libraries"
            complete = libraries / "complete"
            incomplete = libraries / "incomplete"
            for library in (complete, incomplete):
                library.mkdir(parents=True)
                (library / "jsonlite").symlink_to(package, target_is_directory=True)
            (incomplete / "duckdb").symlink_to(packages / "missing")
            markers = cache / "ir/resolutions"
            markers.mkdir()
            (markers / "candidate").write_text(str(incomplete))

            result = subprocess.run(
                [sys.executable, ROOT / "scripts/prune-r-cache", cache],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("incomplete", result.stdout)
            self.assertTrue((complete / "jsonlite").is_dir())
            self.assertFalse(incomplete.exists())
            self.assertTrue(package.is_dir())
            self.assertTrue((markers / "candidate").is_file())

    def test_empty_cache_needs_no_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, ROOT / "scripts/prune-r-cache", temporary],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "checkout"
        self.root.mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "target").mkdir()
        for name in (
            "check",
            "test",
            "with-checkout",
            "checkout_workflow.py",
            "build_backend.py",
        ):
            source = ROOT / "scripts" / name
            shutil.copy2(source, self.root / "scripts" / name)
        self.environment = os.environ | {
            "XDG_CACHE_HOME": str(self.directory / "cache"),
            "PYTHONPATH": str(self.root / "scripts")
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        }
        self.environment.pop("MCP_CONSOLE_CHECKOUT_LOCKS", None)
        self.environment.pop("MCP_CONSOLE_VALIDATION_RUN", None)
        self.environment.pop("MCP_CONSOLE_VALIDATION_GROUP", None)
        self.write_script(
            "scripts/stage-sandbox-runner",
            # fmt: python
            """
            import os
            import sys
            from pathlib import Path

            if os.environ.get("HOLD_STAGE"):
                Path("target").rename("hidden-target")
                with open(os.environ["STAGE_READY"], "wb", buffering=0) as receipt:
                    receipt.write(b"1")
                assert sys.stdin.buffer.read(1) == b"1"
            print("stage complete", flush=True)
            """,
        )
        for name in ("scripts/check-core", "tests/install.py"):
            self.write_script(name, 'print("phase complete")')
        self.write_script(
            "maturin.py",
            # fmt: python
            """
            def build_wheel(*args):
                return "fixture.whl"


            build_editable = build_wheel
            build_sdist = build_wheel
            get_requires_for_build_editable = build_wheel
            get_requires_for_build_sdist = build_wheel
            get_requires_for_build_wheel = build_wheel
            prepare_metadata_for_build_editable = build_wheel
            prepare_metadata_for_build_wheel = build_wheel
            """,
        )
        self.write_script(
            "scripts/test",
            # fmt: python
            """
            print("transcripts complete")
            """,
        )
        subprocess.run(["git", "init", "-q", self.root], check=True)
        # Fixture copies must not race Git's detached maintenance process.
        subprocess.run(
            ["git", "config", "maintenance.auto", "false"], cwd=self.root, check=True
        )
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.org",
                "commit",
                "-qm",
                "Fixture",
            ],
            cwd=self.root,
            check=True,
        )

    def write_script(self, name: str, source: str) -> None:
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n" + code(source))
        path.chmod(0o755)

    def run_command(
        self, *command: str, root: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=root or self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def start_check(self, root: Path | None = None) -> subprocess.Popen[str]:
        root = root or self.root
        ready = FifoCheckpoint.create(root / "stage-ready")
        self.addCleanup(ready.close)
        process = subprocess.Popen(
            [root / "scripts/check"],
            cwd=root,
            env=self.environment | {"HOLD_STAGE": "1", "STAGE_READY": str(ready.path)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(self.finish_check, process)
        ready.wait("stage owns checkout and has hidden target")
        return process

    def finish_check(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.communicate("1", timeout=10)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    def records(self) -> list[dict]:
        return [
            json.loads(path.read_text())
            for path in (self.root / ".dev-workflow/runs").glob("*/result.json")
        ]

    def test_records_phase_failure_and_tested_revision(self) -> None:
        self.write_script(
            "scripts/check-core",
            # fmt: python
            """
            import sys

            print("complete failure diagnostic", file=sys.stderr)
            print("client_server/output/test_previews::example: failed", flush=True)
            print("rerun: scripts/test --timeout 45 client_server/output/test_previews::example")
            raise SystemExit(7)
            """,
        )
        result = self.run_command("scripts/check")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        records = self.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["exit_status"], 7)
        self.assertEqual(
            record["revision"],
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=self.root, text=True
            ).strip(),
        )
        self.assertTrue(record["worktree_status"])
        self.assertEqual(
            [phase["name"] for phase in record["phases"]], ["stage", "core"]
        )
        self.assertEqual(
            record["failing_selectors"], ["client_server/output/test_previews::example"]
        )
        for phase in record["phases"]:
            self.assertGreaterEqual(phase["elapsed_seconds"], 0)
            self.assertTrue(Path(phase["log"]).is_file())
        self.assertIn("result.json", result.stderr)
        self.assertIn("complete failure diagnostic", result.stderr)
        self.assertIn(
            "rerun: scripts/test --timeout 45 client_server/output/test_previews::example",
            result.stderr,
        )

    def test_check_profiles_record_scope_and_forward_full_to_both_children(
        self,
    ) -> None:
        for script in ("scripts/test", "scripts/check-core"):
            self.write_script(
                script,
                # fmt: python
                """
                import sys

                print(repr(sys.argv[1:]))
                """,
            )
        for arguments, phases, test_arguments in (
            ((), ["stage", "core", "transcripts"], "[]"),
            (("--quick",), ["stage", "core", "transcripts"], "[]"),
            (
                ("--full",),
                ["stage", "core", "transcripts", "installation"],
                "['--full']",
            ),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_command("scripts/check", *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = next(
                    r for r in self.records() if r["command"] == ["check", *arguments]
                )
                self.assertEqual([p["name"] for p in record["phases"]], phases)
                for phase in record["phases"]:
                    if phase["name"] in {"core", "transcripts"}:
                        self.assertEqual(
                            Path(phase["log"]).read_text().strip(), test_arguments
                        )

    def test_core_profiles_keep_tooling_self_tests_in_full(self) -> None:
        shutil.copy2(ROOT / "scripts/check-core", self.root / "scripts/check-core")
        common = [
            "runtime-sources",
            "architecture",
            "rust-format",
            "clippy",
            "rust-tests",
        ]
        tooling = [
            "release-tests",
            "staging-tests",
            "runner-tests",
            "workflow-tests",
            "format-tests",
            "development-tests",
            "client-tests",
        ]
        for script in (
            "scripts/validate_runtime_sources.py",
            "tests/release.py",
            "tests/staging.py",
            "tests/transcript_runner.py",
            "tests/workflow.py",
            "tests/format.py",
            "tests/development.py",
            "tests/mcp_client.py",
            "tests/architecture.py",
            "scripts/cargo",
        ):
            self.write_script(script, 'print("checked")')
        # Exercise a shebang alias whose spelling differs from this interpreter.
        (self.root / "scripts/python3").symlink_to(sys.executable)
        self.environment["PATH"] = (
            f"{self.root / 'scripts'}{os.pathsep}{os.environ['PATH']}"
        )
        for arguments in ((), ("--quick",), ("--full",)):
            with self.subTest(arguments=arguments):
                result = self.run_command("scripts/check-core", *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = next(
                    r
                    for r in self.records()
                    if r["command"] == ["check-core", *arguments]
                )
                expected = (
                    common[:1] + tooling + common[1:]
                    if arguments == ("--full",)
                    else common
                )
                self.assertEqual([p["name"] for p in record["phases"]], expected)
                interpreter = next(
                    phase["command"][0]
                    for phase in record["phases"]
                    if phase["name"] == "architecture"
                )
                self.assertTrue(Path(interpreter).samefile(sys.executable))
                architecture = f"[architecture] {interpreter} tests/architecture.py"
                if arguments != ("--full",):
                    architecture += " SandboxProcessBoundaryTests"
                self.assertIn(architecture + "\n", result.stderr)

    def test_validation_output_is_kept_in_advertised_phase_logs(self) -> None:
        self.write_script(
            "scripts/check-core",
            # fmt: python
            """
            import sys

            print("complete phase output")
            print("complete phase diagnostic", file=sys.stderr)
            """,
        )
        result = self.run_command("scripts/check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("complete phase output", result.stderr)
        (record,) = self.records()
        phase = record["phases"][1]
        self.assertIn(phase["log"], result.stderr)
        self.assertIn("complete phase output\n", Path(phase["log"]).read_text())
        self.assertIn("complete phase diagnostic\n", Path(phase["log"]).read_text())

    def test_cancellation_during_diagnostic_replay_preserves_phase_record(self) -> None:
        self.write_script(
            "scripts/check-core",
            # fmt: python
            """
            print("failure diagnostic " * 1_000_000)
            raise SystemExit(7)
            """,
        )
        process = subprocess.Popen(
            ["scripts/check"],
            cwd=self.root,
            env=self.environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stderr is not None
            # The pipe cannot hold the entire log: receiving this prefix proves
            # replay began, and withholding further reads keeps it incomplete.
            prefix = process.stderr.read(4096)
            self.assertIn("failure diagnostic", prefix)
            process.terminate()
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 128 + signal.SIGTERM)
            (record,) = self.records()
            self.assertEqual(record["exit_status"], 128 + signal.SIGTERM)
            self.assertEqual(record["phases"][-1]["name"], "core")
            self.assertEqual(record["phases"][-1]["exit_status"], 7)
            self.assertIn(
                "failure diagnostic", Path(record["phases"][-1]["log"]).read_text()
            )
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_signalled_phase_preserves_shell_exit_status(self) -> None:
        self.write_script(
            "scripts/check-core",
            # fmt: python
            """
            import os
            import signal

            os.kill(os.getpid(), signal.SIGTERM)
            """,
        )
        result = self.run_command("scripts/check")
        self.assertEqual(result.returncode, 128 + signal.SIGTERM)
        (record,) = self.records()
        self.assertEqual(record["exit_status"], 128 + signal.SIGTERM)
        self.assertEqual(record["phases"][-1]["exit_status"], -signal.SIGTERM)

    def test_final_record_and_announcement_keep_the_completed_status(self) -> None:
        # Inject a signal at the standard file/stream boundary of the public
        # command; no workflow helpers are imported or replaced.
        self.write_script(
            "sitecustomize.py",
            # fmt: python
            """
            import json
            import os
            import signal
            import sys
            from pathlib import Path

            write_text = Path.write_text


            def signal_final_write(path, text, *args, **kwargs):
                if (
                    os.environ["CANCEL_FINAL"] == "save"
                    and path.name == "result.tmp"
                    and json.loads(text)["exit_status"] is not None
                ):
                    os.kill(os.getpid(), signal.SIGTERM)
                return write_text(path, text, *args, **kwargs)


            Path.write_text = signal_final_write
            write_stderr = sys.stderr.write


            def signal_announcement(text):
                if os.environ["CANCEL_FINAL"] == "announcement" and text.startswith(
                    "Validation record:"
                ):
                    os.kill(os.getpid(), signal.SIGTERM)
                return write_stderr(text)


            sys.stderr.write = signal_announcement
            """,
        )
        self.environment["PYTHONPATH"] = str(self.root)
        runs = self.root / ".dev-workflow/runs"
        for boundary in ("save", "announcement"):
            with self.subTest(boundary=boundary):
                before = set(runs.glob("*/result.json"))
                self.environment["CANCEL_FINAL"] = boundary
                result = self.run_command("scripts/check")
                self.assertEqual(result.returncode, 0, result.stderr)
                (path,) = set(runs.glob("*/result.json")) - before
                self.assertEqual(json.loads(path.read_text())["exit_status"], 0)
                self.assertIn(f"Validation record: {path.resolve()}", result.stderr)

    def test_source_archive_does_not_inherit_enclosing_git_metadata(self) -> None:
        archive = self.root / "archive"
        shutil.copytree(
            self.root, archive, ignore=shutil.ignore_patterns(".git", "archive")
        )
        result = self.run_command("scripts/check", root=archive)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (path,) = (archive / ".dev-workflow/runs").glob("*/result.json")
        record = json.loads(path.read_text())
        self.assertIsNone(record["revision"])
        self.assertIsNone(record["worktree_status"])

    def test_packaging_conflict_survives_target_rename(self) -> None:
        process = self.start_check()
        for method in ("build_wheel", "build_sdist"):
            with self.subTest(method=method):
                result = self.run_command(
                    sys.executable,
                    "-c",
                    f"import build_backend; build_backend.{method}('.')",
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("checkout is busy", result.stdout + result.stderr)
                self.assertIn("Last recorded owner (may be stale)", result.stderr)
        self.finish_check(process)
        result = self.run_command(
            sys.executable, "-c", "import build_backend; build_backend.build_wheel('.')"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_nested_packaging_reuses_checkout_owner(self) -> None:
        self.write_script(
            "scripts/check-core",
            # fmt: python
            """
            import subprocess
            import sys

            subprocess.run(
                [
                    "scripts/with-checkout",
                    sys.executable,
                    "-c",
                    "import build_backend; assert build_backend.build_wheel('.') == 'fixture.whl'",
                ],
                check=True,
            )
            """,
        )
        result = self.run_command("scripts/check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.records())
        self.assertTrue(all(record["exit_status"] == 0 for record in self.records()))

    def test_termination_records_status_and_releases_ownership(self) -> None:
        process = self.start_check()
        process.terminate()
        self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
        (record,) = self.records()
        self.assertEqual(record["exit_status"], 128 + signal.SIGTERM)
        self.assertEqual(record["phases"][-1]["exit_status"], -signal.SIGTERM)
        result = self.run_command(
            sys.executable, "-c", "import build_backend; build_backend.build_wheel('.')"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cancellation_during_git_metadata_finalizes_record(self) -> None:
        ready = FifoCheckpoint.create(self.directory / "git-ready")
        self.addCleanup(ready.close)
        self.write_script(
            "git",
            # fmt: python
            """
            import os
            import signal
            import sys

            if sys.argv[1:] == ["status", "--porcelain"]:
                with open(os.environ["GIT_READY"], "wb", buffering=0) as receipt:
                    receipt.write(b"1")
                signal.pause()
            else:
                os.execv(os.environ["REAL_GIT"], ["git", *sys.argv[1:]])
            """,
        )
        process = subprocess.Popen(
            ["scripts/check"],
            cwd=self.root,
            env=self.environment
            | {
                "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                "REAL_GIT": shutil.which("git"),
                "GIT_READY": str(ready.path),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready.wait("git status is collecting checkout metadata")
            process.terminate()
            _, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 128 + signal.SIGTERM)
            (record,) = self.records()
            self.assertEqual(record["exit_status"], 128 + signal.SIGTERM)
            self.assertEqual(record["phases"], [])
            self.assertIn("Validation record:", stderr)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_cancellation_escalates_for_direct_and_nested_stubborn_phases(self) -> None:
        self.write_script(
            "stubborn.py",
            # fmt: python
            """
            import os
            import signal

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print(f"stubborn ready {os.getpgrp()}", flush=True)
            signal.pause()
            """,
        )
        for nested in (False, True):
            with self.subTest(nested=nested):
                command = ["scripts/with-checkout"]
                if nested:
                    command.append("scripts/with-checkout")
                command += [sys.executable, "stubborn.py"]
                process = subprocess.Popen(
                    command,
                    cwd=self.root,
                    env=self.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                group = None
                try:
                    assert process.stdout is not None
                    receipt = read_lines(process.stdout, 1, "stubborn phase setup")
                    self.assertTrue(receipt[-1].startswith("stubborn ready "), receipt)
                    group = int(receipt[-1].rsplit(" ", 1)[1])
                    process.terminate()
                    # EOF also proves the stubborn writer has retired.
                    output, _ = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
                finally:
                    if group is not None:
                        try:
                            os.killpg(group, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)
                result = self.run_command(
                    "scripts/with-checkout", sys.executable, "-c", "pass"
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(PROCESS_EVENTS.available, PROCESS_EVENTS.reason)
    def test_cancellation_preserves_status_with_only_zombie_group_members(self) -> None:
        ready = FifoCheckpoint.create(self.directory / "zombie-ready")
        release = FifoCheckpoint.create(self.directory / "reap-zombie")
        for checkpoint in (ready, release):
            self.addCleanup(checkpoint.close)
        self.write_script(
            "reaper.py",
            # fmt: python
            """
            import os
            import sys

            group = int(sys.argv[1])
            os.setpgid(0, 0)
            child = os.fork()
            if child == 0:
                os.setpgid(0, group)
                os._exit(0)
            os.waitid(os.P_PID, child, os.WEXITED | os.WNOWAIT)
            with open(os.environ["ZOMBIE_READY"], "wb", buffering=0) as ready:
                ready.write(b"1")
            with open(os.environ["REAP_ZOMBIE"], "rb", buffering=0) as release:
                assert release.read(1) == b"1"
            os.waitpid(child, 0)
            """,
        )
        self.write_script(
            "parent.py",
            # fmt: python
            """
            import os
            import signal
            import subprocess
            import sys

            reaper = subprocess.Popen(
                [sys.executable, "reaper.py", str(os.getpgrp())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"reaper ready {reaper.pid}", flush=True)
            signal.pause()
            """,
        )
        process = subprocess.Popen(
            ["scripts/with-checkout", sys.executable, "parent.py"],
            cwd=self.root,
            env=self.environment
            | {"ZOMBIE_READY": str(ready.path), "REAP_ZOMBIE": str(release.path)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with Events() as events:
            reaper = None
            try:
                assert process.stdout is not None
                receipt = read_lines(process.stdout, 1, "zombie reaper setup")
                reaper = int(receipt[-1].rsplit(" ", 1)[1])
                events.watch_process(reaper)
                ready.wait("exited child remains unreaped in the phase group")
                process.terminate()
                output, _ = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
            finally:
                release.release()
                try:
                    if reaper is not None:
                        self.assertIn(reaper, events.wait(3))
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.communicate(timeout=10)

    def test_quit_retires_phase_before_releasing_ownership(self) -> None:
        self.write_script(
            "waiting.py",
            # fmt: python
            """
            import os
            import signal

            print(f"waiting {os.getpgrp()}", flush=True)
            signal.pause()
            """,
        )
        process = subprocess.Popen(
            ["scripts/with-checkout", sys.executable, "waiting.py"],
            cwd=self.root,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        group = None
        try:
            assert process.stdout is not None
            receipt = read_lines(process.stdout, 1, "waiting phase setup")
            self.assertTrue(receipt[-1].startswith("waiting "), receipt)
            group = int(receipt[-1].split()[1])
            process.send_signal(signal.SIGQUIT)
            self.assertEqual(process.wait(timeout=10), 128 + signal.SIGQUIT)
            process.communicate(timeout=10)
        finally:
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        result = self.run_command("scripts/with-checkout", sys.executable, "-c", "pass")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_checks_in_separate_checkouts_run_concurrently(self) -> None:
        other = self.directory / "other"
        shutil.copytree(self.root, other)
        process = self.start_check()
        result = self.run_command("scripts/check", root=other)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(process.poll())
        self.finish_check(process)

    def test_missing_nested_script_records_failure_after_successful_phase(self) -> None:
        shutil.copy2(ROOT / "scripts/check-core", self.root / "scripts/check-core")
        self.write_script("scripts/validate_runtime_sources.py", 'print("checked")')
        result = self.run_command("scripts/check")
        self.assertNotEqual(result.returncode, 0)
        record = next(r for r in self.records() if r["command"] == ["check-core"])
        self.assertEqual(record["phases"][0]["exit_status"], 0)
        # Python now launches repository scripts explicitly on every platform;
        # a missing script is Python's exit status 2, not a shebang exec error.
        self.assertEqual(record["phases"][1]["exit_status"], 2)
        self.assertEqual(record["exit_status"], 2)

    def test_wrapped_command_preserves_stdout_and_stderr(self) -> None:
        self.write_script(
            "output.py",
            # fmt: python
            """
            import sys

            print('{"answer": 42}')
            print("diagnostic", file=sys.stderr)
            """,
        )
        result = self.run_command("scripts/with-checkout", sys.executable, "output.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{"answer": 42}\n')
        self.assertEqual(result.stderr, "diagnostic\n")
        self.assertEqual(self.records(), [])

    def test_repeated_cancellation_cannot_interrupt_escalation(self) -> None:
        self.write_script(
            "stubborn.py",
            # fmt: python
            """
            import os
            import signal


            def terminated(*_):
                with open(os.environ["TERM_RECEIPT"], "wb", buffering=0) as receipt:
                    receipt.write(b"1")


            signal.signal(signal.SIGTERM, terminated)
            print(f"stubborn ready {os.getpgrp()}", flush=True)
            while True:
                signal.pause()
            """,
        )
        receipt = FifoCheckpoint.create(self.directory / "term-received")
        self.addCleanup(receipt.close)
        process = subprocess.Popen(
            ["scripts/with-checkout", sys.executable, "stubborn.py"],
            cwd=self.root,
            env=self.environment | {"TERM_RECEIPT": str(receipt.path)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        group = None
        try:
            assert process.stdout is not None
            output = read_lines(process.stdout, 1, "stubborn phase setup")
            group = int(output[-1].rsplit(" ", 1)[1])
            process.terminate()
            receipt.wait("cancellation reached the phase")
            process.send_signal(signal.SIGHUP)
            self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
        finally:
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_phase_exit_does_not_wait_for_inherited_descendant_streams(self) -> None:
        self.write_script(
            "child.py",
            # fmt: python
            """
            import signal

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print("ready", flush=True)
            signal.pause()
            """,
        )
        self.write_script(
            "parent.py",
            # fmt: python
            """
            import os
            import subprocess
            import sys
            from pathlib import Path

            child = subprocess.Popen([sys.executable, "child.py"], stdout=subprocess.PIPE)
            assert child.stdout.readline() == b"ready\\n"
            Path("group").write_text(str(os.getpgrp()))
            raise SystemExit(7)
            """,
        )
        try:
            result = self.run_command(
                "scripts/with-checkout", sys.executable, "parent.py"
            )
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            result = self.run_command(
                "scripts/with-checkout", sys.executable, "-c", "pass"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            if (self.root / "group").exists():
                try:
                    os.killpg(int((self.root / "group").read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @unittest.skipUnless(PROCESS_EVENTS.available, PROCESS_EVENTS.reason)
    def test_descendant_can_finish_cleanup_after_its_leader_exits(self) -> None:
        terminating = FifoCheckpoint.create(self.directory / "terminating")
        release = FifoCheckpoint.create(self.directory / "release")
        finished = FifoCheckpoint.create(self.directory / "finished")
        for checkpoint in (terminating, release, finished):
            self.addCleanup(checkpoint.close)
        self.write_script(
            "child.py",
            # fmt: python
            """
            import os
            import signal


            def terminate(*_):
                with open(os.environ["TERMINATING"], "wb", buffering=0) as receipt:
                    receipt.write(b"1")
                with open(os.environ["RELEASE"], "rb", buffering=0) as gate:
                    assert gate.read(1) == b"1"
                with open(os.environ["FINISHED"], "wb", buffering=0) as receipt:
                    receipt.write(b"1")
                raise SystemExit(0)


            signal.signal(signal.SIGTERM, terminate)
            print("ready", flush=True)
            signal.pause()
            """,
        )
        self.write_script(
            "parent.py",
            # fmt: python
            """
            import os
            import signal
            import subprocess
            import sys

            child = subprocess.Popen([sys.executable, "child.py"], stdout=subprocess.PIPE)
            assert child.stdout.readline() == b"ready\\n"
            print(f"parent ready {os.getpid()}", flush=True)
            signal.pause()
            """,
        )
        process = subprocess.Popen(
            ["scripts/with-checkout", sys.executable, "parent.py"],
            cwd=self.root,
            env=self.environment
            | {
                "TERMINATING": str(terminating.path),
                "RELEASE": str(release.path),
                "FINISHED": str(finished.path),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        parent = None
        try:
            assert process.stdout is not None
            output = read_lines(process.stdout, 1, "parent and child setup")
            parent = int(output[-1].rsplit(" ", 1)[1])
            with Events() as events:
                events.watch_process(parent)
                process.terminate()
                terminating.wait("child started cleanup")
                self.assertIn(parent, events.wait(3))
                self.assertEqual(
                    read_lines(process.stdout, 1, "descendant cleanup grace"),
                    ["[cleanup] waiting for phase descendants"],
                )
                release.release()
                finished.wait("child completed cleanup after leader exit", timeout=3)
            self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
        finally:
            if parent is not None:
                try:
                    os.killpg(parent, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_first_cancellation_during_normal_cleanup_finishes_retirement(self) -> None:
        self.write_script(
            "parent.py",
            # fmt: python
            """
            import os
            import signal
            from pathlib import Path

            reader, writer = os.pipe()
            if os.fork() == 0:
                os.close(reader)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                os.write(writer, b"1")
                os.close(writer)
                signal.pause()
            else:
                os.close(writer)
                assert os.read(reader, 1) == b"1"
                os.close(reader)
                Path("group").write_text(str(os.getpgrp()))
            """,
        )
        process = subprocess.Popen(
            ["scripts/with-checkout", sys.executable, "parent.py"],
            cwd=self.root,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            assert process.stdout is not None
            self.assertEqual(
                read_lines(process.stdout, 1, "normal descendant cleanup"),
                ["[cleanup] waiting for phase descendants"],
            )
            process.terminate()
            self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
            # EOF requires the descendant to retire as well as the wrapper.
            process.communicate(timeout=10)
        finally:
            if (self.root / "group").exists():
                try:
                    os.killpg(int((self.root / "group").read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
