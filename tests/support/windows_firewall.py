"""Read-only evidence that native acceptance needs no firewall authorization."""

from pathlib import Path


def require_unelevated():
    import ctypes
    import os
    import unittest
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    security.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    security.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(
        kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)
    ):
        raise ctypes.WinError()
    try:
        elevated, size = wintypes.DWORD(), wintypes.DWORD()
        if not security.GetTokenInformation(
            token,
            20,
            ctypes.byref(elevated),
            ctypes.sizeof(elevated),
            ctypes.byref(size),
        ):
            raise ctypes.WinError()
        if elevated.value:
            reason = "no-admin validation requires an unelevated Windows process"
            if os.environ.get("MCP_CONSOLE_TEST_REQUIRE_MXC") == "1":
                raise AssertionError(reason)
            raise unittest.SkipTest(reason)
    finally:
        kernel.CloseHandle(token)


def application_rules(executables: list[Path]) -> dict[str, str]:
    import winreg

    paths = {str(path.resolve()).casefold() for path in executables}
    result = {}
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules",
    ) as key:
        for index in range(winreg.QueryInfoKey(key)[1]):
            name, value, _ = winreg.EnumValue(key, index)
            if any(
                field.partition("=")[2].casefold() in paths
                for field in value.split("|")
                if field.partition("=")[0].casefold() == "app"
            ):
                result[name] = value
    return result
