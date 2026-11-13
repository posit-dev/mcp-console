#!/usr/bin/env python3
"""Repackage five audited native wheels as native bundles and one pure wheel.

Each ZIP under `native/` is one target's installation prefix, for a GitHub
release. The `py3-none-any` wheel under `pypi/` carries the Python package and
metadata the native wheels share, plus a manifest pinning each ZIP's size and
SHA-256. Run the native ABI and installed-runtime acceptance first: this
command relocates wheel members and does not replace those gates.
"""

import argparse
import base64
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import sys
import tempfile
import zipfile

import release
from release import require

# Each target's wheel platform tag, and the manifest field for its OS floor.
PLATFORMS = {
    "aarch64-apple-darwin": (r"macosx_(\d+)_(\d+)_arm64", "minimum_macos"),
    "x86_64-apple-darwin": (r"macosx_(\d+)_(\d+)_x86_64", "minimum_macos"),
    "aarch64-unknown-linux-gnu": (r"manylinux_(\d+)_(\d+)_aarch64", "minimum_glibc"),
    "x86_64-unknown-linux-gnu": (r"manylinux_(\d+)_(\d+)_x86_64", "minimum_glibc"),
    "x86_64-pc-windows-msvc": (r"win_amd64", None),
}
LICENSES = "share/licenses/mcp-console/"
ENTRY_POINTS = """\
[console_scripts]
mcp-console = mcp_console._launcher:main
mcp-console-install = mcp_console._binary:main
"""
WHEEL = """\
Wheel-Version: 1.0
Generator: build-python-distribution
Root-Is-Purelib: true
Tag: py3-none-any
"""


def identify(wheel):
    version, tags = release.wheel_identity(wheel)
    for target, (pattern, floor) in PLATFORMS.items():
        matches = [re.fullmatch("py3-none-" + pattern, tag) for tag in tags]
        if all(matches):
            minimum = min(tuple(map(int, match.groups())) for match in matches)
            return version, target, {floor: ".".join(map(str, minimum))} if floor else {}
    raise release.ReleaseError(f"unsupported or mixed wheel tags: {wheel.name}")


def split(wheel, version, target):
    """Return a wheel's native prefix and its platform-independent members."""
    data = f"mcp_console-{version}.data/"
    metadata = f"mcp_console-{version}.dist-info/"
    native, shared = {}, {}
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            name = info.filename
            if info.is_dir():
                continue
            content = archive.read(info)
            mode = 0o755 if info.external_attr >> 16 & 0o111 else 0o644
            if name.startswith(data + "scripts/"):
                native["bin/" + name.removeprefix(data + "scripts/")] = content, mode
            elif name.startswith(data + "data/"):
                native[name.removeprefix(data + "data/")] = content, mode
            elif name.startswith(metadata + "sboms/"):
                # The SBOM describes the native build, so it follows the bundle.
                native["share/" + name.removeprefix(metadata)] = content, 0o644
            elif name == metadata + "METADATA" or name.startswith(
                metadata + "licenses/"
            ):
                # A Windows checkout gives the README and license CRLF line
                # endings; nothing else distinguishes these members by target.
                shared[name] = content.replace(b"\r\n", b"\n")
            elif name.startswith("mcp_console/"):
                shared[name] = content
            else:
                replaced = ("WHEEL", "RECORD", "entry_points.txt")
                require(
                    name in {metadata + member for member in replaced},
                    f"unhandled wheel member; audit its relocation first: {name}",
                )
    native[LICENSES + "console-LICENSE"] = shared[metadata + "licenses/LICENSE"], 0o644
    windows = "windows" in target
    commands = {
        "libexec/mcp-console.exe" if windows else "bin/mcp-console",
        "libexec/mcp-console-sandbox" + (".exe" if windows else ""),
    }
    notices = ["LICENSE", "NOTICE"]
    if "linux" in target:
        commands.add("libexec/bwrap")
        notices += ["bubblewrap-COPYING", "bubblewrap-NOTICE", "bubblewrap-SOURCE.json"]
    required = commands | {LICENSES + name for name in notices}
    require(
        required <= set(native),
        f"{wheel.name} is missing: {', '.join(sorted(required - set(native)))}",
    )
    # The installer restores only the executable bit recorded in the ZIP.
    require(
        windows or all(native[name][1] == 0o755 for name in commands),
        f"{wheel.name} has a native command that is not executable",
    )
    return native, shared


def write_archive(path, members):
    """Write members in the given order with fixed timestamps."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, (content, mode) in members:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "wheels", type=Path, help="directory containing all five audited native wheels"
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="new output directory (must not exist)"
    )
    args = parser.parse_args()
    require(not args.out.exists(), f"output already exists: {args.out}")
    with redirect_stdout(io.StringIO()):
        release.verify_wheel_set(argparse.Namespace(directory=args.wheels))
    wheels = sorted(args.wheels.glob("*.whl"))
    identities = [identify(wheel) for wheel in wheels]
    versions = {version for version, _, _ in identities}
    require(len(versions) == 1, f"wheels differ in version: {sorted(versions)}")
    (version,) = versions
    require(
        {target for _, target, _ in identities} == set(PLATFORMS),
        "expected one wheel for each supported target",
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".python-distribution-", dir=args.out.parent
    ) as temporary:
        staging = Path(temporary) / "prepared"
        (staging / "native").mkdir(parents=True)
        (staging / "pypi").mkdir()
        artifacts = {}
        package = None
        for wheel, (_, target, floor) in zip(wheels, identities):
            native, shared = split(wheel, version, target)
            package = package or shared
            differing = sorted(
                name
                for name in package.keys() | shared.keys()
                if package.get(name) != shared.get(name)
            )
            require(
                not differing,
                "platform-independent members differ between wheels: "
                + ", ".join(differing),
            )
            bundle = staging / f"native/mcp-console-{version}-{target}.zip"
            write_archive(bundle, sorted(native.items()))
            content = bundle.read_bytes()
            artifacts[target] = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            } | floor
        metadata = f"mcp_console-{version}.dist-info/"
        manifest = {"version": version, "artifacts": artifacts}
        package |= {
            "mcp_console/_artifacts.json": (
                json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            ).encode(),
            metadata + "WHEEL": WHEEL.encode(),
            metadata + "entry_points.txt": ENTRY_POINTS.encode(),
        }
        # Installers expect the metadata directory, and RECORD within it, last.
        names = sorted(package, key=lambda name: (name.startswith(metadata), name))
        record = "".join(
            f"{name},sha256="
            + base64.urlsafe_b64encode(hashlib.sha256(package[name]).digest())
            .rstrip(b"=")
            .decode()
            + f",{len(package[name])}\n"
            for name in names
        )
        package[metadata + "RECORD"] = (record + f"{metadata}RECORD,,\n").encode()
        write_archive(
            staging / f"pypi/mcp_console-{version}-py3-none-any.whl",
            [(name, (package[name], 0o644)) for name in [*names, metadata + "RECORD"]],
        )
        staging.rename(args.out)
    print(f"Prepared five native bundles and one pure Python wheel under {args.out}")


if __name__ == "__main__":
    try:
        main()
    except release.ReleaseError as error:
        sys.exit(f"build-python-distribution: {error}")
