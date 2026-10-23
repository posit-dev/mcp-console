#!/usr/bin/env -S uv run --script

import os
import plistlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.execution import SANDBOXED
from support.normalization import code
from support.records import Transcript
from support.requirements import SANDBOX, SYSTEM_FONT_DIRECTORY, SYSTEM_FONTS, requires
from support.resolvers import matplotlib_test_environment
from support.suites import run_this_suite


@requires(SANDBOX, SYSTEM_FONTS)
def test_prepares_system_fonts_and_protects_host_cache(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        workspace = temporary / "workspace-one"
        workspace.mkdir()
        system_fonts = sorted(
            path
            for path in SYSTEM_FONT_DIRECTORY.iterdir()
            if path.is_file() and path.suffix.lower() in {".otf", ".ttc", ".ttf"}
        )
        assert system_fonts, "test system font is required"
        system_font = system_fonts[0]
        profiler_output = temporary / "system-profiler.plist"
        profiler_output.write_bytes(
            plistlib.dumps([{"_items": [{"path": str(system_font)}]}])
        )
        path = os.environ.get("PATH")
        assert path is not None, "PATH is required"
        probe = temporary / "bin" / "system_profiler"
        probe.parent.mkdir()
        probe.write_text(
            code(r"""
                #!/bin/sh
                set -eu
                test "$#" -eq 2
                test "$1" = "-xml"
                test "$2" = "SPFontsDataType"
                : > "$TMPDIR/mcp-console-font-discovery"
                : > "$MCP_CONSOLE_TEST_SYSTEM_PROFILER_MARKER"
                /bin/cat "$MCP_CONSOLE_TEST_SYSTEM_PROFILER_OUTPUT"
                """),
            encoding="utf-8",
        )
        probe.chmod(0o755)
        fontconfig = temporary / "fonts.conf"
        fontconfig.write_text(
            code(r"""
                <?xml version="1.0"?>
                <!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
                <fontconfig>
                  <cachedir prefix="xdg">mcp-console-test</cachedir>
                </fontconfig>
                """),
            encoding="utf-8",
        )
        host_matplotlib = temporary / "host-matplotlib"
        host_matplotlib.mkdir()
        host_matplotlibrc = host_matplotlib / "matplotlibrc"
        host_matplotlibrc.write_text("lines.linewidth: 7.25\n", encoding="utf-8")
        environment = matplotlib_test_environment(temporary / "host-cache")
        environment["TMPDIR"] = temporary_directory
        environment["FONTCONFIG_FILE"] = str(fontconfig)
        environment["MPLCONFIGDIR"] = str(host_matplotlib)
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(host_matplotlibrc)
        environment["MCP_CONSOLE_TEST_SYSTEM_PROFILER_OUTPUT"] = str(profiler_output)
        host_discovery = temporary / "profiler-record/mcp-console-font-discovery"
        host_discovery.parent.mkdir()
        environment["UV_TOOL_DIR"] = str(host_discovery.parent)
        environment["MCP_CONSOLE_TEST_SYSTEM_PROFILER_MARKER"] = str(host_discovery)
        environment["PATH"] = os.pathsep.join((str(probe.parent), path))
        environment.pop("MATPLOTLIBRC", None)
        environment.pop("MPL_IGNORE_SYSTEM_FONTS", None)
        client = McpClient(
            binary,
            SANDBOXED.serve("-c", "cache=host"),
            environment,
            current_directory=workspace,
        )
        with client:
            client.initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                reticulate::py_require("matplotlib")
                invisible(reticulate::py_config())
                """)
            client.expect(r=r)
            assert host_discovery.is_file()
            persistent_caches = list(host_matplotlib.glob("fontlist-v*.json"))
            assert len(persistent_caches) == 1, persistent_caches
            persistent_cache_bytes = persistent_caches[0].read_bytes()
            host_discovery.unlink()

            client.expect(
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path

                    import matplotlib

                    invalid_cache = Path(os.environ["MPLCONFIGDIR"]) / "fontlist-v999.json"
                    _ = invalid_cache.write_text(
                        '{"__class__":"FontManager","_version":999}', encoding="utf-8"
                    )
                    """)
            )
            client.send(control="restart")
            assert last_result_text(client) == (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            )
            # fmt: python
            python = code("""
                import os
                from pathlib import Path

                marker = Path(os.environ["TMPDIR"]) / "mcp-console-font-discovery"
                invalid_cache = Path(os.environ["MPLCONFIGDIR"]) / "fontlist-v999.json"
                invalid_cache_was_seeded = invalid_cache.exists()

                import matplotlib
                import matplotlib.font_manager

                config = Path(matplotlib.matplotlib_fname())
                font_cache = next(Path(os.environ["MPLCONFIGDIR"]).glob("fontlist-v*.json"))
                try:
                    with font_cache.open("a", encoding="utf-8"):
                        pass
                except PermissionError:
                    font_cache_read_only = True
                else:
                    font_cache_read_only = False

                try:
                    with config.open("a", encoding="utf-8"):
                        pass
                except PermissionError:
                    config_read_only = True
                else:
                    config_read_only = False

                try:
                    config.with_name("worker-payload").write_text("payload", encoding="utf-8")
                except PermissionError:
                    config_directory_read_only = True
                else:
                    config_directory_read_only = False

                private_probe = Path(os.environ["MPLCONFIGDIR"]) / "config-write-probe"
                private_probe.write_text("ok", encoding="utf-8")

                (
                    config.resolve() == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve(),
                    matplotlib.rcParams["lines.linewidth"],
                    font_cache_read_only,
                    config_read_only,
                    config_directory_read_only,
                    private_probe.read_text(encoding="utf-8") == "ok",
                    marker.exists(),
                    invalid_cache_was_seeded,
                )
                """)
            client.expect(
                "(True, 7.25, True, True, True, True, False, False)\n", python=python
            )
            transcript = client.finish()
            discoveries = list(temporary.rglob("mcp-console-font-discovery"))
            assert not discoveries, discoveries
            assert (
                host_matplotlibrc.read_text(encoding="utf-8")
                == "lines.linewidth: 7.25\n"
            )
            assert not (host_matplotlib / "worker-payload").exists()
            assert len(persistent_caches) == 1, persistent_caches
            assert persistent_caches[0].read_bytes() == persistent_cache_bytes
            assert not (persistent_caches[0].parent / "fontlist-v999.json").exists()
            assert not list(
                (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                    "fontlist-v*.json"
                )
            )
            return transcript


