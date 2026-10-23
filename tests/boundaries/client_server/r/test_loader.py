#!/usr/bin/env -S uv run --script

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from contextlib import ExitStack

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed
from support.requirements import LINUX_NATIVE, NON_UTF8_FILENAMES, POSIX, requires
from support.assertions import collect_running_output, tool_text
from support.client import McpClient
from support.checkpoints import FifoCheckpoint
from support.normalization import code
from support.r import isolated_r_home, r_test_environment
from support.records import Transcript
from support.resolvers import bare_runtime_environment
from support.execution import DIRECT
from support.suites import run_this_suite


@requires(POSIX)
@requires(LINUX_NATIVE)
def test_loads_native_libraries_from_selected_r_home(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
        root = Path(directory)
        # Native loader coverage needs only base R, without package resolution.
        library = root / "library"
        library.mkdir()
        environment = bare_runtime_environment(environment, library)
        gate = FifoCheckpoint.create(root / "loader-release")
        resources.callback(gate.close)
        selected = isolated_r_home(root, environment)
        libraries = selected / "lib"
        inherited = root / "inherited"
        inherited.mkdir()
        for name, location, value in (
            ("mcp_r_home_probe", libraries, 20),
            ("mcp_inherited_probe", inherited, 22),
        ):
            source = root / f"{name}.c"
            source.write_text(
                code(f"""
                int {name}(void) {{
                    return {value};
                }}
                """)
            )
            subprocess.run(
                ["cc", "-shared", "-fPIC", "-o", location / f"lib{name}.so", source],
                check=True,
                capture_output=True,
            )
        source = root / "loader_probe.c"
        source.write_text(
            code("""
            extern int mcp_r_home_probe(void);
            extern int mcp_inherited_probe(void);
            void loader_probe(int *result) {
                *result = mcp_r_home_probe() + mcp_inherited_probe();
            }
            """)
        )
        subprocess.run(
            [
                "cc",
                "-shared",
                "-fPIC",
                "-o",
                root / "loader_probe.so",
                source,
                "-L",
                libraries,
                "-lmcp_r_home_probe",
                "-L",
                inherited,
                "-lmcp_inherited_probe",
            ],
            check=True,
            capture_output=True,
        )
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(
            [str(inherited), *filter(None, [environment.get("LD_LIBRARY_PATH")])]
        )
        with McpClient(
            binary, DIRECT.serve(), environment, current_directory=root
        ) as client:
            client.initialize_and_list_tools()
            # Complete silent setup before timing the library-loading cell.
            ready = client.send(r="invisible(NULL)", timeout_ms=600_000)
            assert tool_text(ready) == "[done]", ready
            client.transcript.pop()
            result = client.send(
                # fmt: r
                r=code("""
                    dyn.load("loader_probe.so")
                    local({
                      con <- fifo("loader-release", "rb", blocking = TRUE)
                      on.exit(close(con))
                      stopifnot(identical(readBin(con, "raw", 1L), charToRaw("1")))
                    })
                    .C("loader_probe", result = 0L)$result
                    """),
                timeout_ms=0,
            )
            assert (
                without_elapsed(tool_text(result))
                == "\n[running; poll with an empty send]"
            ), result
            gate.release()
            output = collect_running_output(
                client, "R loader probe", timeouts_ms=(60_000,) * 8
            )
            assert "".join(output) == "[1] 42\n", output
            return client.finish()


@requires(NON_UTF8_FILENAMES)
def test_preserves_non_utf8_r_home(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    original = Path(environment["R_HOME"])
    with tempfile.TemporaryDirectory() as directory:
        library = Path(directory) / "library"
        library.mkdir()
        environment = bare_runtime_environment(environment, library)
        selected = Path(directory) / os.fsdecode(b"R-home-\xff")
        selected.symlink_to(original, target_is_directory=True)
        environment["R_HOME"] = str(selected)
        # R requires a byte-oriented locale for paths outside UTF-8.
        environment["LC_ALL"] = "C"
        # Native sandbox policy requires UTF-8 environment values; exercise
        # the direct launch contract that supports arbitrary Unix path bytes.
        with McpClient(binary, DIRECT.serve(), environment) as client:
            client.initialize_and_list_tools()
            for control in ({}, {"control": "restart"}):
                client.send(
                    **control,
                    # fmt: r
                    r=code("""
                        stopifnot(as.raw(255) %in% charToRaw(Sys.getenv("R_HOME")))
                        42L
                        """),
                )
                assert "[1] 42\n" in tool_text(client.transcript[-1]["result"]), (
                    client.transcript[-1]
                )
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
