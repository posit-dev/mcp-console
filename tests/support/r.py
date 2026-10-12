import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from support.client import McpClient
from support.execution import Execution


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# R accepts the Windows code-page suffix and platform-native Unix locale names.
UTF8_LOCALE = (
    ".UTF-8" if os.name == "nt" else "C.UTF-8" if sys.platform == "linux" else "UTF-8"
)


def r_test_environment() -> tuple[dict[str, str], Path]:
    environment = os.environ.copy()
    # Keep personal startup code out of isolated fixture libraries.
    for name in ("R_ENVIRON", "R_ENVIRON_USER", "R_PROFILE", "R_PROFILE_USER"):
        environment[name] = os.devnull
    if r_home := environment.get("R_HOME"):
        home = Path(r_home)
    else:
        output = subprocess.run(
            [shutil.which("R") or "R", "RHOME"],
            check=True,
            capture_output=True,
            text=True,
        )
        home = Path(output.stdout.strip())
        environment["R_HOME"] = str(home)
    return environment, home / "bin" / ("Rscript.exe" if os.name == "nt" else "Rscript")


def install_r_startup(
    directory: Path, environment: dict[str, str], source: str
) -> Path:
    """Install the shared interactive startup package with a case-specific script."""
    rscript = (
        Path(environment["R_HOME"])
        / "bin"
        / ("Rscript.exe" if os.name == "nt" else "Rscript")
    )
    libraries = subprocess.check_output(
        [rscript, "--vanilla", "-e", "writeLines(.libPaths())"],
        env=environment,
        text=True,
    ).splitlines()
    script = directory / "startup.R"
    script.write_text(
        f".libPaths(c({', '.join(json.dumps(path) for path in libraries)}, .libPaths()))\n"
        + source
    )
    library = directory / "library"
    library.mkdir()
    subprocess.run(
        [
            rscript.with_name("R.exe" if os.name == "nt" else "R"),
            "CMD",
            "INSTALL",
            f"--library={library}",
            FIXTURES / "bootstrap_r",
        ],
        env=environment,
        check=True,
        capture_output=True,
    )
    environment.update(
        R_LIBS=os.pathsep.join(filter(None, (str(library), environment.get("R_LIBS")))),
        R_DEFAULT_PACKAGES="datasets,utils,grDevices,graphics,stats,methods,mcpconsolebootstrap",
        MCP_CONSOLE_TEST_BOOTSTRAP_SCRIPT=str(script),
    )
    return library


def isolated_r_home(directory: Path, environment: dict[str, str]) -> Path:
    """Retain installed R files while isolating bootstrap settings and loader paths."""
    original = Path(environment["R_HOME"])
    selected = directory / "R"
    selected.mkdir()
    for entry in original.iterdir():
        if entry.name not in {"bin", "etc", "lib"}:
            (selected / entry.name).symlink_to(entry)
    for name in ("bin", "etc", "lib"):
        destination = selected / name
        destination.mkdir()
        for entry in (original / name).iterdir():
            target = destination / entry.name
            if name == "bin" and entry.name == "R":
                source, count = re.subn(
                    r"(?m)^R_HOME_DIR=.*$",
                    f"R_HOME_DIR={shlex.quote(str(selected))}",
                    entry.read_text(),
                    count=1,
                )
                assert count == 1, "R launcher must declare R_HOME_DIR"
                target.write_text(source)
                target.chmod(entry.stat().st_mode)
            elif name == "etc" and entry.name == "Renviron":
                shutil.copyfile(entry, target)
            else:
                target.symlink_to(entry)
    environment["R_HOME"] = str(selected)
    # Rscript uses RHOME to override its compiled-in installation path.
    environment["RHOME"] = str(selected)
    environment["PATH"] = os.pathsep.join([str(selected / "bin"), environment["PATH"]])
    return selected


