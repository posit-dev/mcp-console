#!/usr/bin/env -S uv run --script

import os
import plistlib
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.server.test_no_r import no_r_environment
from support.progress import without_elapsed
from support.assertions import (
    last_result_text,
    wait_for_evaluation_output,
    wait_for_worker_ready,
)
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import SANDBOXED
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, SANDBOX, SYSTEM_FONTS, requires
from support.resolvers import matplotlib_test_environment
from support.suites import run_this_suite


@contextmanager
def font_cache_client(
    binary: Path, root: Path, *, with_r: bool
) -> Iterator[tuple[McpClient, Path]]:
    environment = (
        matplotlib_test_environment(root / "cache")
        if with_r
        else no_r_environment(root)
    )
    environment["XDG_CACHE_HOME"] = str(root / "cache")
    source = root / (
        "host-matplotlib" if with_r else "cache/mcp-console/dependencies/matplotlib"
    )
    source.mkdir(parents=True)
    if with_r:
        environment["MPLCONFIGDIR"] = str(source)
    else:
        environment.pop("MPLCONFIGDIR", None)
    config = root / "selected-matplotlibrc"
    config.write_text("lines.linewidth: 7.25\n", encoding="utf-8")
    environment["MATPLOTLIBRC"] = str(config)
    environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(config)
    environment["MPLBACKEND"] = "svg"
    environment.pop("MPL_IGNORE_SYSTEM_FONTS", None)
    environment.pop("MCP_CONSOLE_TEST_FONT_GATE", None)
    environment["MCP_CONSOLE_TEST_FONT_SOURCE"] = str(source)
    environment["MCP_CONSOLE_TEST_FONT_DISCOVERY"] = str(source / "discovery.log")
    profiler_output = root / "system-profiler.plist"
    profiler_output.write_bytes(plistlib.dumps([{"_items": []}]))
    environment["MCP_CONSOLE_TEST_PROFILER_OUTPUT"] = str(profiler_output)
    probe = root / "bin/system_profiler"
    probe.parent.mkdir()
    probe.write_text(
        code(r"""
            #!/bin/sh
            set -eu
            test "$#" -eq 2
            test "$1" = "-xml"
            test "$2" = "SPFontsDataType"
            printf 'discovery\n' >> "$MCP_CONSOLE_TEST_FONT_DISCOVERY"
            if test "${MCP_CONSOLE_TEST_FONT_GATE:-}" = 1; then
                printf 1 > "$MCP_CONSOLE_TEST_FONT_STARTED"
                /bin/dd bs=1 count=1 < "$MCP_CONSOLE_TEST_FONT_RELEASE" > /dev/null 2> /dev/null
            fi
            /bin/cat "$MCP_CONSOLE_TEST_PROFILER_OUTPUT"
            """),
        encoding="utf-8",
    )
    probe.chmod(0o755)
    environment["PATH"] = os.pathsep.join((str(probe.parent), environment["PATH"]))
    arguments = SANDBOXED.serve("-c", "cache=host") if with_r else SANDBOXED.serve()
    with McpClient(binary, arguments, environment, root) as client:
        client.initialize_and_list_tools()
        wait_for_worker_ready(client, "Matplotlib font-cache readiness")
        client.expect("[prepared]", requirements={"python": ["matplotlib"]})
        assert (source / "discovery.log").read_text() == "discovery\n"
        yield client, source


@requires(SANDBOX, SYSTEM_FONTS, POSIX, R)
def test_reports_slow_font_cache_build_with_r(binary: Path) -> Transcript:
    return reports_slow_font_cache_build(binary, with_r=True)


@requires(SANDBOX, SYSTEM_FONTS, POSIX)
def test_reports_slow_font_cache_build_without_r(binary: Path) -> Transcript:
    return reports_slow_font_cache_build(binary, with_r=False)


def reports_slow_font_cache_build(binary: Path, *, with_r: bool) -> Transcript:
    with tempfile.TemporaryDirectory() as directory, ExitStack() as checkpoints:
        root = Path(directory).resolve()
        with font_cache_client(binary, root, with_r=with_r) as (client, source):
            persistent = next(source.glob("fontlist-v*.json"))
            persistent_bytes = persistent.read_bytes()
            setup = client.send(
                timeout_ms=60_000,
                # fmt: python
                python=code("""
                    import logging
                    import os
                    from pathlib import Path

                    import matplotlib

                    private = Path(os.environ["MPLCONFIGDIR"])
                    for cache in private.glob("fontlist-v*.json"):
                        cache.unlink()
                        cache.write_text("{}", encoding="utf-8")
                    os.environ["MCP_CONSOLE_TEST_FONT_DISCOVERY"] = str(private / "discovery.log")
                    os.environ["MCP_CONSOLE_TEST_FONT_GATE"] = "1"
                    os.environ["MCP_CONSOLE_TEST_FONT_STARTED"] = str(private / "font-started")
                    os.environ["MCP_CONSOLE_TEST_FONT_RELEASE"] = str(private / "font-release")
                    logger = logging.getLogger("matplotlib.font_manager")
                    handler = logging.StreamHandler()
                    handler.setFormatter(logging.Formatter("fonts: %(message)s"))
                    logger.addHandler(handler)
                    logger.propagate = False
                    logger.setLevel(logging.WARNING)
                    print(os.environ["MCP_CONSOLE_TEST_FONT_STARTED"])
                    print(os.environ["MCP_CONSOLE_TEST_FONT_RELEASE"])
                    """),
            )
            assert setup.get("isError") is not True, setup
            paths = last_result_text(client).splitlines()
            assert len(paths) == 2, setup
            setup["content"][0]["text"] = (
                "<font discovery started>\n<font discovery release>\n"
            )
            started, release = [FifoCheckpoint.create(Path(path)) for path in paths]
            checkpoints.callback(started.close)
            checkpoints.callback(release.close)
            try:
                client.send(
                    timeout_ms=0,
                    # fmt: python
                    python=code("""
                        import matplotlib.pyplot as plt

                        assert matplotlib.rcParams["lines.linewidth"] == 7.25
                        assert matplotlib.get_backend() == "svg"
                        _ = plt.plot([0, 1], [1, 0])
                        plt.close("all")
                        print("plot complete")
                        """),
                )
                assert (
                    without_elapsed(last_result_text(client))
                    == "\n[running; poll with an empty send]"
                )
                started.wait("cold worker reached system font discovery")
                # Hold real discovery until Matplotlib's own five-second timer
                # emits its diagnostic and the MCP client receives it.
                wait_for_evaluation_output(
                    client,
                    "fonts: Matplotlib is building the font cache; this may take a moment.\n"
                    "\n[running; poll with an empty send]",
                    "slow font-cache diagnostic",
                    completion_timeout_seconds=10,
                    timeout_ms=100,
                )
            finally:
                release.release()
            client.expect("plot complete\n", timeout_ms=60_000)
            client.expect(
                "fonts: findfont: Font family ['MCP Console missing test font'] not found. "
                "Falling back to DejaVu Sans.\n",
                python="_ = matplotlib.font_manager.findfont('MCP Console missing test font')",
            )
            assert persistent.read_bytes() == persistent_bytes
            assert (source / "discovery.log").read_text() == "discovery\n"
            return client.finish()


