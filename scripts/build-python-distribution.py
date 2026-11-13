#!/usr/bin/env python3
"""Prepare GitHub native bundles and a pure Python project from audited wheels.

Run the existing native ABI and installed-runtime acceptance first. This command
checks identities and layout; it does not replace those platform-specific gates.
It refuses additional native install layouts until their relocation is audited.
"""

import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
import re
import shutil
import stat
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

import release

ROOT = Path(__file__).resolve().parent.parent
PLATFORMS = {
    "aarch64-apple-darwin": r"macosx_\d+_\d+_arm64",
    "x86_64-apple-darwin": r"macosx_\d+_\d+_x86_64",
    "aarch64-unknown-linux-gnu": r"manylinux_\d+_\d+_aarch64",
    "x86_64-unknown-linux-gnu": r"manylinux_\d+_\d+_x86_64",
    "x86_64-pc-windows-msvc": r"win_amd64",
}


def require(condition, message):
    release.require(condition, message)


def python_file(name, content):
    # These are the only package files copied into the independently built
    # wrapper. Do not silently omit a newly introduced native dependency.
    magic = content[:4]
    return (
        (Path(name).suffix in {".py", ".pyi", ".json"} or Path(name).name == "py.typed")
        and not content.startswith((b"\x7fELF", b"MZ"))
        and magic not in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"}
    )


def safe_path(name):
    path = PurePosixPath(name.rstrip("/"))
    require(
        bool(path.parts)
        and not path.is_absolute()
        and "\\" not in name
        and ":" not in name
        and "\0" not in name
        and path.parts[0].casefold() != ".complete"
        and all(
            part not in {"", ".", ".."} and not part.endswith((".", " "))
            for part in name.rstrip("/").split("/")
        ),
        f"unsafe archive path: {name}",
    )


def bundle(wheel, directory, version):
    found_version, tags = release.wheel_identity(wheel)
    require(
        found_version == version,
        f"wheel version {found_version} differs from Cargo version {version}",
    )
    matches = [
        target
        for target, pattern in PLATFORMS.items()
        if all(re.fullmatch("py3-none-" + pattern, tag) for tag in tags)
    ]
    require(len(matches) == 1, f"unsupported or mixed wheel tags: {wheel.name}")
    target = matches[0]
    windows = "windows" in target
    executable = "libexec/mcp-console.exe" if windows else "bin/mcp-console"
    required = {
        executable,
        "libexec/mcp-console-sandbox" + (".exe" if windows else ""),
        "share/licenses/mcp-console/LICENSE",
        "share/licenses/mcp-console/NOTICE",
    }
    if "linux" in target:
        required.update(
            {
                "libexec/bwrap",
                *(
                    "share/licenses/mcp-console/" + name
                    for name in (
                        "bubblewrap-COPYING",
                        "bubblewrap-NOTICE",
                        "bubblewrap-SOURCE.json",
                    )
                ),
            }
        )
    filename = f"mcp-console-{version}-{target}.zip"
    destination = directory / filename
    written = set()
    folded = set()
    data = f"mcp_console-{version}.data/"
    metadata = f"mcp_console-{version}.dist-info/"
    with (
        zipfile.ZipFile(wheel) as source,
        zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive,
    ):

        def write(name, content, mode):
            safe_path(name)
            require(name.casefold() not in folded, f"duplicate bundle path: {name}")
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
            written.add(name)
            folded.add(name.casefold())

        for info in sorted(source.infolist(), key=lambda info: info.filename):
            name = info.filename
            safe_path(name)
            require(
                stat.S_IFMT(info.external_attr >> 16)
                in {0, stat.S_IFREG, stat.S_IFDIR},
                f"unsupported archive member type: {name}",
            )
            if info.is_dir():
                continue
            content = source.read(info)
            if name.startswith(data + "scripts/"):
                name = "bin/" + name.removeprefix(data + "scripts/")
            elif name.startswith(data + "data/"):
                name = name.removeprefix(data + "data/")
            elif name.startswith(metadata):
                continue
            elif name.startswith("mcp_console/") and python_file(name, content):
                continue
            else:
                raise release.ReleaseError(
                    f"unhandled wheel member; audit its native relocation before proceeding: {name}"
                )
            mode = stat.S_IMODE(info.external_attr >> 16) & 0o777
            require(
                windows
                or name
                not in {executable, "libexec/mcp-console-sandbox", "libexec/bwrap"}
                or mode & 0o111,
                f"native command is not executable: {name}",
            )
            write(name, content, mode or 0o644)
        require(
            required <= written,
            f"bundle is missing: {', '.join(sorted(required - written))}",
        )
        write(
            "share/licenses/mcp-console/console-LICENSE",
            (ROOT / "LICENSE").read_bytes(),
            0o644,
        )
    with destination.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    artifact = {
        "filename": filename,
        "sha256": digest,
        "size": destination.stat().st_size,
        "executable": executable,
    }
    if "apple" in target:
        floors = [
            tuple(map(int, re.search(r"macosx_(\d+)_(\d+)_", tag).groups()))
            for tag in tags
        ]
        artifact["minimum_macos"] = ".".join(map(str, min(floors)))
    return target, artifact