def reference_r_error(environment: dict[str, str], source: str) -> str:
    result = subprocess.run(
        [
            Path(environment["R_HOME"])
            / "bin"
            / ("Rscript.exe" if os.name == "nt" else "Rscript"),
            "--vanilla",
            "-",
        ],
        input=source,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert result.returncode == 1, result
    # The persistent Console worker does not exit when an R cell errors.
    assert result.stdout.endswith("Execution halted\n"), result.stdout
    return result.stdout.removesuffix("Execution halted\n")


def build_r_input_handler(
    directory: Path,
    environment: dict[str, str],
    rscript: Path,
) -> None:
    source = FIXTURES / "r_input_handler.c"
    local_source = directory / source.name
    shutil.copyfile(source, local_source)
    subprocess.run(
        [
            rscript.parent / "R",
            "CMD",
            "SHLIB",
            "-o",
            "mcp_test_input_handler.so",
            local_source.name,
        ],
        cwd=directory,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def reference_plots(
    rscript: Path,
    environment: dict[str, str],
    source: str,
    *,
    width: float,
    height: float,
    dpi: float,
    pages: int,
    expected_error: str | None = None,
) -> list[bytes]:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        error_handler = ""
        if expected_error is not None:
            message = json.dumps(expected_error)
            error_handler = (
                ", error = function(error) "
                f"stopifnot(identical(conditionMessage(error), {message}))"
            )
        script = (
            "base::local({\n"
            "  directory <- commandArgs(trailingOnly = TRUE)[[1L]]\n"
            "  device_counter <- 0L\n"
            "  options(device = function(...) {\n"
            "    device_counter <<- device_counter + 1L\n"
            "    grDevices::png(\n"
            "      filename = file.path(\n"
            "        directory,\n"
            '        sprintf("device-%06d-page-%%06d.png", device_counter)\n'
            "      ),\n"
            f'      width = {width}, height = {height}, units = "in", res = {dpi}\n'
            "    )\n"
            "  })\n"
            "  tryCatch({\n"
            f"{source}"
            f"  }}{error_handler}, finally = grDevices::graphics.off())\n"
            "})\n"
        )
        subprocess.run(
            [rscript, "--vanilla", "-", str(directory)],
            input=script,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        paths = sorted(directory.glob("device-*-page-*.png"))
        assert len(paths) == pages, paths
        return [path.read_bytes() for path in paths]


@contextmanager
def r_input_handler_client(
    binary: Path, execution: Execution
) -> Iterator[tuple[McpClient, Path]]:
    with tempfile.TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        environment, rscript = r_test_environment()
        environment["TMPDIR"] = temporary_directory
        build_r_input_handler(directory, environment, rscript)
        with McpClient(
            binary,
            execution.serve(),
            environment=environment,
            current_directory=directory,
        ) as client:
            yield client, directory


@contextmanager
def startup_r_package(directory: Path, source: str) -> Iterator[dict[str, str]]:
    """Run a default package's .onLoad hook during native R startup."""
    environment, rscript = r_test_environment()
    package = directory / "startup-package"
    (package / "R").mkdir(parents=True)
    library = directory / "startup-library"
    library.mkdir()
    hook = directory / "startup.R"
    hook.write_text(source)
    (package / "DESCRIPTION").write_text(
        "Package: mcpconsolestartup\nVersion: 0.0.1\nTitle: Startup fixture\n"
        "Description: Exercises public R package startup.\nLicense: MIT\n"
        "Author: Test\nMaintainer: Test <test@example.org>\n"
    )
    (package / "NAMESPACE").write_text("")
    (package / "R/startup.R").write_text(
        ".onLoad <- function(libname, pkgname) {\n"
        '  if (!nzchar(Sys.getenv("MCP_CONSOLE_LOCAL_RUNTIME"))) return(invisible())\n'
        '  sys.source(Sys.getenv("MCP_CONSOLE_TEST_R_STARTUP"), envir = .GlobalEnv)\n'
        "}\n"
    )
    subprocess.run(
        [
            rscript.with_name("R"),
            "CMD",
            "INSTALL",
            "--no-test-load",
            f"--library={library}",
            package,
        ],
        env=environment,
        capture_output=True,
        check=True,
    )
    environment.update(
        R_LIBS=os.pathsep.join(filter(None, (str(library), environment.get("R_LIBS")))),
        R_DEFAULT_PACKAGES="datasets,utils,grDevices,graphics,stats,methods,mcpconsolestartup",
        MCP_CONSOLE_TEST_R_STARTUP=str(hook),
    )
    yield environment
