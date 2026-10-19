"""Local binding is resolved only after file and command-line layering."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.suites import run_this_suite


def test_local_binding_values_and_layering(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        project = root / ".agents/console"
        home.mkdir()
        project.mkdir(parents=True)
        scopes = ("sandbox", "resolver.sandbox")
        for scope in scopes:

            def document(value):
                sandbox = {"network": {"proxy": {}} | value}
                return (
                    {"sandbox": sandbox}
                    if scope == "sandbox"
                    else {"resolver": {"sandbox": sandbox}}
                )

            def resolve(*overrides):
                return subprocess.run(
                    [
                        binary,
                        "config",
                        "resolve",
                        "--full",
                        "--format",
                        "json",
                        *overrides,
                    ],
                    cwd=root,
                    env=os.environ | {"MCP_CONSOLE_HOME": str(home)},
                    capture_output=True,
                    text=True,
                    timeout=15,
                )

            def check(value, *overrides):
                result = resolve(*overrides)
                auto = sys.platform == "win32" or scope == "resolver.sandbox"
                expected = auto if value is None or value == "auto" else value
                if sys.platform == "win32" and expected is False:
                    assert result.returncode == 1 and result.stdout == "", result
                    assert (
                        "allow_local_binding" in result.stderr
                        and "false" in result.stderr
                    ), result
                else:
                    assert result.returncode == 0 and result.stderr == "", result
                    resolved = json.loads(result.stdout)
                    for key in scope.split("."):
                        resolved = resolved[key]
                    assert resolved["network"]["allow_local_binding"] is expected, (
                        resolved
                    )

            for fields in (
                {},
                *({"allow_local_binding": v} for v in ("auto", None, True, False)),
            ):
                (home / "config.yaml").write_text(json.dumps(document(fields)))
                (project / "config.yaml").write_text("{}")
                check(fields.get("allow_local_binding"))

            for earlier in (True, False):
                (home / "config.yaml").write_text(
                    json.dumps(document({"allow_local_binding": earlier}))
                )
                for later in ("auto", None, True, False):
                    (project / "config.yaml").write_text(
                        json.dumps(document({"allow_local_binding": later}))
                    )
                    check(later)
                    check(
                        earlier,
                        "-c",
                        f"{scope}.network.allow_local_binding={json.dumps(earlier)}",
                    )
                    check(
                        later,
                        "-c",
                        f"{scope}.network.allow_local_binding={json.dumps(earlier)}",
                        "-c",
                        f"{scope}.network.allow_local_binding={json.dumps(later)}",
                    )

            (project / "config.yaml").write_text("{}")
            for value in ("true", "false", "AUTO", "", 0, 1, [], {}):
                result = resolve(
                    "-c", f"{scope}.network.allow_local_binding={json.dumps(value)}"
                )
                assert result.returncode == 1 and result.stdout == "", result
                assert "allow_local_binding" in result.stderr, result
        # Exact host choices are asserted above. This shared transcript describes
        # the same public contract without duplicating OS-specific defaults.
        return [
            {
                "omitted_auto_and_null": "resolve to the platform/context default",
                "booleans": "honored or explicitly rejected when unsupported",
                "layering": "global, project, then ordered CLI overrides",
                "invalid_values": "rejected before launch",
            }
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
