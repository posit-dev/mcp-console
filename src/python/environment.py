"""Live path activation; manifests and resolution belong to the native owner."""

# Activation errors belong to the private runtime module, not user builtins.
import importlib
import importlib.metadata
import json
import os
import re
import sys

import _mcp_console as runtime
import _mcp_console_services as services

_active = None
_site_paths = set()


class _Commit:
    def __enter__(self) -> None:
        services.begin_python_commit()

    def __exit__(self, *exception: object) -> None:
        services.finish_python_commit()


class _ActivationFailure(Exception):
    """Preserve the restart contract for failures after site mutation begins."""


class _RollbackFailure(BaseException):
    """Escape the language-error result when the live state cannot be restored."""


def initialize(request: str) -> str:
    global _active, _site_paths
    configuration = json.loads(request)
    # Inspect the already running selection, without reentering reticulate's
    # initializer or executing site.main() again in the live interpreter.
    inspected = json.loads(services.inspect_python(sys.executable))
    _active = dict(
        inspected,
        executable=sys.executable,
        prefix=sys.prefix,
        exec_prefix=sys.exec_prefix,
        pythonpath=os.pathsep.join(path or "." for path in sys.path),
        libpython=os.path.realpath(configuration["libpython"]),
        version=sys.version.split()[0],
    )
    # Capture embedded prefix-local entries and reproducible .pth additions
    # before cells add paths. Startup hooks that add different paths in the
    # probe and embedded process are outside the path-cleanup contract.
    _site_paths = {
        path
        for path in sys.path
        if path in inspected["site_paths"]
        or any(
            path == prefix or path.startswith(prefix + os.sep)
            for prefix in (sys.prefix, sys.exec_prefix)
        )
    }
    return json.dumps(_active)


def _name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _check_compatible(candidate: dict) -> None:
    if (candidate["libpython"], candidate["version"]) != (
        _active["libpython"],
        _active["version"],
    ):
        raise RuntimeError(
            "New environment does not use the same Python binary; restart with the requested Python version"
        )
    versions = {
        _name(name): version for name, version in candidate["distributions"].items()
    }
    loaded = {
        os.path.realpath(path)
        for module in tuple(sys.modules.values())
        if (path := getattr(module, "__file__", None)) is not None
    }
    for distribution in importlib.metadata.distributions(path=_site_paths):
        name, version = distribution.metadata["Name"], distribution.version
        if name is not None and versions.get(_name(name)) == version:
            continue
        # A namespace alone does not load all distributions contributing to it.
        is_loaded = any(
            os.path.realpath(distribution.locate_file(path)) in loaded
            for path in distribution.files or ()
        )
        if is_loaded and (selected := versions.get(_name(name))) != version:
            raise RuntimeError(
                f"Cannot replace loaded {name} {version} with {selected or 'an absent distribution'}. "
                "Restart with compatible requirements; the running interpreter and objects are unchanged."
            )


def _activate(candidate: dict, manifest: dict) -> None:
    global _active, _site_paths
    _check_compatible(candidate)
    paths = list(sys.path)
    identity = {
        name: getattr(sys, name, None)
        for name in ("prefix", "exec_prefix", "executable", "real_prefix")
    }
    environment = {
        name: os.environ.get(name)
        for name in ("PATH", "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT")
    }
    committed = False
    try:
        sys.prefix, sys.exec_prefix = candidate["prefix"], candidate["exec_prefix"]
        sys.executable = candidate["executable"]
        old_bin = os.path.dirname(identity["executable"])
        bins = os.environ.get("PATH", "").split(os.pathsep)
        os.environ["PATH"] = os.pathsep.join(path for path in bins if path != old_bin)
        previous = (
            _site_paths
            if candidate["site_packages"] != _active["site_packages"]
            else set()
        )
        sys.path[:] = [path for path in sys.path if path not in previous]
        retained = set(sys.path)
        # The managed environment supplies its activation hook. Set identity
        # first so its .pth hooks observe the candidate, then track its additions.
        services.activate_python_environment()
        sys.real_prefix = identity["prefix"]
        importlib.invalidate_caches()
        added = (_site_paths - previous) | (set(sys.path) - retained)
        candidate["pythonpath"] = os.pathsep.join(path or "." for path in sys.path)
        # The native SIGINT callback consults this deferral and leaves R's
        # pending bit intact. Site processing above stays interruptible.
        with _Commit():
            services.publish_python_activation(
                json.dumps({"environment": candidate, "manifest": manifest})
            )
            _active, _site_paths, committed = candidate, added, True
    except Exception as error:
        raise _ActivationFailure() from error
    finally:
        # Roll back state owned here, not arbitrary side effects of site hooks.
        with _Commit():
            if not committed:
                try:
                    sys.path[:] = paths
                    for name, value in identity.items():
                        if value is None:
                            sys.__dict__.pop(name, None)
                        else:
                            setattr(sys, name, value)
                    runtime.activate_process_environment(identity["executable"])
                    for name, value in environment.items():
                        if value is None:
                            os.environ.pop(name, None)
                        else:
                            os.environ[name] = value
                    importlib.invalidate_caches()
                except BaseException as error:
                    raise _RollbackFailure(
                        "Python activation rollback failed"
                    ) from error


def prepare(request: str) -> str:
    try:
        request = json.loads(request)
        runtime.without_automatic_resolution(
            _activate, request["environment"], request["manifest"]
        )
        return json.dumps({"kind": "prepared"})
    except _ActivationFailure as failure:
        runtime.setup_error = failure.__cause__
        return json.dumps(
            {"kind": "failed", "message": "Python activation failed; restart required"}
        )
    except Exception as error:
        return json.dumps(
            {"kind": "rejected", "message": str(error) or type(error).__name__}
        )
