"""Dependency activation preserves native R project library ownership."""

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.requirements.test_r_automatic import (
    recording_fixture_r_environment,
)
from boundaries.client_server.python.test_peer_runtime import (
    DEFER_R_STARTUP,
    defer_r_bootstrap,
)
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import (
    POSIX,
    R,
    SANDBOX,
    command,
    posix_r_tempdir_whitespace,
    requires,
)
from support.resolvers import bare_runtime_environment, ir_run_records, ir_requirements
from support.suites import run_this_suite


@requires(R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_replaces_console_layer_at_its_native_profile_position(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        project_library = root / "project-library"
        project_library.mkdir()
        environment, _ = r_test_environment()
        profile = root / ".Rprofile"
        profile.write_text('.libPaths(c("project-library", .libPaths()))\n')
        environment.update(R_PROFILE_USER=str(profile), RETICULATE_PYTHON="")
        with McpClient(
            binary, execution.serve(), environment, root, use_r_startup_files=True
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "[done]",
                # fmt: r
                r=code(r"""
                    native_paths <- .libPaths()
                    managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 3L else 2L
                    initial_library <- native_paths[[managed_index]]
                    stopifnot(identical(
                      native_paths[[managed_index - 1L]],
                      normalizePath("project-library", winslash = "/")
                    ))
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["praise"]})
            client.expect(
                "project order retained\n",
                # fmt: r
                r=code(r"""
                    paths <- .libPaths()
                    stopifnot(
                      !initial_library %in% paths,
                      identical(paths[-managed_index], native_paths[-managed_index]),
                      identical(dirname(find.package("praise")), paths[[managed_index]])
                    )
                    cat("project order retained\n")
                    """),
            )
            return client.finish()


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_duplicate_managed_library_keeps_native_search_order(
    binary: Path, execution: Execution
) -> Transcript:
    return unchanged_library_order(binary, execution, "${R_LIBS}")


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_symlink_to_managed_library_keeps_native_search_order(
    binary: Path, execution: Execution
) -> Transcript:
    return unchanged_library_order(
        binary, execution, "project-root${MCP_CONSOLE_R_LIBRARY}"
    )


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_symlink_to_prepared_library_keeps_native_search_order(
    binary: Path, execution: Execution
) -> Transcript:
    return unchanged_library_order(
        binary,
        execution,
        "project-root${MCP_CONSOLE_R_LIBRARY}",
        start_after_preparation_in=".",
    )


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_later_absolute_alias_keeps_native_search_order_after_chdir(
    binary: Path, execution: Execution
) -> Transcript:
    return unchanged_library_order(
        binary,
        execution,
        "${PROJECT_ROOT}/project-root${MCP_CONSOLE_R_LIBRARY}",
        start_after_preparation_in="child",
    )


def unchanged_library_order(
    binary: Path,
    execution: Execution,
    project_entry: str,
    *,
    start_after_preparation_in: str | None = None,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = recording_fixture_r_environment(
            root, ("consolefirst", "consolesecond")
        )
        (root / "project-lib").mkdir()
        (root / "project-root").symlink_to("/")
        environ = root / ".Renviron"
        environ.write_text(
            'ORIGINAL_LIBRARY="${MCP_CONSOLE_R_LIBRARY}"\n'
            'R_LIBS="${R_LIBS}:project-lib:' + project_entry + '"\n'
        )
        environment.update(R_ENVIRON_USER=str(environ), PROJECT_ROOT=str(root))
        if start_after_preparation_in is not None:
            if start_after_preparation_in != ".":
                child = root / start_after_preparation_in
                child.mkdir()
                (child / "project-lib").symlink_to(root / "project-lib")
            modules = root / "modules"
            modules.mkdir()
            (modules / "sitecustomize.py").write_text(DEFER_R_STARTUP)
            environment.update(
                RETICULATE_PYTHON=sys.executable, RETICULATE_PYTHONPATH=str(modules)
            )
        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.transcript.append(
                {"fixture": {".Renviron": environ.read_text().strip()}}
            )
            if start_after_preparation_in is not None:
                defer_r_bootstrap(client)
                client.expect(python="pass")
                client.expect("[prepared]", requirements={"r": ["consolefirst"]})
                if start_after_preparation_in != ".":
                    client.expect(
                        python=f"import os; os.chdir({start_after_preparation_in!r})"
                    )
            client.expect(
                # fmt: r
                r=code("""
                    original <- normalizePath(Sys.getenv("ORIGINAL_LIBRARY"))
                    project <- normalizePath("project-lib")
                    paths <- .libPaths()
                    stopifnot(identical(
                      paths[paths %in% c(original, project)],
                      c(original, project)
                    ))
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consolesecond"]})
            client.expect(
                # fmt: r
                r=code("""
                    paths <- .libPaths()
                    stopifnot(
                      identical(paths[paths %in% c(original, project)], c(original, project)),
                      consolesecond::fixture()
                    )
                    """),
            )
            return client.finish()


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_preserves_project_copy_of_candidate_across_requirement_changes(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = recording_fixture_r_environment(
            root, ("consoleproject", "consolemissing")
        )
        project_library = root / "resolved-r-libraries/consoleproject"
        project_library.mkdir(parents=True)
        (root / "project-extra").mkdir()
        profile = root / ".Rprofile"
        profile.write_text(
            '.libPaths(c(.libPaths(), "resolved-r-libraries/consoleproject", "project-extra"))\n'
        )
        environment["R_PROFILE_USER"] = str(profile)
        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "[done]",
                # fmt: r
                r=code(r"""
                    native_paths <- .libPaths()
                    initial_library <- dirname(find.package("jsonlite"))
                    project_paths <- native_paths[!native_paths %in% initial_library]
                    project_library <- normalizePath(
                      "resolved-r-libraries/consoleproject",
                      winslash = "/"
                    )
                    stopifnot(project_library %in% project_paths)
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consoleproject"]})
            client.expect(
                "[prepared]",
                requirements={"r": ["consoleproject", "consolemissing"]},
            )
            client.expect(
                "project library retained across changes\n",
                # fmt: r
                r=code(r"""
                    paths <- .libPaths()
                    stopifnot(
                      !initial_library %in% paths,
                      all(project_paths %in% paths),
                      identical(paths[paths %in% project_paths], project_paths),
                      consoleproject::fixture(),
                      consolemissing::fixture()
                    )
                    cat("project library retained across changes\n")
                    """),
            )
            return client.finish()


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_renviron_reselects_a_former_managed_library(
    binary: Path, execution: Execution
) -> Transcript:
    return reselect_project_library(binary, execution, relative_alias=False)


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_renviron_selects_the_absolute_path_in_place_of_a_relative_alias(
    binary: Path, execution: Execution
) -> Transcript:
    return reselect_project_library(binary, execution, relative_alias=True)


def reselect_project_library(
    binary: Path, execution: Execution, *, relative_alias: bool
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = recording_fixture_r_environment(
            root, ("consolefirst", "consolesecond")
        )
        modules = root / "modules"
        modules.mkdir()
        (modules / "sitecustomize.py").write_text(DEFER_R_STARTUP)
        environ = root / ".Renviron"
        environ.write_text('R_LIBS="${PROJECT_LIBRARY}:${MCP_CONSOLE_R_LIBRARY}"\n')
        environment.update(
            RETICULATE_PYTHON=sys.executable,
            RETICULATE_PYTHONPATH=str(modules),
            R_ENVIRON_USER=str(environ),
        )
        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            defer_r_bootstrap(client)
            client.expect(
                # fmt: python
                python=code("""
                    import os

                    old = os.environ["MCP_CONSOLE_R_LIBRARY"]
                    os.environ["PROJECT_LIBRARY"] = old
                    """),
            )
            if relative_alias:
                client.expect(
                    python='os.environ["R_LIBS"] = os.pathsep.join((old, os.path.relpath(old)))'
                )
            else:
                client.expect(
                    python='os.environ["R_LIBS"] = os.pathsep.join((old, old))'
                )
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect(
                # fmt: r
                r=code("""
                    old <- normalizePath(Sys.getenv("PROJECT_LIBRARY"))
                    new <- dirname(find.package("consolefirst"))
                    paths <- .libPaths()
                    stopifnot(identical(paths[paths %in% c(old, new)], c(old, new)))
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consolesecond"]})
            client.expect(
                # fmt: r
                r=code("""
                    stopifnot(old %in% .libPaths(), !new %in% .libPaths(), consolesecond::fixture())
                    """),
            )
            return client.finish()


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_replaces_library_prepared_before_r_initialization(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = recording_fixture_r_environment(
            root, ("consolefirst", "consolesecond")
        )
        modules = root / "modules"
        modules.mkdir()
        (modules / "sitecustomize.py").write_text(DEFER_R_STARTUP)
        environ = root / ".Renviron"
        environ.write_text('R_LIBS_USER="${R_LIBS}"\nR_LIBS=""\n')
        profile = root / ".Rprofile"
        profile.write_text(
            'writeLines("started", file.path(Sys.getenv("TMPDIR"), "r-started"))\n'
        )
        environment.update(
            RETICULATE_PYTHON=sys.executable,
            RETICULATE_PYTHONPATH=str(modules),
            R_ENVIRON_USER=str(environ),
            R_PROFILE_USER=str(profile),
            TMPDIR=str(root),
        )
        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            defer_r_bootstrap(client)
            client.expect(
                "[done]",
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path

                    r_started = Path(os.environ["TMPDIR"]) / "r-started"
                    assert not r_started.exists()
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect("[done]", python="assert not r_started.exists()")
            client.expect(
                "[done]",
                # fmt: r
                r=code(r"""
                    native_paths <- .libPaths()
                    user_paths <- strsplit(Sys.getenv("R_LIBS_USER"), .Platform$path.sep)[[1L]]
                    prepared_library <- dirname(find.package("consolefirst"))
                    stopifnot(
                      identical(Sys.getenv("R_LIBS"), ""),
                      prepared_library %in% user_paths,
                      prepared_library %in% native_paths
                    )
                    """),
            )
            client.expect(
                "[done]", python='assert r_started.read_text() == "started\\n"'
            )
            client.expect(
                "[prepared]",
                requirements={"r": ["consolefirst", "consolesecond"]},
            )
            client.expect(
                "pre-initialization library replaced\n",
                # fmt: r
                r=code(r"""
                    paths <- .libPaths()
                    replacement <- dirname(find.package("consolesecond"))
                    stopifnot(
                      !prepared_library %in% paths,
                      identical(
                        paths[!paths %in% replacement],
                        native_paths[!native_paths %in% prepared_library]
                      ),
                      consolefirst::fixture(),
                      consolesecond::fixture()
                    )
                    cat("pre-initialization library replaced\n")
                    """),
            )
            return client.finish()


@requires(SANDBOX, command("ir"), posix_r_tempdir_whitespace(accepted=False))
def test_preparation_preserves_renviron_libraries_while_r_startup_is_paused(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory(dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        environment, _ = recording_fixture_r_environment(
            root, ("consolefirst", "consoleproject", "consolesecond")
        )
        for name in ("private temp", "project-last"):
            (root / name).mkdir()
        (root / "resolved-r-libraries/consolefirst-consoleproject").mkdir(parents=True)
        environ = root / ".Renviron"
        environ.write_text(
            f'TMPDIR="{root}/private temp"\n'
            'R_LIBS="project-first:${MCP_CONSOLE_R_LIBRARY}:'
            'resolved-r-libraries/consolefirst-consoleproject:project-last"\n'
        )
        environment.update(
            R_ENVIRON_USER=str(environ), RETICULATE_PYTHON=sys.executable
        )
        with McpClient(
            binary,
            ("serve", "--writable-root", str(root), "-c", "cache=host"),
            environment,
            root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                # fmt: python
                python=code("""
                    import os
                    import subprocess
                    from pathlib import Path

                    Path("project-first").symlink_to(os.environ["MCP_CONSOLE_R_LIBRARY"])
                    answer = 42
                    """)
            )
            output = client.expect(None, r="1L")["content"][0]["text"]
            assert "R requires a temporary directory without whitespace" in output, (
                output
            )
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect(
                "[done]",
                # fmt: python
                python=code("""
                    paths = (
                        subprocess.check_output(["printenv", "R_LIBS"], text=True).strip().split(os.pathsep)
                    )
                    assert [Path(path).name for path in paths] == [
                        "project-first",
                        "consolefirst",
                        "consolefirst-consoleproject",
                        "project-last",
                    ], paths
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consoleproject"]})
            client.expect("[prepared]", requirements={"r": ["consolesecond"]})
            client.expect(
                "42\n", python='os.environ["TMPDIR"] = os.getcwd(); print(answer)'
            )
            client.expect(
                "project libraries retained\n",
                # fmt: r
                r=code(r"""
                    paths <- .libPaths()
                    stopifnot(
                      identical(
                        paths[2:5],
                        normalizePath(c(
                          "resolved-r-libraries/consolefirst-consoleproject-consolesecond",
                          "project-first",
                          "resolved-r-libraries/consolefirst-consoleproject",
                          "project-last"
                        ))
                      ),
                      !"consolefirst" %in% basename(paths),
                      consoleproject::fixture(),
                      consolesecond::fixture()
                    )
                    cat("project libraries retained\n")
                    """),
            )
            return client.finish()


@contextmanager
def paused_project_library(
    binary: Path,
    root: Path,
    project_entry: str,
    *,
    library_root: str = "resolved-r-libraries",
) -> Iterator[McpClient]:
    environment, _ = recording_fixture_r_environment(
        root, ("consolefirst", "consolesecond")
    )
    environment["MCP_CONSOLE_TEST_IR_LIBRARY_ROOT"] = str(root / library_root)
    for name in ("private temp", "project-first", "project-last"):
        (root / name).mkdir()
    environ = root / ".Renviron"
    environ.write_text(
        'TMP="${TMPDIR}"\n'
        f'TMPDIR="{root}/private temp"\n'
        'R_LIBS_SITE="${MCP_CONSOLE_R_LIBRARY}"\n'
        'R_LIBS="${MCP_CONSOLE_R_LIBRARY}:'
        f'{root}/project-first:{project_entry}:{root}/project-last"\n'
    )
    environment.update(R_ENVIRON_USER=str(environ), RETICULATE_PYTHON=sys.executable)
    with McpClient(
        binary,
        (
            "serve",
            "--writable-root",
            str(root),
            "-c",
            "cache=host",
            "-c",
            "sandbox.tempdir.managed.parent=/tmp",
        ),
        environment,
        root,
        use_r_startup_files=True,
    ) as client:
        client.initialize_and_list_tools()
        output = client.expect(None, r="1L")["content"][0]["text"]
        assert "R requires a temporary directory without whitespace" in output, output
        yield client


@requires(SANDBOX, command("ir"), posix_r_tempdir_whitespace(accepted=False))
def test_later_project_alias_survives_two_preparations_before_r_startup(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory(dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        (root / "project-root").symlink_to("/")
        project_entry = str(root / "project-root") + "${MCP_CONSOLE_R_LIBRARY}"
        with paused_project_library(binary, root, project_entry) as client:
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect("[prepared]", requirements={"r": ["consolesecond"]})
            client.expect(python='import os; del os.environ["TMPDIR"]')
            client.expect(
                # fmt: r
                r=code("""
                    original <- normalizePath(Sys.getenv("R_LIBS_SITE"))
                    project <- normalizePath(c("project-first", "project-last"))
                    paths <- .libPaths()
                    stopifnot(
                      identical(paths[paths %in% c(original, project)], c(original, project)),
                      consolefirst::fixture(),
                      consolesecond::fixture()
                    )
                    """),
            )
            return client.finish()


@requires(SANDBOX, command("ir"), posix_r_tempdir_whitespace(accepted=False))
def test_later_glob_preserves_the_replaced_library_position(binary: Path) -> Transcript:
    with TemporaryDirectory(dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        (root / "libs").mkdir()
        with paused_project_library(binary, root, "libs/*") as client:
            client.expect(
                # fmt: python
                python=code("""
                    import os
                    import subprocess
                    from pathlib import Path

                    old = os.environ["MCP_CONSOLE_R_LIBRARY"]
                    Path("libs/pkg").symlink_to(old)
                    os.environ["R_LIBS_SITE"] = str(Path("project-last").resolve())
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect(
                # fmt: python
                python=code("""
                    paths = subprocess.check_output(["printenv", "R_LIBS"], text=True).strip()
                    os.environ["R_LIBS"] = paths + ":unmatched-*"
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consolesecond"]})
            client.expect(python='del os.environ["TMPDIR"]')
            client.expect(
                # fmt: r
                r=code("""
                    project <- normalizePath(c("libs/pkg", "project-first", "project-last"))
                    paths <- .libPaths()
                    stopifnot(
                      identical(paths[paths %in% project], project),
                      !normalizePath("resolved-r-libraries/consolefirst") %in% paths,
                      consolefirst::fixture(),
                      consolesecond::fixture()
                    )
                    """),
            )
            return client.finish()


@requires(SANDBOX, command("ir"), posix_r_tempdir_whitespace(accepted=False))
def test_replaced_library_expires_when_relative_alias_points_elsewhere(
    binary: Path,
) -> Transcript:
    return expire_replaced_library(binary, absolute_alias=False)


@requires(SANDBOX, command("ir"), posix_r_tempdir_whitespace(accepted=False))
def test_replaced_library_expires_when_absolute_symlink_is_retargeted(
    binary: Path,
) -> Transcript:
    return expire_replaced_library(binary, absolute_alias=True)


def expire_replaced_library(binary: Path, *, absolute_alias: bool) -> Transcript:
    with TemporaryDirectory(dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        (root / "child/local").mkdir(parents=True)
        alias = str(root / "local") if absolute_alias else "local"
        with paused_project_library(binary, root, alias) as client:
            client.expect(
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path

                    original = os.environ["MCP_CONSOLE_R_LIBRARY"]
                    Path("local").symlink_to(original)
                    os.environ["ORIGINAL_LIBRARY"] = original
                    os.environ["R_LIBS_SITE"] = str(Path("project-last").resolve())
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect("[prepared]", requirements={"r": ["consolesecond"]})
            if absolute_alias:
                client.expect(
                    # fmt: python
                    python=code("""
                        alias = Path("local")
                        alias.unlink()
                        alias.symlink_to("child/local")
                        """),
                )
            client.expect(
                # fmt: python
                python=code("""
                    os.chdir("child")
                    del os.environ["TMPDIR"]
                    """),
            )
            client.expect(
                # fmt: r
                r=code("""
                    project <- normalizePath(c("../project-first", "local", "../project-last"))
                    paths <- .libPaths()
                    stopifnot(
                      !normalizePath(Sys.getenv("ORIGINAL_LIBRARY")) %in% paths,
                      identical(paths[paths %in% project], project),
                      consolefirst::fixture(),
                      consolesecond::fixture()
                    )
                    """),
            )
            return client.finish()


@requires(SANDBOX, command("ir"), posix_r_tempdir_whitespace(accepted=False))
def test_prepared_relative_library_survives_a_directory_change(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory(dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        relative_library = "resolved-r-libraries/consolefirst"
        (root / "child" / relative_library).mkdir(parents=True)
        with paused_project_library(binary, root, relative_library) as client:
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect(
                # fmt: python
                python=code("""
                    import os

                    os.chdir("child")
                    del os.environ["TMPDIR"]
                    """)
            )
            client.expect(
                "prepared library available after chdir\n",
                # fmt: r
                r=code(r"""
                    root <- dirname(getwd())
                    candidate <- file.path(root, "resolved-r-libraries", "consolefirst")
                    project <- c(
                      file.path(root, "project-first"),
                      file.path(getwd(), "resolved-r-libraries", "consolefirst"),
                      file.path(root, "project-last")
                    )
                    paths <- .libPaths()
                    stopifnot(
                      basename(getwd()) == "child",
                      identical(dirname(find.package("consolefirst")), candidate),
                      identical(paths[paths %in% project], project),
                      consolefirst::fixture()
                    )
                    cat("prepared library available after chdir\n")
                    """),
            )
            return client.finish()


@requires(SANDBOX, command("ir"), posix_r_tempdir_whitespace(accepted=False))
def test_home_relative_project_library_survives_native_activation(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory(dir=Path.home()) as temporary:
        root = Path(temporary).resolve()
        project_library = root / "resolved-r-libraries/consolefirst"
        project_entry = "~/" + str(project_library.relative_to(Path.home().resolve()))
        with paused_project_library(binary, root, project_entry) as client:
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect(python='import os; del os.environ["TMPDIR"]')
            client.expect(
                # fmt: r
                r=code(r"""
                    project <- file.path(
                      getwd(),
                      c(
                        "project-first",
                        "resolved-r-libraries/consolefirst",
                        "project-last"
                      )
                    )
                    initial <- .libPaths()
                    stopifnot(identical(dirname(find.package("consolefirst")), project[[2L]]))
                    """)
            )
            client.expect("[prepared]", requirements={"r": ["consolesecond"]})
            client.expect(
                "project library retained across native activation\n",
                # fmt: r
                r=code(r"""
                    replacement <- file.path(
                      getwd(),
                      "resolved-r-libraries/consolefirst-consolesecond"
                    )
                    paths <- .libPaths()
                    stopifnot(
                      project[[2L]] %in% paths,
                      identical(paths[2:5], c(replacement, project)),
                      identical(initial[2:4], project),
                      identical(dirname(find.package("consolesecond")), replacement),
                      consolefirst::fixture(),
                      consolesecond::fixture()
                    )
                    cat("project library retained across native activation\n")
                    """),
            )
            return client.finish()


@requires(SANDBOX, command("ir"), posix_r_tempdir_whitespace(accepted=False))
def test_home_expansion_can_turn_a_library_entry_into_a_glob(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory(dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        home = root / "person[1]"
        home.mkdir()
        (home / "local").symlink_to(root / "resolved-r-libraries/consolefirst")
        (root / "person1/local").mkdir(parents=True)
        with paused_project_library(binary, root, "~/local") as client:
            client.expect(
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path

                    os.environ["HOME"] = str(Path.cwd() / "person[1]")
                    """)
            )
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect(python='del os.environ["TMPDIR"]')
            client.expect(
                # fmt: r
                r=code("""
                    project <- normalizePath(Sys.glob("~/local"))
                    candidate <- file.path(getwd(), "resolved-r-libraries", "consolefirst")
                    stopifnot(
                      identical(project, file.path(getwd(), "person1", "local")),
                      identical(dirname(find.package("consolefirst")), candidate),
                      identical(
                        .libPaths()[2:5],
                        c(
                          file.path(getwd(), "project-first"),
                          project,
                          file.path(getwd(), "project-last"),
                          candidate
                        )
                      ),
                      consolefirst::fixture()
                    )
                    """)
            )
            return client.finish()


@requires(SANDBOX, command("ir"), posix_r_tempdir_whitespace(accepted=False))
def test_literal_glob_name_does_not_supply_a_prepared_library(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory(dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        (root / "lib[1]").symlink_to(root / "resolved-r-libraries/consolefirst")
        (root / "lib1").mkdir()
        (root / r"q\a").symlink_to(root / "resolved-r-libraries/consolefirst")
        (root / "qa").mkdir()
        with paused_project_library(binary, root, r"lib[1]:q\a") as client:
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect(python='import os; del os.environ["TMPDIR"]')
            client.expect(
                # fmt: r
                r=code("""
                    matched <- normalizePath(Sys.glob(c("lib[1]", "q\\\\a")))
                    candidate <- file.path(getwd(), "resolved-r-libraries", "consolefirst")
                    stopifnot(
                      identical(matched, file.path(getwd(), c("lib1", "qa"))),
                      all(matched %in% .libPaths()),
                      identical(dirname(find.package("consolefirst")), candidate)
                    )
                    """),
            )
            return client.finish()


@requires(SANDBOX, command("ir"), posix_r_tempdir_whitespace(accepted=False))
def test_project_glob_keeps_its_prepared_library(binary: Path) -> Transcript:
    with TemporaryDirectory(dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        child = root / "child[1]"
        child.mkdir()
        (child / "lib1").symlink_to(root / "resolved-r-libraries/consolefirst")
        (root / "project-glob-second").mkdir()
        (child / "lib2").symlink_to(root / "project-glob-second")
        (root / "return.R").write_text('stopifnot(file.remove("lib1"))\nsetwd("..")\n')
        with paused_project_library(binary, root, "lib[12]") as client:
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect(
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path

                    os.environ["R_PROFILE_USER"] = str(Path.cwd() / "return.R")
                    os.chdir("child[1]")
                    del os.environ["TMPDIR"]
                    """),
            )
            client.expect(
                # fmt: r
                r=code("""
                    matched <- file.path(
                      getwd(),
                      c("resolved-r-libraries/consolefirst", "project-glob-second")
                    )
                    project <- c(
                      file.path(getwd(), "project-first"),
                      matched,
                      file.path(getwd(), "project-last")
                    )
                    stopifnot(
                      !file.exists("child[1]/lib1"),
                      identical(dirname(find.package("consolefirst")), matched[[1L]]),
                      identical(.libPaths()[2:5], project)
                    )
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consolesecond"]})
            client.expect(
                # fmt: r
                r=code("""
                    replacement <- dirname(find.package("consolesecond"))
                    stopifnot(identical(.libPaths()[2:6], c(replacement, project)))
                    """),
            )
            return client.finish()


@requires(POSIX, R, command("ir"))
@executions(SANDBOXED)
def test_glob_created_by_profile_does_not_retain_previous_library(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = recording_fixture_r_environment(root, ("consolesecond",))
        (root / "projectlibs").mkdir()
        (root / ".Renviron").write_text('R_LIBS="${R_LIBS}:projectlibs/*"\n')
        (root / ".Rprofile").write_text(
            'previous <- dirname(find.package("jsonlite"))\n'
            'stopifnot(file.symlink(previous, "projectlibs/pkg"))\n'
        )
        environment.update(
            R_ENVIRON_USER=str(root / ".Renviron"),
            R_PROFILE_USER=str(root / ".Rprofile"),
        )
        with McpClient(
            binary,
            execution.serve("--writable-root", str(root), "-c", "cache=host"),
            environment,
            root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                # fmt: r
                r=code("""
                    stopifnot(
                      previous %in% .libPaths(),
                      identical(normalizePath(Sys.glob("projectlibs/*")), previous)
                    )
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consolesecond"]})
            client.expect(
                # fmt: r
                r=code("""
                    stopifnot(
                      !previous %in% .libPaths(),
                      consolesecond::fixture()
                    )
                    """),
            )
            return client.finish()


@requires(POSIX, R, command("ir"))
@executions(SANDBOXED)
def test_renviron_glob_after_managed_entry_keeps_its_library(
    binary: Path, execution: Execution
) -> Transcript:
    return renviron_project_glob(
        binary, execution, "${R_LIBS}::projectlibs/*:project-last"
    )


@requires(POSIX, R, command("ir"))
@executions(SANDBOXED)
def test_renviron_glob_replacing_managed_entry_keeps_its_library(
    binary: Path, execution: Execution
) -> Transcript:
    return renviron_project_glob(binary, execution, "projectlibs/*::project-last")


def renviron_project_glob(
    binary: Path, execution: Execution, r_libs: str
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = recording_fixture_r_environment(
            root, ("consolefirst", "consolesecond")
        )
        modules = root / "modules"
        modules.mkdir()
        (modules / "sitecustomize.py").write_text(DEFER_R_STARTUP)
        (root / "projectlibs").mkdir()
        (root / "projectlibs/pkg").symlink_to(
            root / "resolved-r-libraries/consolefirst"
        )
        (root / "project-last").mkdir()
        environ = root / ".Renviron"
        environ.write_text(f'R_LIBS="{r_libs}"\n')
        environment.update(
            RETICULATE_PYTHON=sys.executable,
            RETICULATE_PYTHONPATH=str(modules),
            R_ENVIRON_USER=str(environ),
        )
        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.transcript.append(
                {"fixture": {".Renviron": environ.read_text().strip()}}
            )
            defer_r_bootstrap(client)
            client.expect(python="pass")
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect(
                # fmt: r
                r=code("""
                    project <- normalizePath(Sys.glob("projectlibs/*"))
                    tail <- normalizePath("project-last")
                    stopifnot(
                      grepl("::", Sys.getenv("R_LIBS"), fixed = TRUE),
                      identical(dirname(find.package("consolefirst")), project),
                      tail %in% .libPaths()
                    )
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consolesecond"]})
            client.expect(
                # fmt: r
                r=code("""
                    paths <- .libPaths()
                    stopifnot(project %in% paths, match(project, paths) < match(tail, paths))
                    """),
            )
            return client.finish()


@requires(SANDBOX, command("ir"), posix_r_tempdir_whitespace(accepted=False))
def test_relative_entry_supplies_prepared_library_after_chdir(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory(dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        (root / "local").mkdir()
        (root / "child").mkdir()
        (root / "child/local").symlink_to(root / "resolved-r-libraries/consolefirst")
        with paused_project_library(binary, root, "local") as client:
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect(
                # fmt: python
                python=code("""
                    import os

                    os.chdir("child")
                    del os.environ["TMPDIR"]
                    """),
            )
            client.expect(
                # fmt: r
                r=code("""
                    project <- normalizePath(c("../project-first", "local", "../project-last"))
                    stopifnot(identical(dirname(find.package("consolefirst")), project[[2L]]))
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consolesecond"]})
            client.expect(
                # fmt: r
                r=code("""
                    paths <- .libPaths()
                    stopifnot(identical(paths[paths %in% project], project))
                    """),
            )
            return client.finish()


@requires(SANDBOX, command("ir"), posix_r_tempdir_whitespace(accepted=False))
def test_prepared_library_path_with_literal_brackets(binary: Path) -> Transcript:
    with TemporaryDirectory(dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        with paused_project_library(
            binary, root, "lib*", library_root="packages[1]"
        ) as client:
            restarted = client.send(
                control="restart", requirements={"action": "set", "duckdb": []}
            )
            assert not restarted["isError"], restarted
            result = client.send(requirements={"r": ["consolefirst"]})
            assert result["isError"], result
            assert (
                "resolved R library path contains wildcard characters"
                in result["content"][0]["text"]
            ), result
            return client.finish()


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_renv_project_keeps_available_packages_and_library_order(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, record = recording_fixture_r_environment(
            root, ("consoleambient", "consolemissing")
        )
        base = subprocess.run(
            [
                environment["MCP_CONSOLE_TEST_REAL_IR"],
                "run",
                "--with",
                "renv",
                "--with",
                "reticulate",
                "--with",
                "jsonlite",
                "--isolated",
                "--vanilla",
                "-e",
                "cat(.libPaths()[[1L]])",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert Path(base).is_dir(), base
        # Prepare a real renv project before starting sandboxed execution.
        subprocess.run(
            [Path(environment["R_HOME"]) / "bin/Rscript", "--vanilla", "-"],
            # fmt: r
            input=code(r"""
                renv::init(project = getwd(), bare = TRUE, restart = FALSE)
                project <- renv::paths$library(project = getwd())
                for (package in list.files(
                  Sys.getenv("CONSOLE_PROJECT_BASE"),
                  full.names = TRUE
                )) {
                  target <- file.path(project, basename(package))
                  if (!file.exists(target)) stopifnot(file.symlink(package, target))
                }
                ambient <- file.path(
                  strsplit(
                    Sys.getenv("MCP_CONSOLE_TEST_IR_SOURCE_LIBRARIES"),
                    .Platform$path.sep
                  )[[1L]][[1L]],
                  "consoleambient"
                )
                stopifnot(file.symlink(ambient, file.path(project, "consoleambient")))
                """),
            env={
                **environment,
                "R_LIBS": base,
                "CONSOLE_PROJECT_BASE": base,
                "RENV_CONFIG_AUTO_SNAPSHOT": "FALSE",
                "RENV_CONFIG_SANDBOX_ENABLED": "FALSE",
            },
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        (root / "project-extra").mkdir()
        profile = root / ".Rprofile"
        profile.write_text(
            'source("renv/activate.R")\n.libPaths(c(.libPaths(), "project-extra"))\nlibrary(consoleambient)\nproject_paths <- .libPaths()\n'
        )
        environment.update(
            RENV_CONFIG_AUTO_SNAPSHOT="FALSE",
            RENV_CONFIG_STARTUP_QUIET="TRUE",
            RENV_CONFIG_SYNCHRONIZED_CHECK="FALSE",
            RENV_CONFIG_SANDBOX_ENABLED="FALSE",
            RENV_CONFIG_USER_PROFILE="FALSE",
            R_PROFILE_USER=str(profile),
            RETICULATE_PYTHON=sys.executable,
        )
        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "ambient available\n",
                r='stopifnot(consoleambient::fixture(), identical(as.character(packageVersion("consoleambient")), "0.0.0.9000")); cat("ambient available\\n")',
            )
            baseline = len(ir_run_records(record))
            client.expect(
                "ambient retained\n",
                r='stopifnot(requireNamespace("consoleambient"), consoleambient::fixture()); cat("ambient retained\\n")',
            )
            assert len(ir_run_records(record)) == baseline
            client.expect(
                "project paths retained\n",
                r='stopifnot(requireNamespace("consolemissing", quietly = TRUE), consolemissing::fixture(), identical(.libPaths()[.libPaths() %in% project_paths], project_paths)); cat("project paths retained\\n")',
            )
            runs = ir_run_records(record)[baseline:]
            assert len(runs) == 1 and "consolemissing" in ir_requirements(runs[0]), runs
            return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_bare_worker_clears_inherited_console_library(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = r_test_environment()
        environment = bare_runtime_environment(environment, root / "r-library")
        environment["MCP_CONSOLE_R_LIBRARY"] = str(root / "retired-library")
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            source = 'stopifnot(Sys.getenv("MCP_CONSOLE_R_LIBRARY") == ""); cat("bare library ready\\n")'
            client.expect("bare library ready\n", r=source)
            client.send(control="restart")
            client.expect("bare library ready\n", r=source)
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
