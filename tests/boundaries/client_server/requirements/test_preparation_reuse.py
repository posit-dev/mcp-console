"""Reuse successful preparation without hiding changed or missing artifacts."""

from collections import Counter
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_without_r import environment
from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT
from support.normalization import code
from support.python import isolated_python
from support.records import McpTranscript, Transcript, TranscriptWithCompanions
from support.requirements import POSIX, R, command, requires
from support.resolvers import probing_uv, recording_ir_environment, ir_run_records


@requires(command("rustc"), command("uv"))
def test_reuses_matplotlib_preparation_and_rebuilds_changed_cache(
    binary: Path,
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "bin").mkdir()
        python, site = isolated_python(root)
        package = site / "matplotlib"
        package.mkdir()
        (package / "__init__.py").write_text(
            "import os\ndef get_cachedir(): return os.environ['MPLCONFIGDIR']\n",
            encoding="utf-8",
        )
        manager = package / "font_manager.py"
        manager.write_text(
            # fmt: python
            code("""
                import os
                from pathlib import Path

                cache = Path(os.environ["MPLCONFIGDIR"])
                cache.mkdir(parents=True, exist_ok=True)
                with (cache / "warmups").open("a") as record:
                    record.write("imported\\n")
                if (cache / "fail").exists():
                    raise RuntimeError("fixture font preparation failed")
                (cache / "fontlist-v1.json").write_text("{}")
                """),
            encoding="utf-8",
        )
        metadata = site / "matplotlib-1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            "Metadata-Version: 2.3\nName: matplotlib\nVersion: 1.0\n",
            encoding="utf-8",
        )
        (metadata / "RECORD").write_text(
            "matplotlib/__init__.py,,\nmatplotlib/font_manager.py,,\n",
            encoding="utf-8",
        )
        probing_uv(root, python, "")
        cache = root / "matplotlib-cache"
        env = environment(root / "bin") | {"MPLCONFIGDIR": str(cache)}
        transcripts = {}
        for scenario, expected in (
            ("initial", 1),
            ("reused", 1),
            ("malformed", 2),
            ("removed", 3),
            ("package_changed", 4),
            ("failed", 5),
            ("retry", 6),
        ):
            if scenario == "malformed":
                receipts = list(cache.glob(".mcp-console-fonts-*.json"))
                assert receipts
                for receipt in receipts:
                    receipt.write_bytes(b"\xff")
            if scenario in ("removed", "failed"):
                (cache / "fontlist-v1.json").unlink()
            if scenario == "package_changed":
                manager.write_text(
                    manager.read_text() + "\n# changed fixture package\n"
                )
            if scenario == "failed":
                (cache / "fail").touch()
            if scenario == "retry":
                (cache / "fail").unlink()
            workspace = root / scenario
            workspace.mkdir()
            with McpClient(
                binary,
                DIRECT.serve("--no-config", "-c", "python.managed.packages=[]"),
                env,
                current_directory=workspace,
            ) as client:
                client.transcript.append(
                    {
                        "scenario": scenario,
                        "fixture": "No R; seeded Python with a recording Matplotlib font manager.",
                    }
                )
                client.initialize_and_list_tools()
                client.expect(python="value = 1")
                warmups = (cache / "warmups").read_text().splitlines()
                assert warmups == ["imported"] * expected, (scenario, warmups, expected)
                transcripts[scenario] = client.finish()
        return TranscriptWithCompanions(
            transcripts.pop("initial"),
            {
                f"{name}.yaml": McpTranscript(transcript)
                for name, transcript in transcripts.items()
            },
        )


