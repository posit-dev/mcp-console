import importlib

for name in ("yaml12", "rpython", "xml.mcp_console_missing"):
    for load in (
        importlib.import_module,
        __import__,
        lambda name: exec(f"import {name}"),
    ):
        try:
            load(name)
        except ModuleNotFoundError as error:
            assert type(error) is ModuleNotFoundError
            assert error.args == (f"No module named '{name}'",), error.args
            assert error.name == name
            assert error.path is None
        else:
            raise AssertionError(f"undeclared import succeeded: {name}")

try:
    from xml import mcp_console_missing
except ImportError as error:
    assert type(error) is ImportError
    assert str(error).startswith("cannot import name 'mcp_console_missing' from 'xml'")
    assert error.name == "xml"
else:
    raise AssertionError("missing from-import succeeded")

print("native import errors")
