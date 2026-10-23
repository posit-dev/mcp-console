"""Inspect one selected environment; this program never chooses an interpreter."""

import ctypes
import importlib.metadata
import json
import os
import site
import sys
import sysconfig

# -S gives site processing an explicit path baseline in this disposable child.
# In particular, executable .pth lines can add paths outside the environment.
before = set(sys.path)
site.main()
directories = [path for path in site.getsitepackages() if os.path.isdir(path)]
owned = (set(sys.path) - before) | set(directories)
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
    library = os.path.realpath(buffer.value)
else:
    directory = sysconfig.get_config_var(
        "PYTHONFRAMEWORKPREFIX"
        if sysconfig.get_config_var("PYTHONFRAMEWORK")
        else "LIBDIR"
    )
    library = os.path.realpath(
        os.path.join(directory, sysconfig.get_config_var("INSTSONAME"))
    )
# Match reticulate's optional NumPy metadata without importing it in the worker.
numpy = None
try:
    import numpy as np
except Exception:
    pass
else:
    numpy = {"path": os.path.realpath(np.__path__[0]), "version": np.__version__}
print(
    "\x1eMCP_CONSOLE_ENVIRONMENT\x1e"
    + json.dumps(
        {
            "executable": sys.executable,
            "libpython": library,
            "version": sys.version.split()[0],
            "prefix": sys.prefix,
            "exec_prefix": sys.exec_prefix,
            "base_executable": sys._base_executable,
            "pythonpath": os.pathsep.join(path or "." for path in sys.path),
            "site_packages": directories,
            "site_paths": sorted(owned),
            "numpy": numpy,
            "distributions": {
                d.metadata["Name"]: d.version
                for d in importlib.metadata.distributions()
            },
        }
    )
    + "\x1f",
    flush=True,
)