@requires(SANDBOX)
def test_explicit_matplotlib_config_is_read_only(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        explicit = temporary / "explicit"
        explicit.mkdir()
        explicit_rc = explicit / "matplotlibrc"
        explicit_rc.write_text("lines.linewidth: 8.25\n", encoding="utf-8")
        inherited = temporary / "inherited"
        inherited.mkdir()
        (inherited / "matplotlibrc").write_text(
            "lines.linewidth: 18.25\n",
            encoding="utf-8",
        )
        environment = matplotlib_test_environment(temporary / "host-cache")
        environment["TMPDIR"] = temporary_directory
        environment["MPLCONFIGDIR"] = str(inherited)
        environment["MATPLOTLIBRC"] = str(explicit_rc)
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(explicit_rc)
        client = McpClient(binary, SANDBOXED.serve("-c", "cache=host"), environment)
        with client:
            client.initialize_and_list_tools()
            client.expect("[prepared]", requirements={"python": ["matplotlib"]})
            # fmt: python
            python = code("""
                import errno
                import os
                import sys
                from pathlib import Path

                import matplotlib

                config = Path(matplotlib.matplotlib_fname())
                try:
                    with config.open("a", encoding="utf-8"):
                        pass
                except OSError as error:
                    assert error.errno == (errno.EROFS if sys.platform == "linux" else errno.EPERM)
                    config_read_only = True
                else:
                    config_read_only = False

                private_probe = Path(os.environ["MPLCONFIGDIR"]) / "config-write-probe"
                private_probe.write_text("ok", encoding="utf-8")

                (
                    config.resolve() == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve(),
                    matplotlib.rcParams["lines.linewidth"],
                    config_read_only,
                    private_probe.read_text(encoding="utf-8") == "ok",
                )
                """)
            client.expect("(True, 8.25, True, True)\n", python=python)
            transcript = client.finish()
            assert explicit_rc.read_text(encoding="utf-8") == "lines.linewidth: 8.25\n"
            assert not list(explicit.glob("fontlist-v*.json"))
            caches = list(inherited.glob("fontlist-v*.json"))
            assert len(caches) == 1, caches
            assert not list(
                (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                    "fontlist-v*.json"
                )
            )
            return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
