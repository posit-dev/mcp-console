#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.client import McpClient
from support.normalization import code
from support.python import virtualenv_python
from support.records import Transcript
from support.requirements import R, SANDBOX, UNPRIVILEGED, requires
from support.snapshots import platform_snapshots
from support.suites import run_this_suite


CONFIG = ".agents/console/config.yaml"
LABEL = "MCP_CONSOLE_TEST_DISCOVERY"


def write_config(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def observe_environment(
    binary: Path,
    workspace: Path,
    environment: dict[str, str],
    arguments: tuple[str, ...],
    expected: str,
) -> None:
    if "serve" in arguments:
        with McpClient(
            binary,
            arguments,
            environment=environment,
            current_directory=workspace,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                expected + "\n",
                # fmt: r
                r=code("""
                    cat(
                      Sys.getenv(c("MCP_CONSOLE_TEST_DISCOVERY", "KEEP", "ADDED"), unset = "unset"),
                      sep = "|"
                    )
                    cat("\n")
                    """),
            )
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr
    else:
        # fmt: python
        script = code("""
            import os

            print(
                "|".join(
                    os.environ.get(name, "unset")
                    for name in ("MCP_CONSOLE_TEST_DISCOVERY", "KEEP", "ADDED")
                )
            )
            """)
        result = subprocess.run(
            [binary, *arguments, "--", sys.executable, "-c", script],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0 and result.stderr == "", result
        assert result.stdout == expected + "\n", result


@requires(SANDBOX, R)
def test_layers_global_project_and_cli_in_both_commands(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        home = root / "console-home"
        write_config(
            home / "config.yaml",
            {"cache": "host", "environment": {LABEL: "home", "KEEP": "home"}},
        )
        environment = os.environ | {"MCP_CONSOLE_HOME": str(home)}
        for name in (LABEL, "KEEP", "ADDED"):
            environment.pop(name, None)
        for command in ("serve", "sandbox"):
            project = workspace / CONFIG
            write_config(
                project,
                {"environment": {LABEL: "project", "ADDED": "project"}},
            )
            cases = (
                ("default", (command,), "project|home|project"),
                (
                    "client default below files",
                    (
                        "--default-config",
                        "environment={MCP_CONSOLE_TEST_DISCOVERY: client, KEEP: client}",
                        command,
                    ),
                    "project|home|project",
                ),
                (
                    "client default without files",
                    (
                        command,
                        "--no-config",
                        "--default-config",
                        "environment={MCP_CONSOLE_TEST_DISCOVERY: client, KEEP: client}",
                    ),
                    "client|client|unset",
                ),
                (
                    "ordered overrides",
                    (
                        "-c",
                        "environment.MCP_CONSOLE_TEST_DISCOVERY=first",
                        command,
                        "-c",
                        "environment.MCP_CONSOLE_TEST_DISCOVERY=last",
                    ),
                    "last|home|project",
                ),
                ("before", ("--no-project-config", command), "home|home|unset"),
                ("after", (command, "--no-project-config"), "home|home|unset"),
                (
                    "no global before",
                    ("--no-global-config", command),
                    "project|unset|project",
                ),
                (
                    "no global after",
                    (command, "--no-global-config"),
                    "project|unset|project",
                ),
                (
                    "no global with overrides",
                    (
                        "--no-global-config",
                        "-c",
                        "environment.MCP_CONSOLE_TEST_DISCOVERY=first",
                        command,
                        "-c",
                        "environment.MCP_CONSOLE_TEST_DISCOVERY=last",
                    ),
                    "last|unset|project",
                ),
                ("no config before", ("--no-config", command), "unset|unset|unset"),
                ("no config after", (command, "--no-config"), "unset|unset|unset"),
                (
                    "both scopes excluded",
                    ("--no-global-config", command, "--no-project-config"),
                    "unset|unset|unset",
                ),
                (
                    "both",
                    (
                        "--no-project-config",
                        command,
                        "--no-config",
                        "-c",
                        "environment.ADDED=cli",
                    ),
                    "unset|unset|cli",
                ),
            )
            for placement, arguments, label in cases:
                observe_environment(binary, workspace, environment, arguments, label)
                records.append(
                    {"command": command, "flag": placement, "selected": label}
                )
            project.unlink()
            for arguments in ((command,), (command, "--no-project-config")):
                observe_environment(
                    binary, workspace, environment, arguments, "home|home|unset"
                )
                records.append(
                    {
                        "command": command,
                        "arguments": list(arguments),
                        "project": "absent",
                        "selected": "home",
                    }
                )
            observe_environment(
                binary,
                workspace,
                environment,
                (command, "--no-global-config", "-c", "environment.ADDED=cli"),
                "unset|unset|cli",
            )
            records.append(
                {
                    "command": command,
                    "project": "absent",
                    "global": "excluded",
                    "selected": "unset|unset|cli",
                }
            )
    return records


@requires(SANDBOX, R)
def test_layers_ordered_overrides_on_home_and_defaults(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        home = root / "console-home"
        home.mkdir()
        write_config(workspace / CONFIG, {"unknown": "must be skipped"})
        environment = os.environ | {"MCP_CONSOLE_HOME": str(home)}
        for name in (LABEL, "KEEP", "ADDED"):
            environment.pop(name, None)
        for configured in (False, True):
            if configured:
                write_config(
                    home / "config.yaml",
                    {"environment": {LABEL: "home", "KEEP": "home"}},
                )
            for command in ("serve", "sandbox"):
                arguments = (
                    "--no-project-config",
                    "-c",
                    "cache=invalid",
                    "-c",
                    "environment.MCP_CONSOLE_TEST_DISCOVERY=42",
                    command,
                    "-c",
                    "cache=host",
                    "-c",
                    "environment={MCP_CONSOLE_TEST_DISCOVERY: last, ADDED: cli}",
                )
                expected = "last|" + ("home" if configured else "unset") + "|cli"
                observe_environment(binary, workspace, environment, arguments, expected)
                records.append(
                    {"command": command, "home": configured, "environment": expected}
                )
        (home / "config.yaml").unlink()
        for command in ("serve", "sandbox"):
            observe_environment(
                binary,
                workspace,
                environment,
                (command, "--no-project-config"),
                "unset|unset|unset",
            )
            records.append(
                {
                    "command": command,
                    "home": False,
                    "overrides": False,
                    "environment": "unset|unset|unset",
                }
            )
    return records


def test_skips_invalid_project_configuration(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        home = root / "console-home"
        workspace.mkdir()
        home.mkdir()
        (home / "config.yaml").write_text("{}", encoding="utf-8")
        project = workspace / CONFIG
        project.parent.mkdir(parents=True)
        environment = os.environ | {"MCP_CONSOLE_HOME": str(home)}
        for state in ("malformed", "unreadable"):
            if state == "malformed":
                project.write_text("sandbox: [", encoding="utf-8")
            else:
                project.unlink()
                project.mkdir()
            result = subprocess.run(
                [binary, "serve", "--no-sandbox", "--worker", "unused-worker"],
                cwd=workspace,
                env=environment,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert CONFIG in result.stderr, result.stderr
            with McpClient(
                binary,
                (
                    "serve",
                    "--no-project-config",
                    "--no-sandbox",
                    "--worker",
                    "unused-worker",
                ),
                environment=environment,
                current_directory=workspace,
                use_home_configuration=True,
            ) as client:
                client.initialize_and_list_tools()
                _, stderr = client.finish_with_standard_error()
                assert stderr == "", stderr
            records.append(
                {"project": state, "default_error": result.stderr, "skipped": True}
            )
    return records


def test_retains_home_errors_and_final_validation(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        home = root / "console-home"
        home.mkdir()
        write_config(workspace / CONFIG, {})
        config = home / "config.yaml"
        for state in (
            "malformed",
            "unreadable",
            "invalid",
            "invalid override",
            "invalid environment override",
            "relative home",
            "empty home",
        ):
            if config.is_dir():
                config.rmdir()
            elif config.exists():
                config.unlink()
            environment = os.environ | {"MCP_CONSOLE_HOME": str(home)}
            overrides = ()
            if state == "malformed":
                config.write_text("sandbox: [", encoding="utf-8")
            elif state == "unreadable":
                config.mkdir()
            elif state == "invalid":
                write_config(config, {"unknown": "home"})
            elif state == "invalid override":
                write_config(config, {})
                overrides = ("-c", "cache=invalid")
            elif state == "invalid environment override":
                write_config(config, {"environment": {"TOKEN": "global-secret"}})
                write_config(
                    workspace / CONFIG, {"environment": {"TOKEN": "project-secret"}}
                )
                overrides = ("-c", "environment.TOKEN=42")
            else:
                environment["MCP_CONSOLE_HOME"] = (
                    "relative" if state == "relative home" else ""
                )
            for command in ("serve", "sandbox"):
                arguments = (
                    ("serve", "--no-sandbox") if command == "serve" else ("sandbox",)
                )
                tail = () if command == "serve" else ("--", "unused-command")
                result = subprocess.run(
                    [binary, *arguments, *overrides, *tail],
                    cwd=workspace,
                    env=environment,
                    input="",
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert result.returncode == 1 and result.stdout == "", result
                if state in ("relative home", "empty home"):
                    assert (
                        "MCP_CONSOLE_HOME must be an absolute path" in result.stderr
                    ), result.stderr
                elif state == "invalid override":
                    assert "with CLI overrides: cache:" in result.stderr, result.stderr
                elif state == "invalid environment override":
                    assert str(config) in result.stderr and CONFIG in result.stderr, (
                        result.stderr
                    )
                    assert "with CLI overrides: environment:" in result.stderr, (
                        result.stderr
                    )
                    assert (
                        "global-secret" not in result.stderr
                        and "project-secret" not in result.stderr
                    ), result.stderr
                else:
                    assert str(config) in result.stderr, result.stderr
                records.append(
                    {
                        "command": command,
                        "home": state,
                        "stderr": result.stderr.replace(
                            str(config), "<root>/console-home/config.yaml"
                        ),
                    }
                )
    return records


def test_rejects_discovery_control_with_captured_settings(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = os.environ | {"MCP_CONSOLE_HOME": str(root / "home")}
        environment.pop("MCP_CONSOLE_TEST_CAPTURED", None)
        for selector in ("--config-env", "--settings-env"):
            for payload in (None, "not JSON"):
                selected_environment = environment.copy()
                if payload is not None:
                    selected_environment["MCP_CONSOLE_TEST_CAPTURED"] = payload
                for flags in (
                    ("--no-project-config",),
                    ("--no-global-config",),
                    ("--no-config",),
                    ("--config-file", "selected.yaml"),
                    ("--default-config", "sandbox.profile=workspace"),
                ):
                    flag = flags[0]
                    for placement in ("before", "after"):
                        arguments = (
                            (*flags, "sandbox")
                            if placement == "before"
                            else ("sandbox", *flags)
                        )
                        result = subprocess.run(
                            [
                                binary,
                                *arguments,
                                selector,
                                "MCP_CONSOLE_TEST_CAPTURED",
                                "--",
                                "unused-command",
                            ],
                            cwd=root,
                            env=selected_environment,
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        assert result.returncode == 1 and result.stdout == "", result
                        assert (
                            result.stderr
                            == f"{flag} cannot be combined with --config-env or --settings-env\n"
                        ), result
                        records.append(
                            {
                                "selector": selector,
                                "flag": flag,
                                "placement": placement,
                                "payload": payload,
                                "stderr": result.stderr,
                            }
                        )
    return records


def observe_languages(
    binary: Path,
    workspace: Path,
    environment: dict[str, str],
    flags: tuple[str, ...],
    expected: set[str],
) -> None:
    with McpClient(
        binary,
        ("serve", "--no-sandbox", "--worker", "unused-worker", *flags),
        environment=environment,
        current_directory=workspace,
        use_home_configuration=True,
    ) as client:
        client.initialize_and_list_tools()
        properties = client.transcript[2]["result"]["tools"][0]["inputSchema"][
            "properties"
        ]
        assert set(properties) & {"r", "python", "sql"} == expected, properties
        _, stderr = client.finish_with_standard_error()
        assert stderr == "", stderr


def test_sparse_files_final_validation_and_no_ancestor_discovery(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        global_config = root / "global/config.yaml"
        project = workspace / CONFIG
        environment = os.environ | {"MCP_CONSOLE_HOME": str(global_config.parent)}
        environment.pop("MCP_CONSOLE_LANGUAGES", None)
        # An ancestor file must never be inspected, even when malformed.
        (root / CONFIG).parent.mkdir(parents=True)
        (root / CONFIG).write_text("sandbox: [", encoding="utf-8")
        cases = (
            ("no files", None, None, (), {"r", "python", "sql"}),
            ("global alone", {"languages": ["r"]}, None, (), {"r"}),
            ("project alone", None, {"languages": ["python"]}, (), {"python"}),
            ("empty project", {"languages": ["r"]}, {}, (), {"r"}),
            (
                "list replacement",
                {"languages": ["r", "sql"]},
                {"languages": ["python"]},
                (),
                {"python"},
            ),
            (
                "project repairs global",
                {"languages": False, "environment": False},
                {"languages": ["sql"], "environment": {}},
                (),
                {"sql"},
            ),
            (
                "CLI repairs layers",
                {"languages": [], "cache": False},
                {"languages": False},
                ("-c", "languages=[python]", "-c", "cache=host"),
                {"python"},
            ),
        )
        for name, global_value, project_value, flags, expected in cases:
            for path, value in (
                (global_config, global_value),
                (project, project_value),
            ):
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    write_config(path, value)
            observe_languages(binary, workspace, environment, flags, expected)
            records.append({"case": name, "languages": sorted(expected)})
    return records


def test_no_config_excludes_sources_before_home_resolution(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        project = root / CONFIG
        project.parent.mkdir(parents=True)
        project.write_text("sandbox: [", encoding="utf-8")
        global_config = root / "global/config.yaml"
        global_config.parent.mkdir()
        global_config.write_text("sandbox: [", encoding="utf-8")
        for home in (str(global_config.parent), "relative", "", "~/console"):
            environment = os.environ | {"MCP_CONSOLE_HOME": home}
            environment.pop("MCP_CONSOLE_LANGUAGES", None)
            observe_languages(
                binary,
                root,
                environment,
                ("--no-config", "-c", "languages=[python]"),
                {"python"},
            )
            records.append(
                {
                    "home": home.replace(str(root), "<root>").replace("\\", "/"),
                    "skipped": True,
                }
            )
    return records


def test_no_global_config_excludes_source_before_home_resolution(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        write_config(root / CONFIG, {"languages": ["python"]})
        global_config = root / "global/config.yaml"
        global_config.parent.mkdir()
        global_config.write_text("sandbox: [", encoding="utf-8")
        for home in (str(global_config.parent), "relative", "", "~/console"):
            environment = os.environ | {"MCP_CONSOLE_HOME": home}
            environment.pop("MCP_CONSOLE_LANGUAGES", None)
            observe_languages(
                binary, root, environment, ("--no-global-config",), {"python"}
            )
            records.append(
                {
                    "home": home.replace(str(root), "<root>").replace("\\", "/"),
                    "project_loaded": True,
                }
            )
        global_config.unlink()
        global_config.mkdir()
        observe_languages(
            binary,
            root,
            os.environ | {"MCP_CONSOLE_HOME": str(global_config.parent)},
            ("--no-global-config",),
            {"python"},
        )
        records.append({"non_regular_global_skipped": True})
    return records


@requires(SANDBOX, R)
def test_explicit_file_is_the_only_file_in_both_commands(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        global_config = root / "global/config.yaml"
        for path in (global_config, root / CONFIG):
            path.parent.mkdir(parents=True)
            path.write_text("sandbox: [", encoding="utf-8")
        selected = root / "selected/config.yaml"
        selected.parent.mkdir()
        environment = os.environ | {"MCP_CONSOLE_HOME": str(global_config.parent)}
        for name in (LABEL, "KEEP", "ADDED"):
            environment.pop(name, None)
        for format in ("yaml", "json"):
            if format == "yaml":
                selected.write_text(
                    "cache: host\nenvironment:\n  MCP_CONSOLE_TEST_DISCOVERY: selected\n  KEEP: selected\n",
                    encoding="utf-8",
                )
            else:
                write_config(
                    selected,
                    {
                        "cache": "host",
                        "environment": {LABEL: "selected", "KEEP": "selected"},
                    },
                )
            for command in ("serve", "sandbox"):
                for placement, arguments in (
                    ("before", ("--config-file", str(selected), command)),
                    ("after", (command, "--config-file", "selected/config.yaml")),
                    (
                        "ordered overrides",
                        (
                            "-c",
                            "environment.MCP_CONSOLE_TEST_DISCOVERY=first",
                            "--config-file",
                            "selected/config.yaml",
                            command,
                            "-c",
                            "environment.MCP_CONSOLE_TEST_DISCOVERY=last",
                        ),
                    ),
                ):
                    label = "last" if placement == "ordered overrides" else "selected"
                    observe_environment(
                        binary, root, environment, arguments, f"{label}|selected|unset"
                    )
                    records.append(
                        {
                            "format": format,
                            "command": command,
                            "placement": placement,
                            "selected": label,
                        }
                    )
        for home in ("relative", "", "~/console"):
            observe_languages(
                binary,
                root,
                environment | {"MCP_CONSOLE_HOME": home},
                ("--config-file", str(selected), "-c", "languages=[python]"),
                {"python"},
            )
            records.append({"home": home, "discovery_skipped": True})
    return records


@platform_snapshots(
    "win32", reason="Missing selected files report native filesystem errors"
)
def test_missing_explicit_file_is_an_error(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        write_config(root / CONFIG, {})
        for command in ("serve", "sandbox"):
            tail = ("--", "unused-command") if command == "sandbox" else ()
            result = subprocess.run(
                [binary, command, "--config-file", "selected.yaml", *tail],
                cwd=root,
                env=os.environ | {"MCP_CONSOLE_HOME": "relative"},
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert "cannot inspect 'selected.yaml':" in result.stderr, result.stderr
            assert "MCP_CONSOLE_HOME" not in result.stderr, result.stderr
            records.append(
                {
                    "command": command,
                    "exit_code": result.returncode,
                    "stderr": result.stderr,
                }
            )
    return records


def test_explicit_file_errors_do_not_fall_back(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        write_config(root / CONFIG, {"languages": ["python"]})
        selected = root / "selected.yaml"
        environment = os.environ | {"MCP_CONSOLE_HOME": "relative"}
        for state in ("directory", "malformed", "invalid", "empty"):
            if state == "directory":
                selected.mkdir()
            else:
                if selected.is_dir():
                    selected.rmdir()
                if state == "malformed":
                    selected.write_text("sandbox: [", encoding="utf-8")
                elif state == "invalid":
                    write_config(selected, {"languages": []})
                elif state == "empty":
                    selected.write_text("", encoding="utf-8")
            for command in ("serve", "sandbox"):
                tail = ("--", "unused-command") if command == "sandbox" else ()
                result = subprocess.run(
                    [binary, command, "--config-file", "selected.yaml", *tail],
                    cwd=root,
                    env=environment,
                    input="",
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert result.returncode == 1 and result.stdout == "", result
                assert "selected.yaml" in result.stderr, result.stderr
                assert "MCP_CONSOLE_HOME" not in result.stderr, result.stderr
                if state == "directory":
                    assert "must be a regular file" in result.stderr, result.stderr
                elif state == "invalid":
                    assert "languages must contain at least one" in result.stderr, (
                        result.stderr
                    )
                records.append(
                    {
                        "state": state,
                        "command": command,
                        "exit_code": result.returncode,
                        "stderr": result.stderr,
                    }
                )
        write_config(selected, {})
        observe_languages(
            binary,
            root,
            environment,
            ("--config-file", "selected.yaml"),
            {"r", "python", "sql"},
        )
        records.append({"empty_mapping_uses_builtin_defaults": True})
    return records


def test_explicit_file_rejects_conflicting_selectors(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for command in ("serve", "sandbox"):
            tail = ("--", "unused-command") if command == "sandbox" else ()
            for flag in ("--no-config", "--no-global-config", "--no-project-config"):
                for arguments in (
                    ("--config-file", "selected.yaml", command, flag),
                    (flag, command, "--config-file", "selected.yaml"),
                ):
                    result = subprocess.run(
                        [binary, *arguments, *tail],
                        cwd=root,
                        env=os.environ | {"MCP_CONSOLE_HOME": "relative"},
                        input="",
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    assert result.returncode == 1 and result.stdout == "", result
                    assert (
                        result.stderr
                        == "--config-file cannot be combined with --no-config, --no-global-config, or --no-project-config\n"
                    ), result
                    records.append(
                        {
                            "command": command,
                            "arguments": list(arguments),
                            "stderr": result.stderr,
                        }
                    )
            result = subprocess.run(
                [
                    binary,
                    "--config-file",
                    "first.yaml",
                    command,
                    "--config-file",
                    "second.yaml",
                    *tail,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert result.stderr == "--config-file may be supplied only once\n", result
            records.append(
                {
                    "command": command,
                    "duplicate_file_rejected": True,
                    "stderr": result.stderr,
                }
            )
    return records


def test_home_directory_discovery_deduplicates_after_exclusions(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        home = Path(temporary).resolve()
        config = home / CONFIG
        write_config(config, {"languages": ["python"]})
        environment = os.environ | {"HOME": str(home)}
        environment.pop("MCP_CONSOLE_HOME", None)
        environment.pop("MCP_CONSOLE_LANGUAGES", None)
        for flags in ((), ("--no-project-config",), ("--no-global-config",)):
            observe_languages(binary, home, environment, flags, {"python"})
        write_config(config, {"languages": []})
        result = subprocess.run(
            [binary, "serve", "--no-sandbox"],
            cwd=home,
            env=environment,
            input="",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1 and result.stdout == "", result
        assert result.stderr.count(str(config)) == 1, result.stderr
        assert "configuration from" not in result.stderr, result.stderr
        records.append(
            {
                "home_and_project_loaded_once": True,
                "global_survives_project_exclusion": True,
                "project_survives_global_exclusion": True,
            }
        )
    return records


@requires(UNPRIVILEGED)
def test_selected_unreadable_files_fail_and_excluded_files_are_skipped(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        project = root / CONFIG
        global_config = root / "global/config.yaml"
        environment = os.environ | {"MCP_CONSOLE_HOME": str(global_config.parent)}
        environment.pop("MCP_CONSOLE_LANGUAGES", None)
        for selected in (global_config, project):
            write_config(selected, {})
            selected.chmod(0)
            try:
                result = subprocess.run(
                    [binary, "serve", "--no-sandbox", "--worker", "unused-worker"],
                    cwd=root,
                    env=environment,
                    input="",
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert result.returncode == 1 and result.stdout == "", result
                assert (
                    "cannot read" in result.stderr and selected.name in result.stderr
                ), result.stderr
                flags = (
                    ("--no-global-config",)
                    if selected == global_config
                    else ("--no-project-config",)
                )
                observe_languages(
                    binary, root, environment, flags, {"r", "python", "sql"}
                )
            finally:
                selected.chmod(0o600)
                selected.unlink()
    return [
        {
            "unreadable_global_and_project_rejected": True,
            "excluded_sources_skipped": True,
        }
    ]


def test_global_python_path_is_launch_relative(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        global_config = root / "global/config.yaml"
        # The test runner and temporary workspace can be on different Windows drives.
        python_home = root / "python"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(python_home)],
            check=True,
        )
        python = virtualenv_python(python_home)
        relative_python = os.path.relpath(python, workspace)
        write_config(global_config, {"cache": "host", "python": relative_python})
        with McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment=os.environ
            | {
                "MCP_CONSOLE_HOME": str(global_config.parent),
                "MCP_CONSOLE_TEST_EXPECTED_PYTHON": str(python),
            },
            current_directory=workspace,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "launch-relative Python verified\n",
                # fmt: python
                python=code("""
                    import os
                    import sys

                    assert os.path.samefile(sys.executable, os.environ["MCP_CONSOLE_TEST_EXPECTED_PYTHON"])
                    print("launch-relative Python verified")
                    """),
            )
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr
    return [{"global_python_is_launch_relative": True}]


if __name__ == "__main__":
    run_this_suite(__file__)
