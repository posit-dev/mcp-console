"""Interpreter identity and script arguments through the public MCP API."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_peer_runtime import without_r
from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.python import virtualenv_python
from support.records import Transcript
from support.requirements import R, command, requires


# fmt: python
CHILD_SOURCE = code("""
    import json
    import os
    import sys

    print(
        json.dumps(
            dict(
                identity=[
                    sys.executable,
                    sys._base_executable,
                    sys.prefix,
                    sys.exec_prefix,
                    sys.base_prefix,
                    sys.base_exec_prefix,
                ],
                argv=sys.argv,
                orig_argv=sys.orig_argv,
                virtualenv=os.environ.get("VIRTUAL_ENV"),
            )
        )
    )
    """)


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_launches_children_without_confusing_executable_and_script_arguments(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    with tempfile.TemporaryDirectory() as directory:
        # Framework Python may report /private/var for a /var virtualenv.
        # Create the fixture at its canonical path to keep identity checks exact.
        root = Path(directory).resolve()
        selected = root / "selected λ environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(selected)], check=True
        )
        executable = virtualenv_python(selected)
        subprocess.run(
            ["uv", "pip", "install", "--python", str(executable), "numpy"],
            check=True,
            capture_output=True,
        )
        site = subprocess.check_output(
            [
                str(executable),
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
        ).strip()
        native_program_name = subprocess.check_output(
            [
                str(executable),
                "-c",
                "import json, sys; print(json.dumps(sys.orig_argv[0]))",
            ],
            text=True,
        )
        (Path(site) / "sitecustomize.py").write_text(
            # fmt: python
            code("""
                import builtins
                import sys

                builtins.startup_identity = (sys.executable, sys.argv[:], sys.orig_argv[:])
                """),
            encoding="utf-8",
        )
        for mode in ("without-r", "python-first", "r-first"):
            workspace = root / mode
            workspace.mkdir()
            for name in ("identity λ.py", "identity_module.py"):
                (workspace / name).write_text(CHILD_SOURCE, encoding="utf-8")
            # Framework launchers can re-exec a separate native executable.
            (workspace / "native_program_name.json").write_text(
                native_program_name, encoding="utf-8"
            )
            environment = dict(os.environ, RETICULATE_PYTHON=str(executable))
            if mode == "without-r":
                without_r(environment, workspace)
            with McpClient(binary, execution.serve(), environment, workspace) as client:
                client.initialize_and_list_tools()
                if mode == "r-first":
                    client.send(r="invisible(reticulate::py_config())")
                    assert last_result_text(client) == "[done]"
                client.send(
                    # fmt: python
                    python=code(r"""
                        import builtins
                        import json
                        import os
                        import shutil
                        import subprocess
                        import sys
                        from pathlib import Path

                        assert sys.executable == os.environ["RETICULATE_PYTHON"]
                        assert sys.argv == [""], sys.argv
                        assert sys.orig_argv == [sys.executable], sys.orig_argv
                        assert builtins.startup_identity == (sys.executable, [""], [sys.executable])
                        assert os.environ["VIRTUAL_ENV"] == sys.prefix
                        assert Path(shutil.which("python")).samefile(sys.executable)
                        assert "PYTHONHOME" not in os.environ
                        assert "PYTHONPLATLIBDIR" not in os.environ
                        assert "__PYVENV_LAUNCHER__" not in os.environ
                        identity = [
                            sys.executable,
                            sys._base_executable,
                            sys.prefix,
                            sys.exec_prefix,
                            sys.base_prefix,
                            sys.base_exec_prefix,
                        ]
                        program = Path("identity_module.py").read_text(encoding="utf-8")
                        native_program_name = json.loads(
                            Path("native_program_name.json").read_text(encoding="utf-8")
                        )
                        user = ["two words", "λ", "--literal"]
                        invocations = [
                            ([], [""], program),
                            (["-c", program, *user], ["-c", *user], None),
                            (["identity λ.py", *user], ["identity λ.py", *user], None),
                            (
                                ["-m", "identity_module", *user],
                                [str(Path("identity_module.py").resolve()), *user],
                                None,
                            ),
                            (["-", *user], ["-", *user], program),
                        ]
                        for arguments, expected_argv, stdin in invocations:
                            child = json.loads(
                                subprocess.check_output([sys.executable, *arguments], input=stdin, text=True)
                            )
                            assert all(
                                Path(a).samefile(b) for a, b in zip(child["identity"], identity, strict=True)
                            ), (child, identity)
                            assert child["argv"] == expected_argv, child
                            assert child["orig_argv"] == [native_program_name, *arguments], child
                            assert child["virtualenv"] == sys.prefix, child
                        sys.argv[:] = ["user script.py", *user]
                        sys.orig_argv[:] = [sys.executable, "user script.py", *user]
                        print("Python interpreter and children retain executable, arguments, and environment")
                        """),
                )
                assert last_result_text(client) == (
                    "Python interpreter and children retain executable, arguments, and environment\n"
                ), client.transcript[-1]
                if mode != "without-r":
                    client.send(r="invisible(reticulate::py_config())")
                    assert last_result_text(client) == "[done]", client.transcript[-1]
                client.send(
                    # fmt: python
                    python=code("""
                        assert sys.argv == ["user script.py", "two words", "λ", "--literal"]
                        assert sys.orig_argv == [
                            sys.executable,
                            "user script.py",
                            "two words",
                            "λ",
                            "--literal",
                        ]
                        print("user argv retained across cells and R attachment")
                        """),
                )
                assert (
                    last_result_text(client)
                    == "user argv retained across cells and R attachment\n"
                )
                records.extend(client.finish()[3:])
    return records