@requires(POSIX, R, command("ir"))
def test_failed_r_duckdb_close_is_attempted_once_and_not_cached(
    binary: Path,
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env, cache, _, loader = _r_duckdb_fixture(root)
        closes = root / "closes"
        failure = root / "fail-close"
        failure.touch()
        env.update(
            DUCKDB_CLOSE_RECORD=str(closes),
            DUCKDB_CLOSE_FAILURE=str(failure),
        )
        with loader.open("a") as stream:
            stream.write(
                # fmt: r
                code("""
                    if (!nzchar(Sys.getenv("MCP_CONSOLE_LOCAL_RUNTIME"))) {
                      setHook(packageEvent("duckdb", "onLoad"), function(...) {
                        namespace <- asNamespace("DBI")
                        original <- get("dbDisconnect", namespace)
                        replacement <- function(...) {
                          cat(
                            Sys.getpid(),
                            "\\n",
                            sep = "",
                            file = Sys.getenv("DUCKDB_CLOSE_RECORD"),
                            append = TRUE
                          )
                          original(...)
                          if (file.exists(Sys.getenv("DUCKDB_CLOSE_FAILURE"))) {
                            stop("fixture close failed", call. = FALSE)
                          }
                        }
                        unlockBinding("dbDisconnect", namespace)
                        assign("dbDisconnect", replacement, namespace)
                        lockBinding("dbDisconnect", namespace)
                      })
                    }
                    """)
            )
        transcripts = {}
        for scenario in ("failed", "retry"):
            if scenario == "retry":
                failure.unlink()
            workspace = root / scenario
            workspace.mkdir()
            with McpClient(
                binary,
                DIRECT.serve(
                    "--no-config",
                    "-c",
                    "r.packages=[]",
                    "-c",
                    f"python={json.dumps(sys.executable)}",
                ),
                env,
                current_directory=workspace,
            ) as client:
                client.initialize_and_list_tools()
                if scenario == "failed":
                    client.expect(None, r="1+1")
                    assert "fixture close failed" in last_result_text(client)
                    assert not list(cache.glob(".mcp-console-r-duckdb.json"))
                else:
                    client.expect("[1] 2\n", r="1+1")
                    assert list(cache.glob(".mcp-console-r-duckdb.json"))
                attempts = Counter(closes.read_text().splitlines())
                assert attempts and all(count == 1 for count in attempts.values()), (
                    attempts
                )
                transcripts[scenario] = client.finish()
        return TranscriptWithCompanions(
            transcripts["failed"],
            {"retry.yaml": McpTranscript(transcripts["retry"])},
        )


@requires(POSIX, R, command("ir"))
def test_library_path_preparation_omits_default_packages(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env, record = recording_ir_environment(root)
        with McpClient(
            binary,
            DIRECT.serve(
                "--no-config",
                "-c",
                "r.packages=[]",
                "-c",
                f"python={json.dumps(sys.executable)}",
            ),
            env,
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("[1] 2\n", r="1+1")
            invocations = ir_run_records(record)
            assert invocations, invocations
            assert all(
                "--default-packages=NULL" in item["arguments"] for item in invocations
            ), invocations
            return client.finish()


@requires(command("rustc"), command("uv"))
def test_reuses_python_duckdb_preparation_and_rebuilds_changed_cache(
    binary: Path,
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "bin").mkdir()
        python, site = isolated_python(root)
        package = site / "duckdb"
        package.mkdir()
        engine = package / "__init__.py"
        engine.write_text(
            # fmt: python
            code("""
                import os
                from pathlib import Path


                class Connection:
                    def __init__(self, config):
                        self.directory = Path(config.get("extension_directory", "."))
                        if self.directory.name.startswith("mcp-console-duckdb-builtins-"):
                            record = Path(os.environ["DUCKDB_PREPARATION_RECORD"])
                            with record.open("a") as stream:
                                stream.write("prepared\\n")

                    def execute(self, sql):
                        if sql.startswith("SET extension_directory = "):
                            self.directory = Path(sql.split(" = ", 1)[1][1:-1].replace("''", "'"))
                        return self

                    def fetchall(self):
                        return []

                    def install_extension(self, name):
                        artifact = self.directory / "v1" / "fixture" / (name + ".duckdb_extension")
                        artifact.parent.mkdir(parents=True, exist_ok=True)
                        artifact.write_text("prepared extension")

                    def close(self):
                        pass


                def connect(*args, config=None, **kwargs):
                    return Connection(config or {})
                """),
            encoding="utf-8",
        )
        metadata = site / "duckdb-1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            "Metadata-Version: 2.3\nName: duckdb\nVersion: 1.0\n",
            encoding="utf-8",
        )
        (metadata / "RECORD").write_text("duckdb/__init__.py,,\n", encoding="utf-8")
        probing_uv(root, python, "")
        cache = root / "extensions"
        record = root / "preparations"
        env = environment(root / "bin") | {
            "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY": str(cache),
            "DUCKDB_PREPARATION_RECORD": str(record),
        }
        transcripts = {}
        for scenario, expected in (
            ("initial", 1),
            ("reused", 1),
            ("malformed", 2),
            ("removed", 3),
            ("package_changed", 4),
        ):
            if scenario == "malformed":
                receipts = list(cache.glob(".mcp-console-python-duckdb.json"))
                assert receipts
                for receipt in receipts:
                    receipt.write_bytes(b"\xff")
            if scenario == "removed":
                next(cache.rglob("sqlite.duckdb_extension")).unlink()
            if scenario == "package_changed":
                engine.write_text(engine.read_text() + "\n# changed fixture engine\n")
            workspace = root / scenario
            workspace.mkdir()
            with McpClient(
                binary,
                DIRECT.serve("--no-config"),
                env,
                current_directory=workspace,
            ) as client:
                client.transcript.append(
                    {
                        "scenario": scenario,
                        "fixture": "No R; seeded Python with a recording DuckDB extension provider.",
                    }
                )
                client.initialize_and_list_tools()
                client.expect(python="value = 1")
                prepared = record.read_text().splitlines()
                assert prepared == ["prepared"] * expected, (
                    scenario,
                    prepared,
                    expected,
                )
                transcripts[scenario] = client.finish()
        return TranscriptWithCompanions(
            transcripts.pop("initial"),
            {
                f"{name}.yaml": McpTranscript(transcript)
                for name, transcript in transcripts.items()
            },
        )


def _r_duckdb_fixture(root: Path) -> tuple[dict[str, str], Path, Path, Path]:
    env, _ = recording_ir_environment(root)
    env.update(
        IR_CACHE_DIR=str(root / "ir-cache"),
        IR_LIBRARY_ROOT=str(root / "prepared-libraries"),
    )
    arguments = [
        shutil.which("ir"),
        "run",
        "--rscript",
        str(Path(env["R_HOME"]) / "bin/Rscript"),
    ]
    for package in (
        "DBI",
        "duckdb",
        "jsonlite",
        "nanoarrow",
        "pillar",
        "reticulate",
        "tibble",
        "utf8",
    ):
        arguments += ["--with", package]
    arguments += [
        "--isolated",
        "--vanilla",
        "--default-packages=NULL",
        "-e",
        "cat(.libPaths()[[1]])",
    ]
    # Match Console's preparation launch: pak's asynchronous subprocess uses
    # the existing resolver readiness budget and cannot source the checkout's
    # .Rprofile through its inherited working directory.
    base = subprocess.check_output(
        arguments,
        env=env | {"IR_NO_LOCAL_SOURCES": "1", "PKG_SUBPROCESS_TIMEOUT": "60000"},
        cwd=root,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    base_library = root / "base-library"
    base_library.write_text(base)
    custom = root / "custom"
    custom.mkdir()
    shutil.copytree(Path(base) / "duckdb", custom / "duckdb")
    loader = custom / "duckdb/R/duckdb"
    with loader.open("a") as stream:
        stream.write(
            '\nif (!nzchar(Sys.getenv("MCP_CONSOLE_LOCAL_RUNTIME"))) '
            'cat("prepared\\n", file = Sys.getenv("DUCKDB_PREPARATION_RECORD"), append = TRUE)\n'
        )
    cache = root / "extensions"
    record = root / "preparations"
    env.update(
        MCP_CONSOLE_TEST_IR_SOURCE_LIBRARIES=str(custom),
        MCP_CONSOLE_TEST_IR_LIBRARY_ROOT=str(root / "libraries"),
        MCP_CONSOLE_TEST_IR_BASE_LIBRARY=str(base_library),
        MCP_CONSOLE_TEST_IR_FIXTURE_PACKAGES="duckdb",
        MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY=str(cache),
        DUCKDB_PREPARATION_RECORD=str(record),
    )
    return env, cache, record, loader


@requires(POSIX, R, command("ir"))
def test_reuses_r_duckdb_preparation_and_rebuilds_changed_cache(
    binary: Path,
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env, cache, record, loader = _r_duckdb_fixture(root)
        transcripts = {}
        for scenario, expected in (
            ("initial", 1),
            ("reused", 1),
            ("removed", 2),
            ("package_changed", 3),
        ):
            if scenario == "removed":
                next(cache.rglob("sqlite_scanner.duckdb_extension")).unlink()
            if scenario == "package_changed":
                loader.write_text(loader.read_text() + "\n# changed fixture engine\n")
            workspace = root / scenario
            workspace.mkdir()
            with McpClient(
                binary,
                DIRECT.serve(
                    "--no-config",
                    "-c",
                    "r.packages=[]",
                    "-c",
                    f"python={json.dumps(sys.executable)}",
                ),
                env,
                current_directory=workspace,
            ) as client:
                client.transcript.append(
                    {
                        "scenario": scenario,
                        "fixture": "Copied managed DuckDB records namespace loads during preparation only.",
                    }
                )
                client.initialize_and_list_tools()
                client.expect("[1] 2\n", r="1+1")
                prepared = record.read_text().splitlines()
                assert prepared == ["prepared"] * expected, (
                    scenario,
                    prepared,
                    expected,
                )
                transcripts[scenario] = client.finish()
        return TranscriptWithCompanions(
            transcripts.pop("initial"),
            {
                f"{name}.yaml": McpTranscript(transcript)
                for name, transcript in transcripts.items()
            },
        )
