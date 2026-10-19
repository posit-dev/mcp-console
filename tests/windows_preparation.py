"""Resolver enforcement through public startup and late MCP requirements."""

import ctypes
import gzip
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from support.normalization import code
from support.installation import installed_console, native_console
from support.windows_firewall import application_rules, require_unelevated
from support.windows_mxc import requires_mxc
from windows_sandbox import BINARY, ROOT, workspace


@unittest.skipUnless(os.name == "nt", "native Windows preparation")
class WindowsPreparation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A fresh executable path has no authorization left by earlier runs.
        cls.binary = installed_console(native_console(BINARY))
        cls.companions = list(cls.binary.parent.parent.rglob("mcp-console-sandbox.exe"))
        cls.directory = tempfile.TemporaryDirectory(
            prefix="console preparation fixture "
        )
        cls.fixture = Path(cls.directory.name) / "preparation.exe"
        subprocess.run(
            [
                "rustc",
                str(ROOT / "tests/fixtures/windows_preparation.rs"),
                "-o",
                str(cls.fixture),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.maxDiff = None
        self.assertEqual(application_rules(self.companions), {})
        self.addCleanup(
            lambda: self.assertEqual(application_rules(self.companions), {})
        )
        directory = workspace()
        self.root = directory.__enter__()
        self.addCleanup(directory.__exit__, None, None, None)
        tools = self.root / "bin"
        tools.mkdir()
        for tool in ("uv", "ir"):
            shutil.copyfile(self.fixture, tools / f"{tool}.exe")
        self.protected = self.root / "protected"
        (self.protected / ".agents").mkdir(parents=True)
        (self.protected / "readable").write_text("host read")
        (self.protected / ".agents/control").write_text("trusted control")
        control = self.root / "cache/dependencies/.agents/control"
        control.parent.mkdir(parents=True)
        control.write_text("cache control")
        cache = self.root / "cache/dependencies/uv/cache"
        cache.mkdir(parents=True)
        (cache / "cache-refresh").write_text("host-owned cached data")
        alias = cache / "escape-junction"
        subprocess.run(
            [
                os.environ["COMSPEC"],
                "/d",
                "/c",
                "mklink",
                "/j",
                str(alias),
                str(self.protected),
            ],
            check=True,
            capture_output=True,
        )
        self.environment = os.environ | {
            "PATH": os.pathsep.join(
                [
                    str(tools),
                    str(Path(sys.executable).parent),
                    str(Path(os.environ["SystemRoot"]) / "System32"),
                ]
            ),
            "MCP_CONSOLE_CACHE_DIR": str(self.root / "cache"),
            "RETICULATE_PYTHON": "managed",
            "PREPARATION_PROBE": str(ROOT / "tests/fixtures/resolver/windows_probe.py"),
            "PREPARATION_PROTECTED": str(self.protected),
            "PREPARATION_CACHE_CONTROL": str(control),
            "PREPARATION_ALIAS": str(alias),
            "PREPARATION_PYTHON": sys.executable,
            "PREPARATION_UV": shutil.which("uv"),
            "PREPARATION_IR": shutil.which("ir") or "",
            "UV_CACHE_DIR": str(self.root / "cache/dependencies/uv/cache"),
            "UV_PYTHON_INSTALL_DIR": str(self.root / "cache/dependencies/uv/python"),
            "UV_TOOL_DIR": str(self.root / "cache/dependencies/uv/tools"),
        }
        self.environment.pop("R_HOME", None)
        self.environment.pop("RETICULATE_UV", None)

    def session(self, *, overrides=(), sandbox=True):
        from windows import Session

        session = Session(
            self.environment,
            binary=self.binary,
            sandbox=sandbox,
            overrides=(
                "languages=[python]",
                'python.managed={packages: ["six==1.17.0"], version: "3.14"}',
                *overrides,
            ),
        )
        self.addCleanup(session.close)
        session.timeout = 600
        session.initialize()
        session.request("tools/list", {})
        return session

    def test_resolver_configuration_is_exported_and_explicit_restrictions_survive(self):
        for overrides in (
            (),
            ("resolver.sandbox.filesystem={}", "resolver.sandbox.network=restricted"),
        ):
            with self.subTest(overrides=overrides):
                result = subprocess.run(
                    [
                        BINARY,
                        "config",
                        "resolve",
                        "--no-config",
                        "--full",
                        "--format",
                        "json",
                        *[item for value in overrides for item in ("-c", value)],
                    ],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                policy = json.loads(result.stdout)["resolver"]["sandbox"]
                self.assertIsInstance(policy, dict)
                if overrides:
                    self.assertEqual(policy["network"], "restricted")
                    self.assertEqual(
                        policy["filesystem"],
                        {"read_only": [], "read_write": [], "deny": []},
                    )
                else:
                    self.assertTrue(policy["network"]["allow_local_binding"])
                    self.assertIn(
                        "pypi.org", policy["network"]["proxy"]["domains"]["allow"]
                    )

    @requires_mxc(BINARY)
    def test_default_preparation_contains_descendants_and_preserves_late_state(self):
        session = self.session()
        result = session.send(python="import six; saved = 42; print(six.__version__)")
        self.assertFalse(result.get("isError"), result)
        self.assertIn("1.17.0", json.dumps(result))
        result = session.send(
            requirements={"python": ["sniffio==1.3.1"]},
            python="import sniffio; print(saved)",
        )
        self.assertFalse(result.get("isError"), result)
        self.assertIn("42", json.dumps(result))
        failed = session.send(requirements={"python": ["six==0"]}, control="restart")
        self.assertTrue(failed.get("isError"), failed)
        requirements = session.send(requirements={"action": "get"})[
            "structuredContent"
        ]["requirements"]["python"]
        self.assertEqual(requirements, ["six==1.17.0", "sniffio==1.3.1"])
        self.assertIn("42", json.dumps(session.send(python="print(saved)")))
        records = list((self.root / "cache/dependencies/uv/cache").glob("probe-*.json"))
        self.assertGreater(len(records), 3)
        self.assertTrue(
            any(
                json.loads(path.read_text())["arguments"] == ["--descendant"]
                for path in records
            )
        )
        self.assertFalse((self.protected / "escape").exists())
        self.assertEqual(
            (self.protected / ".agents/control").read_text(), "trusted control"
        )
        self.assertEqual(
            (self.root / "cache/dependencies/.agents/control").read_text(),
            "cache control",
        )

    def test_unavailable_mxc_rejects_preparation_before_running_tools(self):
        from support.windows_faults import mcp_startup_failure, run_with_unavailable_mxc
        from support.windows_mxc import mxc_requirement

        result = run_with_unavailable_mxc(
            [
                str(self.binary),
                "serve",
                "--no-config",
                "-c",
                "languages=[python]",
                "-c",
                'python.managed={packages: ["six==1.17.0"], version: "3.14"}',
            ],
            cwd=self.root,
            env=self.environment | {"MCP_CONSOLE_HOME": str(self.root / "home")},
            inject_unavailable=mxc_requirement(BINARY).available,
            interaction=mcp_startup_failure,
        )
        self.assertEqual(result.returncode, 1, result)
        self.assertIn(b"MXC is unavailable on this executor;", result.stderr)
        self.assertNotIn(b"USER CODE RAN", result.stdout + result.stderr)
        self.assertFalse(list((self.root / "cache").rglob("probe-*.json")))
        self.assertFalse((self.protected / "escape").exists())

    def test_trusted_warmup_prepares_before_unavailable_worker_sandbox(self):
        from support.windows_faults import run_with_unavailable_mxc
        from support.windows_mxc import mxc_requirement

        # A real interpreter keeps inspection's executable identity check.
        runtime = self.root / "python"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(runtime)],
            check=True,
            timeout=60,
        )
        python = runtime / "Scripts/python.exe"
        (runtime / "Lib/site-packages/sitecustomize.py").write_text(
            "import os, runpy\nrunpy.run_path(os.environ['PREPARATION_PROBE'])\n"
        )
        temporary = self.root / "host-temporary"
        temporary.mkdir()
        result = run_with_unavailable_mxc(
            [
                str(self.binary),
                "warmup",
                "--no-config",
                "-c",
                "languages=[python]",
                "-c",
                "python=" + json.dumps(str(python)),
                "-c",
                "resolver.sandbox.filesystem="
                + json.dumps({"read_only": [self.root.anchor]}),
            ],
            cwd=self.root,
            env=self.environment
            | {
                "MCP_CONSOLE_HOME": str(self.root / "home"),
                "PREPARATION_HOST": "1",
                "TEMP": str(temporary),
                "TMP": str(temporary),
            },
            inject_unavailable=mxc_requirement(BINARY).available,
        )
        self.assertEqual(result.returncode, 1, result)
        self.assertIn(b"MXC is unavailable on this executor;", result.stderr)
        records = list((self.root / "cache").rglob("probe-*.json"))
        self.assertTrue(records)
        self.assertTrue(all(json.loads(path.read_text())["host"] for path in records))
        self.assertEqual((self.protected / "escape").read_text(), "changed")

    def test_explicit_resolver_policy_conflicts_with_no_sandbox(self):
        result = subprocess.run(
            [
                BINARY,
                "warmup",
                "--no-config",
                "--no-sandbox",
                "-c",
                "resolver.sandbox.network=restricted",
            ],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result)
        self.assertIn(
            "explicit sandbox or resolver.sandbox permissions require sandboxing",
            result.stderr,
        )

    @requires_mxc(BINARY)
    def test_host_cache_falls_back_to_userprofile_after_relative_home(self):
        profile = self.root / "account"
        extensions = profile / ".duckdb/extensions"
        extensions.mkdir(parents=True)
        self.environment.update(HOME="relative-home", USERPROFILE=str(profile))
        self.environment.pop("MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY", None)
        session = self.session(
            overrides=(
                "cache=host",
                "r=null",
                "resolver.sandbox.filesystem="
                + json.dumps(
                    {
                        "read_only": [self.root.anchor],
                        "read_write": [
                            str(self.root / "cache/dependencies/uv"),
                            str(extensions),
                        ],
                    }
                ),
            )
        )
        # fmt: python
        program = code("""
            import os
            from pathlib import Path

            extensions = Path(os.environ["MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY"])
            assert extensions == Path(os.environ["USERPROFILE"]) / ".duckdb/extensions"
            print("Host cache profile fallback")
            """)
        result = session.send(python=program)
        self.assertFalse(result.get("isError"), result)
        self.assertIn("Host cache profile fallback", json.dumps(result))

    @requires_mxc(BINARY)
    def test_explicit_filesystem_restriction_blocks_cache_writes(self):
        session = self.session(
            overrides=(
                "resolver.sandbox.filesystem="
                + json.dumps({"read_only": [self.root.anchor]}),
            )
        )
        result = session.send(python="raise AssertionError('USER CODE RAN')")
        self.assertTrue(result.get("isError"), result)
        self.assertIn("PermissionError", json.dumps(result))
        self.assertFalse(list((self.root / "cache").rglob("probe-*.json")))

    @requires_mxc(BINARY)
    def test_refreshes_existing_download_cache_during_late_preparation(self):
        self.environment["PREPARATION_REFRESH_CACHE"] = "1"
        session = self.session()
        self.assertIn("42", json.dumps(session.send(python="saved = 42; print(saved)")))
        result = session.send(
            requirements={"python": ["sniffio==1.3.1"]},
            python="import six, sniffio; print(saved)",
        )
        self.assertFalse(result.get("isError"), result)
        self.assertIn("42", json.dumps(result))

    @requires_mxc(BINARY)
    def test_cache_reuse_with_explicit_filesystem_and_network_restrictions(self):
        session = self.session()
        self.assertIn(
            "1.17.0",
            json.dumps(session.send(python="import six; print(six.__version__)")),
        )
        records = list((self.root / "cache/dependencies/uv/cache").glob("probe-*.json"))
        temporary = {json.loads(path.read_text())["temporary"] for path in records}
        session.close()
        self.assertTrue(all(not Path(path).exists() for path in temporary), temporary)
        self.environment.update(
            PREPARATION_NO_DOWNLOADS="1",
            PREPARATION_EXCLUDED_CACHE=str(self.root / "cache/dependencies/ir"),
        )
        filesystem = {
            "read_only": [self.root.anchor],
            "read_write": [str(self.root / "cache/dependencies/uv")],
        }
        session = self.session(
            overrides=(
                "resolver.sandbox.network=restricted",
                "resolver.sandbox.filesystem=" + json.dumps(filesystem),
            )
        )
        result = session.send(python="import six; print(six.__version__)")
        self.assertFalse(result.get("isError"), result)
        self.assertIn("1.17.0", json.dumps(result))
        self.assertFalse((self.root / "cache/dependencies/ir/escape").exists())

    def test_no_sandbox_preserves_explicit_host_execution(self):
        temporary = self.root / "host-temporary"
        temporary.mkdir()
        self.environment.update(
            PREPARATION_HOST="1", TEMP=str(temporary), TMP=str(temporary)
        )
        session = self.session(sandbox=False)
        result = session.send(python="import six; print(six.__version__)")
        self.assertFalse(result.get("isError"), result)
        self.assertIn("1.17.0", json.dumps(result))
        self.assertEqual((self.protected / "escape").read_text(), "changed")

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    @requires_mxc(BINARY)
    def test_native_r_tool_environment_needs_no_firewall_authorization(self):
        require_unelevated()
        self.environment.update(PATH=os.environ["PATH"], R_HOME=os.environ["R_HOME"])
        session = self.session(
            overrides=(
                "languages=[r]",
                "python=" + json.dumps(sys.executable),
                "r.packages=[]",
                "sandbox.network=restricted",
            )
        )
        result = session.expect(
            "R prepared", r="stopifnot(utf8::utf8_valid('prepared')); cat('R prepared')"
        )
        self.assertFalse(result.get("isError"), result)

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    @requires_mxc(BINARY)
    def test_r_backed_preparation_with_default_python_selection(self):
        self.environment["R_HOME"] = os.environ["R_HOME"]
        session = self.session(
            overrides=(
                "languages=[r,python]",
                "python=null",
                "r.packages=[]",
                "sandbox.network=restricted",
            )
        )
        for field, program in (("r", "cat(42L)"), ("python", "print(42)")):
            result = session.send(**{field: program})
            self.assertFalse(result.get("isError"), result)
            self.assertIn("42", json.dumps(result))

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    @requires_mxc(BINARY)
    def test_cold_r_preparation_and_inspection_have_separate_permissions(self):
        shutil.copyfile(self.fixture, self.root / "bin/R.exe")
        self.environment["PREPARATION_R"] = str(
            Path(os.environ["R_HOME"]) / "bin/R.exe"
        )
        session = self.session(
            overrides=(
                "languages=[r]",
                "python=" + json.dumps(sys.executable),
                "r.executable=" + json.dumps(str(self.root / "bin/R.exe")),
                'r.packages=["utf8"]',
                "sandbox.network=enabled",
                "sandbox.filesystem.read_write=" + json.dumps([str(self.protected)]),
            )
        )
        result = session.expect(
            "TRUE 42", r="saved <- 42L; cat(utf8::utf8_valid('prepared'), saved)"
        )
        self.assertFalse(result.get("isError"), result)
        self.assertIn("TRUE 42", json.dumps(result))
        result = session.send(
            requirements={"r": ["digest"]},
            r="stopifnot(saved == 42L); cat(digest::digest('prepared'))",
        )
        self.assertFalse(result.get("isError"), result)
        records = [
            json.loads(path.read_text())
            for path in (self.root / "cache/dependencies/uv/cache").glob("probe-*.json")
        ]
        self.assertTrue(
            any("RHOME" in record["arguments"] for record in records), records
        )
        self.assertTrue(
            list((self.root / "cache/dependencies/ir").rglob("utf8/DESCRIPTION"))
        )
        self.assertFalse((self.protected / "escape").exists())

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    @requires_mxc(BINARY)
    def test_uv_bootstrap_prepares_r_backed_python_inside_mxc(self):
        # Bootstrap ir without reusing an installed managed interpreter.
        (self.root / "bin/ir.exe").unlink()
        shutil.copyfile(self.fixture, self.root / "bin/R.exe")
        self.environment.update(
            PREPARATION_R=str(Path(os.environ["R_HOME"]) / "bin/R.exe"),
            UV_PYTHON_PREFERENCE="only-managed",
            PATH=os.pathsep.join(
                [
                    str(self.root / "bin"),
                    str(Path(os.environ["SystemRoot"]) / "System32"),
                ]
            ),
        )
        self.assertIsNone(shutil.which("ir", path=self.environment["PATH"]))
        session = self.session(
            overrides=(
                "languages=[r,python]",
                "r.executable=" + json.dumps(str(self.root / "bin/R.exe")),
                'r.packages=["utf8"]',
            )
        )
        result = session.send(r="cat(utf8::utf8_valid('prepared'))")
        self.assertFalse(result.get("isError"), result)
        self.assertIn("TRUE", json.dumps(result))
        result = session.send(python="import six; print(six.__version__)")
        self.assertFalse(result.get("isError"), result)
        self.assertIn("1.17.0", json.dumps(result))
        records = [
            json.loads(path.read_text())
            for path in (self.root / "cache/dependencies/uv/cache").glob("probe-*.json")
        ]
        self.assertTrue(
            any(
                record["arguments"][:5] == ["tool", "run", "--from", "r-lib-ir", "ir"]
                for record in records
            ),
            records,
        )
        self.assertFalse((self.protected / "escape").exists())

    @requires_mxc(BINARY)
    def test_python_inspection_and_duckdb_extension_preparation_are_contained(self):
        self.environment["PREPARATION_INJECT_HOOK"] = "1"
        session = self.session(
            overrides=(
                "languages=[python,sql]",
                'python.managed.packages=["duckdb==1.4.3"]',
            )
        )
        result = session.send(sql="SELECT 42 AS prepared")
        self.assertFalse(result.get("isError"), result)
        self.assertIn("42", json.dumps(result))
        result = session.send(
            requirements={"duckdb": ["httpfs"]}, sql="SELECT 43 AS prepared_later"
        )
        self.assertFalse(result.get("isError"), result)
        self.assertIn("43", json.dumps(result))
        extensions = self.root / "cache/dependencies/duckdb/extensions"
        self.assertTrue(list(extensions.rglob("httpfs.duckdb_extension")))
        records = [
            json.loads(path.read_text())
            for path in (self.root / "cache/dependencies/uv/cache").glob("probe-*.json")
        ]
        self.assertTrue(any(record["arguments"] == [] for record in records), records)
        self.assertFalse((self.protected / "escape").exists())

    @requires_mxc(BINARY)
    def test_existing_python_inspection_uses_resolver_permissions(self):
        venv = self.root / "existing"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv)],
            check=True,
            capture_output=True,
        )
        shutil.copyfile(
            ROOT / "tests/fixtures/resolver/windows_sitecustomize.py",
            venv / "Lib/site-packages/sitecustomize.py",
        )
        session = self.session(
            overrides=(
                "python=" + json.dumps(str(venv / "Scripts/python.exe")),
                "sandbox.network=enabled",
                "sandbox.filesystem.read_write=" + json.dumps([str(self.protected)]),
            )
        )
        result = session.send(python="print(42)")
        self.assertFalse(result.get("isError"), result)
        self.assertIn("42", json.dumps(result))
        self.assertTrue(
            list((self.root / "cache/dependencies/uv/cache").glob("probe-*.json"))
        )
        self.assertFalse((self.protected / "escape").exists())

    @requires_mxc(BINARY)
    def test_source_build_and_descendants_obey_resolver_permissions(self):
        sources = self.root / "sources"
        sources.mkdir()
        source = io.BytesIO()
        with tarfile.open(fileobj=source, mode="w") as archive:
            files = {
                "pyproject.toml": '[build-system]\nrequires = []\nbuild-backend = "windows_backend"\nbackend-path = ["."]\n[project]\nname = "mcp-console-build-probe"\nversion = "1.0.0"\n',
                # fmt: python
                "windows_backend.py": code("""
                    import os

                    from build_backend import build_wheel

                    os.environ["PREPARATION_BUILD"] = "1"
                    """),
                "build_backend.py": (
                    ROOT / "tests/fixtures/resolver/build_backend.py"
                ).read_text(),
                "trust_probe.py": (
                    ROOT / "tests/fixtures/resolver/windows_probe.py"
                ).read_text(),
            }
            for name, contents in files.items():
                payload = contents.encode()
                entry = tarfile.TarInfo("mcp_console_build_probe-1.0.0/" + name)
                entry.size = len(payload)
                archive.addfile(entry, io.BytesIO(payload))
        (sources / "mcp_console_build_probe-1.0.0.tar.gz").write_bytes(
            gzip.compress(source.getvalue(), mtime=0)
        )
        requests = []

        class Origin(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                body = b"allowed preparation"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Origin) as origin:
            thread = Thread(target=origin.serve_forever)
            thread.start()
            try:
                self.environment.update(
                    UV_FIND_LINKS=str(sources),
                    PREPARATION_ALLOWED_URL=f"http://127.0.0.1:{origin.server_port}/allowed",
                )
                session = self.session(
                    overrides=(
                        'python.managed.packages=["mcp-console-build-probe==1.0.0"]',
                        "resolver.sandbox.network.proxy.domains.allow=[pypi.org,files.pythonhosted.org,astral.sh,releases.astral.sh,github.com,objects.githubusercontent.com,release-assets.githubusercontent.com,127.0.0.1]",
                    )
                )
                result = session.send(
                    python="import mcp_console_build_probe; print(mcp_console_build_probe.answer)"
                )
                self.assertFalse(result.get("isError"), result)
                self.assertIn("42", json.dumps(result))
                records = [
                    json.loads(path.read_text())
                    for path in (self.root / "cache/dependencies/uv/cache").glob(
                        "probe-*.json"
                    )
                ]
                self.assertTrue(any(record["build"] for record in records), records)
                self.assertGreater(len(requests), 1)
                self.assertFalse((self.protected / "escape").exists())
            finally:
                origin.shutdown()
                thread.join()

    @requires_mxc(BINARY)
    def test_cancelled_late_preparation_retires_descendants_and_keeps_session(self):
        self.cancel_preparation(close_input=False)

    @requires_mxc(BINARY)
    def test_eof_retires_preparation_and_descendants(self):
        self.cancel_preparation(close_input=True)

    def cancel_preparation(self, *, close_input):
        gate = socket.socket()
        self.addCleanup(gate.close)
        gate.bind(("127.0.0.1", 0))
        gate.listen()
        gate.settimeout(60)
        self.environment["PREPARATION_GATE"] = str(gate.getsockname()[1])
        session = self.session()
        self.assertIn("42", json.dumps(session.send(python="saved = 42; print(saved)")))

        def start(arguments):
            # Preparation is synchronous even with a zero observation timeout.
            # Keep both public requests outstanding until cancellation settles.
            session.sequence += 1
            identifier = session.sequence
            message = {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": "tools/call",
                "params": {"name": "send", "arguments": arguments},
            }
            session.process.stdin.write(json.dumps(message).encode() + b"\n")
            session.process.stdin.flush()
            return identifier

        pending = start(
            {"requirements": {"python": ["mcp-console-block==1.0"]}, "timeout_ms": 0}
        )
        connection, _ = gate.accept()
        self.addCleanup(connection.close)
        stream = connection.makefile("rb")
        self.addCleanup(stream.close)
        pids = [int(pid) for pid in stream.readline().split()]
        self.assertEqual(len(pids), 2)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        handles = []
        for pid in pids:
            handle = kernel.OpenProcess(0x100000, 0, pid)
            self.assertTrue(handle, (pid, pids, ctypes.get_last_error()))
            self.addCleanup(kernel.CloseHandle, handle)
            handles.append(handle)
        if close_input:
            session.process.stdin.close()
            session.process.wait(timeout=15)
            for handle in handles:
                self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
            return
        control = start({"control": "interrupt", "timeout_ms": 10000})
        responses = {}
        while not {pending, control} <= responses.keys():
            response = session.messages.get(timeout=30)
            self.assertIsNotNone(response)
            if response.get("id") in (pending, control):
                responses[response["id"]] = response
        self.assertTrue(responses[pending]["result"].get("isError"), responses)
        self.assertIn("interrupted", json.dumps(responses[pending]).lower())
        self.assertFalse(responses[control]["result"].get("isError"), responses)
        for handle in handles:
            self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
        self.assertIn("42", json.dumps(session.send(python="print(saved)")))
        requirements = session.send(requirements={"action": "get"})[
            "structuredContent"
        ]["requirements"]["python"]
        self.assertEqual(requirements, ["six==1.17.0"])