def toml_tables(value, path=()):
    # Project metadata currently contains only TOML strings, arrays and tables.
    # JSON's scalar/array syntax is also valid TOML for those supported types.
    lines = ["[" + ".".join(json.dumps(part) for part in path) + "]"] if path else []
    for key, item in value.items():
        if not isinstance(item, dict):
            require(
                isinstance(item, (str, list)),
                f"unsupported project metadata field: {key}",
            )
            lines.append(f"{json.dumps(key)} = {json.dumps(item, ensure_ascii=False)}")
    for key, item in value.items():
        if isinstance(item, dict):
            lines.extend(["", *toml_tables(item, (*path, key))])
    return lines


def prepare_project(directory, manifest):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    dynamic = [field for field in project.pop("dynamic", []) if field != "version"]
    require(not dynamic, f"unsupported dynamic project metadata: {dynamic}")
    project["version"] = manifest["version"]
    # Current setuptools rejects legacy license classifiers alongside the
    # source project's SPDX license expression; retain the expression.
    project["classifiers"] = [
        value
        for value in project.get("classifiers", [])
        if not value.startswith("License ::")
    ]
    project["scripts"] = project.get("scripts", {}) | {
        "mcp-console": "mcp_console._launcher:main",
        "mcp-console-install": "mcp_console._binary:main",
    }
    metadata = {
        "build-system": {
            "requires": ["setuptools>=77"],
            "build-backend": "setuptools.build_meta",
        },
        "project": project,
        "tool": {
            "setuptools": {
                "packages": {"find": {"where": ["python"]}},
                "package-data": {"mcp_console": ["_artifacts.json"]},
            }
        },
    }
    directory.mkdir()
    (directory / "pyproject.toml").write_text(
        "\n".join(toml_tables(metadata)) + "\n", encoding="utf-8"
    )
    for name in ("README.md", "LICENSE"):
        shutil.copyfile(ROOT / name, directory / name)
    package = directory / "python/mcp_console"
    for source in sorted((ROOT / "python/mcp_console").rglob("*")):
        if "__pycache__" in source.parts or not source.is_file():
            continue
        require(
            not source.is_symlink() and python_file(source.name, source.read_bytes()),
            f"unhandled Python package file: {source}",
        )
        destination = package / source.relative_to(ROOT / "python/mcp_console")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    (package / "_artifacts.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "wheels", type=Path, help="directory containing all five audited native wheels"
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="new output directory (must not exist)"
    )
    parser.add_argument(
        "--allow-dev",
        action="store_true",
        help="allow +dev versions for local rehearsal only",
    )
    args = parser.parse_args()
    version = tomllib.loads((ROOT / "Cargo.toml").read_text())["package"]["version"]
    require(
        re.fullmatch(r"\d+\.\d+\.\d+(?:\+dev)?", version),
        f"unsupported version: {version}",
    )
    require(
        not version.endswith("+dev") or args.allow_dev,
        "development versions require --allow-dev for local rehearsal",
    )
    require(not args.out.exists(), f"output already exists: {args.out}")
    with redirect_stdout(io.StringIO()):
        release.verify_wheel_set(argparse.Namespace(directory=args.wheels))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".python-distribution-", dir=args.out.parent
    ) as temporary:
        staging = Path(temporary) / "prepared"
        native = staging / "native"
        native.mkdir(parents=True)
        artifacts = dict(
            bundle(wheel, native, version)
            for wheel in sorted(args.wheels.glob("*.whl"))
        )
        require(
            set(artifacts) == set(PLATFORMS),
            "expected one bundle for each supported target",
        )
        prepare_project(
            staging / "python-project",
            {
                "schema": 1,
                "version": version,
                "release": "v" + version,
                "artifacts": artifacts,
            },
        )
        staging.rename(args.out)
    print(f"Prepared five native bundles and a pure Python project under {args.out}")


if __name__ == "__main__":
    try:
        main()
    except (
        release.ReleaseError,
        OSError,
        ValueError,
        KeyError,
        zipfile.BadZipFile,
    ) as error:
        sys.exit(f"build-python-distribution: {error}")
