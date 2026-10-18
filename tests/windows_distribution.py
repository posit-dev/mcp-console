"""Inspect Windows distribution artifacts without a compiler or PE dependency.

Run against a wheel, or use the same check for native and installed bundles.
UCRT and ordinary Windows system DLLs (including msvcrt.dll) are allowed.
"""

import argparse
import json
from pathlib import Path
import re
import struct
import subprocess
import zipfile


VC_RUNTIME = re.compile(
    r"(?:vcruntime|msvcp|msvcr|concrt|vcomp|vcamp|vccorlib|mfcm?|atl)\d[^/\\]*\.dll",
    re.IGNORECASE,
)
NATIVE_SUFFIXES = {".exe", ".dll", ".pyd"}


def pe_imports(content):
    def unpack(format, offset):
        return struct.unpack_from("<" + format, content, offset)

    assert content[:2] == b"MZ", "not a PE image"
    (pe,) = unpack("I", 0x3C)
    assert content[pe : pe + 4] == b"PE\0\0", "missing PE signature"
    _, section_count, _, _, _, optional_size, _ = unpack("HHIIIHH", pe + 4)
    optional = pe + 24
    (magic,) = unpack("H", optional)
    assert magic in {0x10B, 0x20B}, "unsupported PE optional header"
    pe64 = magic == 0x20B
    (image_base,) = unpack("Q" if pe64 else "I", optional + (24 if pe64 else 28))
    (directory_count,) = unpack("I", optional + (108 if pe64 else 92))
    directories = optional + (112 if pe64 else 96)
    (header_size,) = unpack("I", optional + 60)
    sections = [
        unpack("IIII", optional + optional_size + 40 * index + 8)
        for index in range(section_count)
    ]

    def file_offset(rva, size):
        if rva < header_size and rva + size <= header_size:
            offset = rva
        else:
            offset = None
            for _, address, raw_size, raw_offset in sections:
                if address <= rva and rva + size <= address + raw_size:
                    offset = raw_offset + rva - address
                    break
        assert offset is not None and offset + size <= len(content), (
            f"unmapped PE RVA {rva:#x} ({size} bytes)"
        )
        return offset

    result = {}
    # IMAGE_IMPORT_DESCRIPTOR and IMAGE_DELAYLOAD_DESCRIPTOR. Delay names can
    # be RVAs or, in the older descriptor format, image-base-relative VAs.
    for kind, index, words, name_index in (
        ("normal", 1, 5, 3),
        ("delayed", 13, 8, 1),
    ):
        imports = set()
        result[kind] = []
        if index >= directory_count:
            continue
        assert directories + (index + 1) * 8 <= optional + optional_size
        rva, size = unpack("II", directories + index * 8)
        if not rva:
            assert not size, "import size without an import directory"
            continue
        descriptor_size = words * 4
        for position in range(0, size - descriptor_size + 1, descriptor_size):
            descriptor = unpack(
                "I" * words, file_offset(rva + position, descriptor_size)
            )
            if not any(descriptor):
                break
            name_rva = descriptor[name_index]
            if kind == "delayed" and not descriptor[0] & 1:
                name_rva -= image_base
            name = bytearray()
            while (byte := content[file_offset(name_rva + len(name), 1)]) != 0:
                name.append(byte)
            imports.add(name.decode("ascii"))
        else:
            raise AssertionError(f"unterminated {kind} import directory")
        result[kind] = sorted(imports, key=str.lower)
    return result


def inspect_artifacts(artifacts):
    reports = {}
    rejected = []
    for name, content in artifacts:
        imports = pe_imports(content)
        reports[str(name)] = imports
        for kind, names in imports.items():
            rejected.extend(
                f"{name}: {kind} import {dll}"
                for dll in names
                if VC_RUNTIME.fullmatch(dll)
            )
    assert reports, "no native distribution artifacts inspected"
    print(json.dumps(reports, indent=2), flush=True)
    assert not rejected, "redistributable VC runtime dependencies:\n" + "\n".join(
        rejected
    )
    return reports


def inspect_wheel(wheel):
    with zipfile.ZipFile(wheel) as archive:
        return inspect_artifacts(
            (name, archive.read(name))
            for name in archive.namelist()
            if Path(name).suffix.lower() in NATIVE_SUFFIXES
        )


def inspect_installation(binary):
    # uv's copied public command is an environment-bound Python launcher. Its
    # shebang selects the interpreter whose distribution metadata owns the
    # complete bundle; inspecting just the copied launcher would miss Console.
    with zipfile.ZipFile(binary) as launcher:
        shebang = launcher.read("__main__.py").decode().splitlines()[0]
    assert shebang.startswith("#!"), "missing installed launcher interpreter"
    python = Path(shebang[2:].strip('"'))
    files = json.loads(
        subprocess.check_output(
            [
                python,
                "-c",
                "import json; from importlib.metadata import distribution; "
                "print(json.dumps([str(p.locate()) for p in distribution('mcp-console').files]))",
            ],
            text=True,
        )
    )
    paths = [
        Path(name) for name in files if Path(name).suffix.lower() in NATIVE_SUFFIXES
    ]
    reports = inspect_artifacts((path, path.read_bytes()) for path in paths)
    (native,) = (
        path for path in paths if path.parts[-2:] == ("libexec", "mcp-console.exe")
    )
    return native, reports


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    inspect_wheel(parser.parse_args().wheel)
