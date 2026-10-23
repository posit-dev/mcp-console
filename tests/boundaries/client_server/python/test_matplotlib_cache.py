#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text, tool_text, wait_for_worker_ready
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.resolvers import matplotlib_test_environment
from support.requirements import R, SANDBOX, requires
from boundaries.client_server.server.test_no_r import no_r_environment
from support.suites import run_this_suite


@requires(SANDBOX, R)
def test_uses_resolver_font_cache_with_r(binary: Path) -> Transcript:
    return uses_resolver_font_cache(binary, with_r=True)


@requires(SANDBOX)
def test_uses_resolver_font_cache_without_r(binary: Path) -> Transcript:
    return uses_resolver_font_cache(binary, with_r=False)


def uses_resolver_font_cache(binary: Path, *, with_r: bool) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        env = (
            matplotlib_test_environment(root / "cache")
            if with_r
            else no_r_environment(root)
        )
        env["XDG_CACHE_HOME"] = str(root / "cache")
        env.pop("MPLCONFIGDIR", None)
        # Reuse the prepared host R environment; rebuilding unrelated R packages
        # is independent of the shared font cache contract. The R-free case
        # covers the default Console cache selection.
        cache = root / (
            "cache/matplotlib"
            if with_r
            else "cache/mcp-console/dependencies/matplotlib"
        )
        if with_r:
            env["MPLCONFIGDIR"] = str(cache)
        env["MCP_CONSOLE_TEST_FONT_CACHE"] = str(cache)
        config = root / "matplotlibrc"
        config.write_text("lines.linewidth: 7.25\n", encoding="utf-8")
        env["MATPLOTLIBRC"] = str(config)
        arguments = SANDBOXED.serve("-c", "cache=host") if with_r else SANDBOXED.serve()
        with McpClient(binary, arguments, env, root) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(client, "resolver font cache readiness")
            client.expect("[prepared]", requirements={"python": ["matplotlib"]})
            for restart in (False, True):
                if restart:
                    client.send(control="restart")
                client.expect(
                    "resolver cache and host configuration retained\n",
                    # fmt: python
                    python=code("""
                        import os
                        from pathlib import Path
                        import matplotlib

                        source = Path(os.environ["MCP_CONSOLE_TEST_FONT_CACHE"])
                        private = Path(os.environ["MPLCONFIGDIR"])
                        caches = list(private.glob("fontlist-v*.json"))
                        assert caches and private != source
                        assert all(path.is_symlink() and path.resolve().parent == source for path in caches)
                        assert matplotlib.rcParams["lines.linewidth"] == 7.25
                        print("resolver cache and host configuration retained")
                        """),
                )
            client.finish()
        assert list(cache.glob("fontlist-v*.json"))
    return [
        {
            "resolver_font_cache": "shared",
            "restart": "shared",
            "host_configuration": "retained",
        }
    ]


@executions(DIRECT, SANDBOXED)
@requires(R)
def test_preserves_matplotlib_cache_across_activation_and_restart(
    binary: Path, execution: Execution
) -> Transcript:
    return preserves_matplotlib_cache_across_activation_and_restart(
        binary, execution, with_r=True
    )


@executions(DIRECT, SANDBOXED)
def test_preserves_no_r_matplotlib_cache_across_activation_and_restart(
    binary: Path, execution: Execution
) -> Transcript:
    return preserves_matplotlib_cache_across_activation_and_restart(
        binary, execution, with_r=False
    )


