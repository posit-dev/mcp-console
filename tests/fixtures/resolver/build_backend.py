"""Minimal PEP 517 backend for an actual resolver source build."""

from pathlib import Path
import zipfile

from trust_probe import probe


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, str] | None = None,
    metadata_directory: str | None = None,
) -> str:
    probe()
    name = "mcp_console_build_probe-1.0.0-py3-none-any.whl"
    info = "mcp_console_build_probe-1.0.0.dist-info"
    entries = {
        "mcp_console_build_probe.py": "answer = 42\n",
        f"{info}/METADATA": "Metadata-Version: 2.3\nName: mcp-console-build-probe\nVersion: 1.0.0\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    entries[f"{info}/RECORD"] = "\n".join(f"{entry},," for entry in entries) + "\n"
    with zipfile.ZipFile(Path(wheel_directory) / name, "w") as wheel:
        for path, contents in entries.items():
            wheel.writestr(path, contents)
    return name
