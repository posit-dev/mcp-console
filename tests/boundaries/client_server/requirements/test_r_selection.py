"""Installed R selection survives configuration layering and worker replacement."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.r import isolated_r_home, r_test_environment
from support.normalization import code
from support.records import Transcript
from support.requirements import NON_UTF8_FILENAMES, POSIX, R, command, requires
from support.resolvers import (
    bare_runtime_environment,
    recording_ir_environment,
    ir_run_records,
)
from support.suites import run_this_suite


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_installed_r_selection_and_layering(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    # The home-relative path uses the real HOME, preserving the caller's tool environment.
    with TemporaryDirectory(dir=Path.home()) as temporary:
        root = Path(temporary).resolve()
        environment, record = recording_ir_environment(root)
        original = Path(environment["R_HOME"])
        installed = isolated_r_home(root, environment)
        (root / "chosen R").symlink_to(installed / "bin/R")
        environment.update(
            R_HOME="stale inherited home",
            RHOME="stale inherited home",
            RETICULATE_PYTHON=sys.executable,
            R_SHARE_DIR="stale share",
            R_INCLUDE_DIR="stale include",
            R_DOC_DIR="stale doc",
        )
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        variants = [
            ("chosen R", ("r.vanilla=true",), True),
            ({"vanilla": True}, ("r=./chosen R",), True),
            ({"vanilla": True}, ("r=null", "r=./chosen R"), False),
            (
                {
                    "executable": "~/"
                    + str((root / "chosen R").relative_to(Path.home()))
                },
                (),
                False,
            ),
            ({"executable": str(original / "bin/exec/R")}, (), False),
        ]
        for selection, overrides, vanilla in variants:
            record.write_text("")
            config.write_text(json.dumps({"r": selection}))
            with McpClient(
                binary,
                execution.serve(
                    "-c",
                    "cache=host",
                    *(item for override in overrides for item in ("-c", override)),
                ),
                environment,
                root,
            ) as client:
                client.initialize_and_list_tools()
                expected_home = (
                    original
                    if isinstance(selection, dict)
                    and selection.get("executable", "").endswith("bin/exec/R")
                    else installed
                )
                program = (
                    # fmt: r
                    code(r"""
                        stopifnot(
                          identical(normalizePath(R.home()), normalizePath(EXPECTED_HOME)),
                          identical(Sys.getenv("RHOME"), R.home()),
                          identical("--vanilla" %in% commandArgs(), VANILLA)
                        )
                        Sys.setenv(R_HOME = "changed", RHOME = "changed")
                        cat("selected installation\n")
                        """)
                    .replace("EXPECTED_HOME", json.dumps(str(expected_home)))
                    .replace("VANILLA", "TRUE" if vanilla else "FALSE")
                )
                client.expect("selected installation\n", r=program)
                runs = ir_run_records(record)
                assert runs and all(
                    run["arguments"][run["arguments"].index("--rscript") + 1]
                    == str(expected_home / "bin/Rscript")
                    for run in runs
                ), runs
                client.send(control="restart")
                client.expect("selected installation\n", r=program)
                assert ir_run_records(record) == runs
                client.finish()
            records.append(
                {
                    "selection": selection
                    if isinstance(selection, str)
                    else list(selection),
                    "vanilla": vanilla,
                    "matching_installation_after_restart": True,
                }
            )
        return records


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_r_shorthand_merges_across_file_layers(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, rscript = r_test_environment()
        (root / "chosen R").symlink_to(rscript.with_name("R"))
        console_home = root / "console-home"
        console_home.mkdir()
        (console_home / "config.yaml").write_text(json.dumps({"r": "./chosen R"}))
        project = root / ".agents/console/config.yaml"
        project.parent.mkdir(parents=True)
        project.write_text(json.dumps({"r": {"vanilla": True}}))
        environment["MCP_CONSOLE_HOME"] = str(console_home)
        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            root,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "42\n",
                r='stopifnot("--vanilla" %in% commandArgs()); cat(42, "\\n", sep="")',
            )
            return client.finish()


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_missing_selected_r_can_be_repaired(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, rscript = r_test_environment()
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"r": "./chosen R"}))
        with McpClient(
            binary, execution.serve("-c", "cache=host"), environment, root
        ) as client:
            client.initialize_and_list_tools()
            failed = client.send(r='cat("must not run\\n")')
            assert failed.get("isError") and "r.executable" in str(failed), failed
            (root / "chosen R").symlink_to(rscript.with_name("R"))
            # Retry retains the captured path even if the project file is edited.
            config.write_text(json.dumps({"r": "./another R"}))
            client.send(control="restart")
            client.expect("42\n", r="cat(42, '\\n', sep='')")
            client.finish()
            return json.loads(
                json.dumps(
                    [{"initial_error": failed, "repaired_captured_selection": True}]
                ).replace(str(root), "<workspace>")
            )


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_changed_selected_launcher_is_rejected(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, rscript = r_test_environment()
        selected = root / "chosen R"
        selected.write_text(
            "#!/bin/sh\nexec " + shlex.quote(str(rscript.with_name("R"))) + ' "$@"\n'
        )
        selected.chmod(0o755)
        with McpClient(
            binary,
            execution.serve("-c", "cache=host", "-c", "r.executable=./chosen R"),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", r="cat(42, '\\n', sep='')")
            before = selected.stat()
            source = selected.read_bytes()
            selected.write_bytes(source.replace(b"exec ", b"exec\t", 1))
            os.utime(selected, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert selected.stat().st_size == before.st_size
            assert selected.stat().st_mtime_ns == before.st_mtime_ns
            failed = client.send(control="restart", r='cat("must not run\\n")')
            assert failed.get("isError") and "selected R installation changed" in str(
                failed
            ), failed
            client.finish()
            return json.loads(
                json.dumps([{"restart_error": failed}]).replace(
                    str(root), "<workspace>"
                )
            )


@requires(POSIX, R)
@executions(SANDBOXED)
def test_r_launcher_inspection_uses_worker_permissions(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, rscript = r_test_environment()
        selected = root / "chosen R"
        marker = root / "resolver-only" / "probe"
        marker.parent.mkdir()
        selected.write_text(
            "#!/bin/sh\nif ! (printf probe > "
            + shlex.quote(str(marker))
            + ") 2>/dev/null; then\n  echo 'probe cannot write resolver-only directory' >&2\n  exit 1\nfi\nexec "
            + shlex.quote(str(rscript.with_name("R")))
            + ' "$@"\n'
        )
        selected.chmod(0o755)
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "r": str(selected),
                    "resolver": {
                        "sandbox": {"filesystem": {"read_write": [str(marker.parent)]}}
                    },
                }
            )
        )
        with McpClient(
            binary, execution.serve("-c", "cache=host"), environment, root
        ) as client:
            client.initialize_and_list_tools()
            failed = client.send(r='cat("must not run\\n")')
            assert failed.get("isError") and "R installation inspection failed" in str(
                failed
            ), failed
            assert not marker.exists(), "inspection used resolver permissions"
            _, stderr = client.finish_with_standard_error(expected_exit_status=1)
            assert "R installation inspection failed" in stderr, stderr
            return json.loads(
                json.dumps(
                    [
                        {
                            "inspection_error": failed,
                            "stderr": stderr,
                            "resolver_grant_did_not_reach_inspection": True,
                        }
                    ]
                ).replace(str(root), "<workspace>")
            )


@requires(POSIX, R)
@executions(DIRECT)
def test_r_inspection_uses_worker_temporary_storage(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        host_temp = root / "host-temp"
        host_temp.mkdir()
        environment, rscript = r_test_environment()
        selected = root / "chosen R"
        selected.write_text(
            '#!/bin/sh\nset -e\nscratch=$(mktemp -d "$TMPDIR/mcp-console-probe.XXXXXX")\nrmdir "$scratch"\nexec '
            + shlex.quote(str(rscript.with_name("R")))
            + ' "$@"\n'
        )
        selected.chmod(0o755)
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "cache=host",
                "-c",
                "r.executable=./chosen R",
                "-c",
                "environment.TMPDIR=" + str(host_temp),
                "-c",
                "resolver.environment.TMPDIR=" + gettempdir(),
            ),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", r="cat(42, '\\n', sep='')")
            client.finish()
            return [{"R_inspection_uses_worker_temporary_storage": True}]


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_changed_rscript_is_rejected_before_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = recording_ir_environment(root)
        installed = isolated_r_home(root, environment)
        rscript = installed / "bin/Rscript"
        # Retain its location in bin for relocatable R installations.
        original = rscript.with_name("Rscript-original")
        rscript.rename(original)
        rscript.write_text("#!/bin/sh\nexec " + shlex.quote(str(original)) + ' "$@"\n')
        rscript.chmod(0o755)
        marker = root / "changed-rscript-executed"
        with McpClient(
            binary,
            execution.serve("-c", "cache=host", "-c", "r.executable=./R/bin/R"),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", r="cat(42, '\\n', sep='')")
            rscript.write_text(
                "#!/bin/sh\nprintf executed > "
                + shlex.quote(str(marker))
                + "\nexec "
                + shlex.quote(str(original))
                + ' "$@"\n'
            )
            failed = client.send(requirements={"r": ["jsonlite", "digest"]})
            assert not marker.exists(), "changed Rscript executed during preparation"
            assert failed.get("isError") and "selected R installation changed" in str(
                failed
            ), failed
            return json.loads(
                json.dumps(client.finish()).replace(str(root), "<workspace>")
            )


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_resource_directory_replacement_is_rejected(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, rscript = r_test_environment()
        original = Path(
            subprocess.check_output(
                [rscript, "--vanilla", "-e", 'cat(R.home("doc"))'],
                env=environment,
                text=True,
            )
        )
        installed = isolated_r_home(root, environment)
        resource = installed / "doc"
        resource.unlink(missing_ok=True)
        resource.mkdir()
        for entry in original.iterdir():
            (resource / entry.name).symlink_to(entry)
        launcher = installed / "bin/R"
        source, count = re.subn(
            r"(?m)^R_DOC_DIR=.*$",
            "R_DOC_DIR=" + shlex.quote(str(resource)),
            launcher.read_text(),
            count=1,
        )
        assert count == 1, "R launcher must declare R_DOC_DIR"
        launcher.write_text(source)
        with McpClient(
            binary,
            execution.serve("-c", "cache=host", "-c", "r.executable=./R/bin/R"),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", r="cat(42, '\\n', sep='')")
            # Ordinary directory contents can change without changing selection.
            (resource / "unrelated").write_text("allowed")
            client.send(control="restart")
            client.expect("42\n", r="cat(42, '\\n', sep='')")
            resource.rename(installed / "retained-doc")
            resource.write_text("a file cannot replace the directory")
            failed = client.send(control="restart", r='cat("must not run\\n")')
            assert failed.get(
                "isError"
            ) and "selected R resource directory changed" in str(failed), failed
            return json.loads(
                json.dumps(client.finish()).replace(str(root), "<workspace>")
            )


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_selected_launcher_in_native_directory(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve() / "workspace-\u00e9"
        root.mkdir()
        environment, rscript = r_test_environment()
        (root / "chosen R").symlink_to(rscript.with_name("R"))
        with McpClient(
            binary,
            execution.serve("-c", "cache=host", "-c", "r.executable=./chosen R"),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", r="cat(42, '\\n', sep='')")
            client.send(control="restart")
            client.expect("42\n", r="cat(42, '\\n', sep='')")
            return json.loads(
                json.dumps(client.finish()).replace(str(root), "<workspace>")
            )


@requires(NON_UTF8_FILENAMES, R)
@executions(DIRECT)
def test_selected_launcher_in_non_utf8_directory(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve() / os.fsdecode(b"workspace-\xff")
        root.mkdir()
        environment, rscript = r_test_environment()
        library = Path(temporary).resolve() / "library"
        library.mkdir()
        environment = bare_runtime_environment(environment, library)
        (root / "chosen R").symlink_to(rscript.with_name("R"))
        with McpClient(
            binary,
            execution.serve("-c", "cache=host", "-c", "r.executable=./chosen R"),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", r="cat(42, '\\n', sep='')")
            client.send(control="restart")
            client.expect("42\n", r="cat(42, '\\n', sep='')")
            transcript, stderr = client.finish_with_standard_error()
            # R selection preserves native filenames; Quarto's existing
            # UTF-8 execution-root requirement still disables projections.
            assert stderr == (
                "mcp-console: transcript projections disabled: "
                "Quarto execution root requires a UTF-8 working directory\n"
            ), stderr
            return [*transcript, {"stderr": stderr}]


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_selected_r_home_preserves_trailing_whitespace(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = r_test_environment()
        previous = isolated_r_home(root, environment)
        installed = root / "R home \t\r"
        previous.rename(installed)
        launcher = installed / "bin/R"
        launcher.write_text(
            launcher.read_text().replace(
                shlex.quote(str(previous)), shlex.quote(str(installed))
            )
        )
        environment.update(R_HOME=str(installed), RHOME=str(installed))
        with McpClient(
            binary,
            execution.serve(
                "-c", "cache=host", "-c", "r.executable=" + json.dumps(str(launcher))
            ),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "selected home\n",
                r="stopifnot(identical(R.home(), "
                + json.dumps(str(installed))
                + ')); cat("selected home\\n")',
            )
            return json.loads(
                json.dumps(client.finish()).replace(str(root), "<workspace>")
            )


if __name__ == "__main__":
    run_this_suite(__file__)