def preserves_matplotlib_cache_across_activation_and_restart(
    binary: Path, execution: Execution, *, with_r: bool
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory, ExitStack() as clients:
        temporary = Path(temporary_directory)
        workspace = temporary / "workspace"
        workspace.mkdir()
        host_matplotlib = temporary / "host-matplotlib"
        host_matplotlib.mkdir()
        host_matplotlibrc = host_matplotlib / "matplotlibrc"
        host_matplotlibrc.write_text("lines.linewidth: 7.25\n", encoding="utf-8")
        environment = (
            matplotlib_test_environment(temporary / "host-cache")
            if with_r
            else no_r_environment(temporary)
        )
        environment["XDG_CACHE_HOME"] = str(temporary / "host-cache")
        environment["TMPDIR"] = temporary_directory
        environment["MPLCONFIGDIR"] = str(host_matplotlib)
        environment["MCP_CONSOLE_TEST_PRIVATE_CACHE"] = str(int(execution is SANDBOXED))
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(host_matplotlibrc)
        environment.pop("MATPLOTLIBRC", None)
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        client = clients.enter_context(
            McpClient(
                binary,
                execution.serve(
                    "-c", "python.managed.resolution=implicit", "-c", "cache=host"
                ),
                environment,
                current_directory=workspace,
            )
        )
        client.initialize_and_list_tools()
        if with_r:
            # fmt: r
            r = code(r"""
                reticulate::py_require("matplotlib")
                invisible(reticulate::py_config())
                """)
            client.expect(r=r)
        else:
            wait_for_worker_ready(client, "Matplotlib cache declaration readiness")
            client.expect("[prepared]", requirements={"python": ["matplotlib"]})
        persistent_caches = list(host_matplotlib.glob("fontlist-v*.json"))
        assert len(persistent_caches) == 1, persistent_caches
        persistent_cache_bytes = persistent_caches[0].read_bytes()
        client.expect(
            # fmt: python
            python=code("""
                import os
                import sys
                from pathlib import Path

                import matplotlib

                invalid_cache = Path(os.environ["MPLCONFIGDIR"]) / "fontlist-v999.json"
                _ = invalid_cache.write_text(
                    '{"__class__":"FontManager","_version":999}', encoding="utf-8"
                )
                """)
        )
        # Replacing the cache link or file must not make a later runtime resolution
        # overwrite user-owned worker state or discard the worker.
        # fmt: python
        python = code("""
            private_cache = next(
                path
                for path in Path(os.environ["MPLCONFIGDIR"]).glob("fontlist-v*.json")
                if path.name != "fontlist-v999.json"
            )
            if sys.platform != "win32":
                assert private_cache.is_symlink() is (os.environ["MCP_CONSOLE_TEST_PRIVATE_CACHE"] == "1")
            private_cache_bytes = private_cache.read_bytes()
            private_cache.unlink()
            private_cache.write_bytes(private_cache_bytes)
            cache_link_replaced = True
            """)
        client.expect(python=python)

        if with_r:
            # fmt: r
            r = code(r"""
                reticulate::py_require("py-yaml12")
                """)
            client.expect(r=r)
        else:
            client.expect("[prepared]", requirements={"python": ["py-yaml12"]})
        client.expect(
            "(True, 'yaml12')\n",
            python="(cache_link_replaced, __import__('yaml12').__name__)",
        )

        client.send(control="restart")
        assert last_result_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib
            import matplotlib.font_manager

            config = Path(matplotlib.matplotlib_fname())
            private_probe = Path(os.environ["MPLCONFIGDIR"]) / "config-write-probe"
            private_probe.write_text("ok", encoding="utf-8")

            (
                config.samefile(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]),
                matplotlib.rcParams["lines.linewidth"],
                private_probe.read_text(encoding="utf-8") == "ok",
            )
            """)
        client.expect("(True, 7.25, True)\n", python=python)
        transcript = client.finish()
        assert (
            host_matplotlibrc.read_text(encoding="utf-8") == "lines.linewidth: 7.25\n"
        )
        assert len(persistent_caches) == 1, persistent_caches
        assert persistent_caches[0].read_bytes() == persistent_cache_bytes
        assert (persistent_caches[0].parent / "fontlist-v999.json").exists() is (
            execution is DIRECT
        )
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


@executions(SANDBOXED)
def test_keeps_python_caches_private_between_workers(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        environment = {**os.environ, "TMPDIR": directory}
        with (
            McpClient(binary, execution.serve(), environment) as first,
            McpClient(binary, execution.serve(), environment) as second,
        ):
            for client in (first, second):
                client.initialize_and_list_tools()
                result = client.send(
                    # fmt: python
                    python=code("""
                        import os
                        from pathlib import Path

                        cache = Path(os.environ["MPLCONFIGDIR"])
                        cache.mkdir(parents=True, exist_ok=True)
                        marker = cache / "session-marker"
                        print(marker.exists())
                        """)
                )
                assert tool_text(result) == "False\n", result
                result = client.send(
                    # fmt: python
                    python=code("""
                        marker.write_text("private")
                        print(marker.read_text())
                        """)
                )
                assert tool_text(result) == "private\n", result
            first.send(control="restart")
            result = first.send(
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path

                    print((Path(os.environ["MPLCONFIGDIR"]) / "session-marker").exists())
                    """)
            )
            assert tool_text(result) == "False\n", result
            return first.finish() + second.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