@requires(SANDBOX, SYSTEM_FONTS, R)
def test_reuses_prepared_and_refreshed_font_cache_with_r(binary: Path) -> Transcript:
    return reuses_prepared_and_refreshed_font_cache(binary, with_r=True)


@requires(SANDBOX, SYSTEM_FONTS)
def test_reuses_prepared_and_refreshed_font_cache_without_r(binary: Path) -> Transcript:
    return reuses_prepared_and_refreshed_font_cache(binary, with_r=False)


def reuses_prepared_and_refreshed_font_cache(
    binary: Path, *, with_r: bool
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        with font_cache_client(binary, root, with_r=with_r) as (client, source):
            persistent_caches = list(source.glob("fontlist-v*.json"))
            assert len(persistent_caches) == 1, persistent_caches
            persistent = persistent_caches[0]
            initial = persistent.read_bytes(), persistent.stat().st_mtime_ns
            # A discovery receipt distinguishes rebuilding from a quiet, fast
            # import. Warm use must neither discover fonts nor rewrite source.
            # fmt: python
            plot = code("""
                import os
                from pathlib import Path

                import matplotlib

                private = Path(os.environ["MPLCONFIGDIR"])
                source = Path(os.environ["MCP_CONSOLE_TEST_FONT_SOURCE"])
                caches = list(private.glob("fontlist-v*.json"))
                assert len(caches) == 1 and caches[0].is_symlink()
                assert caches[0].resolve().parent == source
                assert Path(matplotlib.get_cachedir()) == private
                assert Path(matplotlib.matplotlib_fname()).samefile(
                    os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]
                )
                assert matplotlib.rcParams["lines.linewidth"] == 7.25
                assert matplotlib.get_backend() == "svg"
                os.environ["MCP_CONSOLE_TEST_FONT_DISCOVERY"] = str(private / "discovery.log")

                import matplotlib.pyplot as plt

                _ = plt.plot([0, 1], [1, 0])
                plt.close("all")
                assert not (private / "discovery.log").exists()
                print("prepared font cache and selected configuration reused")
                """)
            for _ in range(2):
                client.expect(
                    "prepared font cache and selected configuration reused\n",
                    python=plot,
                )
            client.expect("[prepared]", requirements={"python": ["py-yaml12"]})
            client.expect(
                "prepared font cache and selected configuration reused\n", python=plot
            )
            client.send(control="restart")
            client.expect(
                "prepared font cache and selected configuration reused\n", python=plot
            )
            assert (source / "discovery.log").read_text() == "discovery\n"
            assert (persistent.read_bytes(), persistent.stat().st_mtime_ns) == initial

            # Invalidate only this fixture's source, then use public preparation
            # to rebuild it and a fresh worker to observe the refreshed cache.
            persistent.unlink()
            client.expect("[prepared]", requirements={"python": ["pillow"]})
            assert (source / "discovery.log").read_text() == "discovery\ndiscovery\n"
            refreshed = persistent.read_bytes(), persistent.stat().st_mtime_ns
            assert refreshed[1] != initial[1]
            client.send(control="restart")
            client.expect(
                "prepared font cache and selected configuration reused\n", python=plot
            )
            client.expect(
                "prepared source is read-only; private replacement is writable\n",
                # fmt: python
                python=code("""
                    cache = next(private.glob("fontlist-v*.json"))
                    try:
                        with cache.open("ab"):
                            pass
                    except PermissionError:
                        pass
                    else:
                        raise AssertionError("prepared source is writable")
                    cache.unlink()
                    cache.write_text("worker-private cache", encoding="utf-8")
                    assert cache.read_text(encoding="utf-8") == "worker-private cache"
                    print("prepared source is read-only; private replacement is writable")
                    """),
            )
            assert (persistent.read_bytes(), persistent.stat().st_mtime_ns) == refreshed
            assert (source / "discovery.log").read_text() == "discovery\ndiscovery\n"
            assert (root / "selected-matplotlibrc").read_text(encoding="utf-8") == (
                "lines.linewidth: 7.25\n"
            )
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
