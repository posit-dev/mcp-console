#!/usr/bin/env -S uv run --script

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite


def _managed_environment(binary: Path, inherit: bool) -> Transcript:
    environment, _ = r_test_environment()
    environment.pop("RETICULATE_PYTHON", None)
    environment["MCP_CONSOLE_TEST_INHERITED_ENV"] = "caller"
    target_environment = {
        name: environment[name] for name in ("R_HOME", "R_PROFILE_USER", "HOME", "PATH")
    }
    target_environment["MCP_CONSOLE_TEST_PROJECT_ENV"] = "project"
    target_environment["MCP_CONSOLE_MANAGED_TEMPDIR"] = "0"
    target_environment["mcp_console_managed_tempdir"] = "project"
    if inherit:
        target_environment.update(
            {
                "R_LIBS": "/invalid/project/library",
                "RETICULATE_PYTHON": "/invalid/project/python",
                "MCP_CONSOLE_MANAGED_PYTHON": "invalid project manifest",
                "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION": "0",
                "MCP_CONSOLE_SANDBOX": "project override",
            }
        )
    with TemporaryDirectory() as directory:
        workspace = Path(directory)
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {"environment": target_environment, "inherit_environment": inherit}
            ),
            encoding="utf-8",
        )
        with McpClient(binary, ("serve",), environment, workspace) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"r": ["praise"], "python": ["py-yaml12"]})
            assert last_tool_text(client) == "[prepared]", last_tool_text(client)
            for generation in range(2):
                if generation:
                    client.send(control="restart")
                    assert last_tool_text(client).endswith(
                        "[starting new worker]\n[idle]"
                    ), last_tool_text(client)
                # Inspect the retained manifest before activating Python; automatic
                # resolution must not mask a lost generation environment.
                client.send(
                    # fmt: r
                    r=code(r"""
                        stopifnot(
                          identical(Sys.getenv("MCP_CONSOLE_SANDBOX"), "1"),
                          identical(Sys.getenv("MCP_CONSOLE_MANAGED_TEMPDIR"), "1"),
                          identical(Sys.getenv("mcp_console_managed_tempdir"), "project"),
                          identical(Sys.getenv("MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION"), "1"),
                          identical(Sys.getenv("RETICULATE_PYTHON"), "managed"),
                          identical(Sys.getenv("MCP_CONSOLE_TEST_PROJECT_ENV"), "project"),
                          identical(dirname(find.package("praise")), .libPaths()[[2L]]),
                          "py-yaml12" %in% reticulate::py_require()$packages
                        )
                        cat("managed R and Python requirements retained\n")
                        """)
                )
                assert (
                    last_tool_text(client)
                    == "managed R and Python requirements retained\n"
                ), last_tool_text(client)
                client.send(
                    # fmt: python
                    python=code("""
                        import os
                        import yaml12

                        print(yaml12.__name__)
                        print(os.environ.get("MCP_CONSOLE_TEST_INHERITED_ENV", "absent"))
                        """)
                )
                assert last_tool_text(client) == "yaml12\n" + (
                    "caller\n" if inherit else "absent\n"
                ), last_tool_text(client)
            return client.finish()


@requires(SANDBOX)
def test_preserves_managed_environment_without_inheritance(binary: Path) -> Transcript:
    return _managed_environment(binary, inherit=False)


@requires(SANDBOX)
def test_preserves_managed_environment_over_project_overrides(
    binary: Path,
) -> Transcript:
    return _managed_environment(binary, inherit=True)


@requires(SANDBOX)
def test_preserves_caller_selected_python_over_project_environment(
    binary: Path,
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = sys.executable
    with TemporaryDirectory() as directory:
        workspace = Path(directory)
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "environment": {
                        "RETICULATE_PYTHON": "/invalid/project/python",
                        "MCP_CONSOLE_MANAGED_PYTHON": "invalid project manifest",
                        "MCP_CONSOLE_TEST_SELECTED_PYTHON": sys.executable,
                    }
                }
            ),
            encoding="utf-8",
        )
        with McpClient(binary, ("serve",), environment, workspace) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import os
                    import sys

                    assert sys.executable == os.environ["MCP_CONSOLE_TEST_SELECTED_PYTHON"]
                    print("caller-selected Python retained")
                    """)
            )
            assert last_tool_text(client) == "caller-selected Python retained\n", (
                last_tool_text(client)
            )
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
