"""Describe one already-selected CPython executable for native embedding."""

import ctypes
import json
import importlib.metadata
import os
import re
import struct
import sys
import sysconfig
from pathlib import Path


def describe() -> dict[str, object]:
    if sys.implementation.name != "cpython":
        raise RuntimeError("native embedding requires CPython")

    if sys.version_info < (3, 10):
        raise RuntimeError("MCP Console requires Python 3.10 or later")

    if sys.platform == "win32":
        # The running interpreter exposes its exact DLL, also in virtualenvs.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        filename = kernel.GetModuleFileNameW
        filename.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
        filename.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        length = filename(sys.dllhandle, buffer, len(buffer))
        if not length or length == len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        library = Path(buffer.value)
    else:
        framework = sysconfig.get_config_var("PYTHONFRAMEWORK")
        root_name = "PYTHONFRAMEWORKPREFIX" if framework else "LIBDIR"
        root = sysconfig.get_config_var(root_name)
        library_name = sysconfig.get_config_var("LDLIBRARY")
        if not root or not library_name:
            raise RuntimeError(
                "selected Python has no shared embedding library configuration"
            )
        if Path(library_name).is_absolute():
            raise RuntimeError(
                "selected Python returned an invalid shared library name"
            )

        library = Path(root) / library_name
    if not library.is_absolute() or not library.is_file():
        raise RuntimeError(f"selected Python embedding library is missing: {library}")

    try:
        loaded = ctypes.CDLL(str(library))
    except OSError as error:
        raise RuntimeError(
            f"selected Python embedding library is unusable: {error}"
        ) from error
    # Standalone Python distributions can ship a static executable alongside
    # libpython, so matching function addresses would reject valid installs.
    # Compare the full version/build/compiler string (including free-threading)
    # and the trace-refs ABI against the running child's C API instead.
    loaded.Py_GetVersion.restype = ctypes.c_char_p
    ctypes.pythonapi.Py_GetVersion.restype = ctypes.c_char_p
    if loaded.Py_GetVersion() != ctypes.pythonapi.Py_GetVersion() or hasattr(
        loaded, "PyModule_Create2TraceRefs"
    ) != hasattr(ctypes.pythonapi, "PyModule_Create2TraceRefs"):
        raise RuntimeError(
            "selected Python embedding library does not match the running interpreter"
        )
    # Match the CPython entrypoints loaded by library.rs. This check runs in
    # the child, before the caller can commit any process-lifetime runtime.
    for symbol in (
        "Py_IsInitialized",
        "Py_SetProgramName",
        "Py_InitializeEx",
        "PySys_SetArgvEx",
        "PyOS_setsig",
        "PyEval_SaveThread",
        "PyEval_RestoreThread",
        "PyGILState_Ensure",
        "PyGILState_Release",
        "PyImport_AddModule",
        "PyModule_GetDict",
        "PyDict_New",
        "PyDict_GetItemString",
        "PyDict_SetItemString",
        "PyRun_StringFlags",
        "PyObject_CallNoArgs",
        "PyObject_CallFunctionObjArgs",
        "PyUnicode_FromStringAndSize",
        "PyLong_AsLong",
        "Py_DecRef",
        "PyErr_Fetch",
        "PyErr_NormalizeException",
        "PyErr_Display",
        "PyErr_Clear",
        "PyErr_Print",
        "PyErr_ExceptionMatches",
        "PyExc_SystemExit",
        "PyException_SetTraceback",
    ):
        try:
            getattr(loaded, symbol)
        except AttributeError as error:
            raise RuntimeError(
                f"selected Python embedding library is missing {symbol}: {library}"
            ) from error

    # This installation snapshot stays isolated. Once Python is configured,
    # the R bridge gets path and module metadata from the running interpreter.
    numpy = None
    try:
        distribution = importlib.metadata.distribution("numpy")
        version = distribution.version
        # Reticulate parses the numeric prefix after removing a version suffix.
        if isinstance(version, str) and re.fullmatch(
            r"[0-9]+(?:\.[0-9]+)*(?:\.?[A-Za-z_+].*)?", version
        ):
            numpy = {
                "path": str(distribution.locate_file("numpy")),
                "version": version,
            }
    except Exception:
        # Missing or unusable optional metadata does not invalidate CPython.
        pass

    return {
        "metadata": {
            "base_executable": sys._base_executable,
            "pythonpath": os.pathsep.join(sys.path),
            "version": sys.version.replace("\n", " "),
            "version_number": f"{sys.version_info.major}.{sys.version_info.minor}",
            "architecture": f"{struct.calcsize('P') * 8}bit",
            "conda": (Path(sys.prefix) / "conda-meta").is_dir(),
            "numpy": numpy,
        },
        "executable": sys.executable,
        "libpython": str(library),
        "prefix": sys.prefix,
        "exec_prefix": sys.exec_prefix,
        "base_prefix": sys.base_prefix,
        "base_exec_prefix": sys.base_exec_prefix,
    }


with open(sys.argv[1], "w", encoding="utf-8") as result:
    json.dump(describe(), result)
