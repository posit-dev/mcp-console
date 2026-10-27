# MCP Console's private Python runtime.
import __main__ as _main
import ast as _ast
import base64 as _base64
import builtins as _builtins
import importlib as _importlib
import importlib.metadata as _importlib_metadata
import importlib.util as _importlib_util
import io as _io
import json as _json
import os as _os
import re as _re
import sys as _sys
import threading as _threading
import traceback as _traceback
import types as _types
import _mcp_console_services as _services


_mcp_console_private_codes = set()


_MCP_CONSOLE_IMPORT_DISTRIBUTIONS = {
    "PIL": "pillow",
    "OpenSSL": "pyopenssl",
    "Crypto": "pycryptodome",
    "_cffi_backend": "cffi",
    "attr": "attrs",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "docx": "python-docx",
    "dotenv": "python-dotenv",
    "jwt": "pyjwt",
    "pptx": "python-pptx",
    "serial": "pyserial",
    "skimage": "scikit-image",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
    "yaml12": "py-yaml12",
}

# Shared namespaces and alternate-interpreter toolchains need an explicit
# distribution. In particular, CPython libraries can probe rpython with an
# ImportError fallback; installing the Python 2 toolchain breaks that fallback.
_MCP_CONSOLE_AMBIGUOUS_IMPORT_ROOTS = {
    "azure",
    "backports",
    "google",
    "opentelemetry",
    "rpython",
    "zope",
}


def _mcp_console_missing_module(fullname, message):
    raise ModuleNotFoundError(message, name=fullname) from None


def _mcp_console_missing_submodule(fullname, message):
    raise ImportError(message, name=fullname) from None


def _mcp_console_explicit_requirement(distribution):
    return f'requirements: {{"python": ["{distribution}"]}}'


class _McpConsoleModuleLoader:
    def __init__(self, loader, callback):
        self._loader = loader
        self._callback = callback

    def __getattr__(self, name):
        return getattr(self._loader, name)

    def create_module(self, specification):
        create = getattr(self._loader, "create_module", None)
        return None if create is None else create(specification)

    def exec_module(self, module):
        module.__loader__ = self._loader
        module.__spec__.loader = self._loader
        self._loader.exec_module(module)
        self._callback()
        return None


class _McpConsoleImportFinder:
    _mcp_console_import_finder = True

    def __init__(
        self,
        importlib,
        importlib_util,
        json,
        os,
        sys,
        threading,
        distributions,
        ambiguous_roots,
        metadata,
        private_codes,
        missing_module,
        missing_submodule,
        explicit_requirement,
        psutil_loader,
    ):
        self._importlib = importlib
        self._importlib_util = importlib_util
        self._json = json
        self._os = os
        self._sys = sys
        self._threading = threading
        self._distributions = distributions
        self._ambiguous_roots = ambiguous_roots
        self._metadata = metadata
        self._csv_reader = importlib.import_module("csv").reader
        self._private_codes = private_codes
        self._id = id
        self._import_globals = (
            importlib.__dict__,
            importlib_util.__dict__,
            importlib._bootstrap.__dict__,
            importlib._bootstrap_external.__dict__,
        )
        self._missing_module = missing_module
        self._missing_submodule = missing_submodule
        self._explicit_requirement = explicit_requirement
        self._psutil_loader = psutil_loader
        self._psutil_callback = None
        self._fromlist_code = importlib._bootstrap._handle_fromlist.__code__
        self._load_codes = (
            importlib._bootstrap._load_unlocked.__code__,
            importlib._bootstrap._exec.__code__,
        )
        self._callback = None
        self._pid = None
        self._thread = None
        self._state = threading.local()

    def configure(self, callback):
        self._callback = callback
        self._pid = self._os.getpid()
        self._thread = self._threading.get_ident()

        # Only implicit policy supplies a callback; other policies leave native
        # import lookup and errors untouched. Keep registration idempotent when
        # setup resumes after interruption.
        if callback is not None and self not in self._sys.meta_path:
            self._sys.meta_path.append(self)
        return None

    def configure_psutil(self, callback):
        self._psutil_callback = callback
        return None

    def _find_spec(self, finders, fullname, path, target):
        for finder in tuple(finders):
            if finder is self:
                continue
            if hasattr(finder, "find_spec"):
                specification = finder.find_spec(fullname, path, target)
            elif self._sys.version_info < (3, 12):
                find_module = getattr(finder, "find_module", None)
                loader = None if find_module is None else find_module(fullname, path)
                specification = (
                    None
                    if loader is None
                    else self._importlib_util.spec_from_loader(fullname, loader)
                )
            else:
                # Python 3.12 and later ignore legacy-only meta-path finders.
                specification = None
            if specification is not None:
                return specification
        return None

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.partition(".")[0]
        if getattr(self._state, "resolving", False):
            return None
        # Availability probes must observe the environment without changing it.
        # A real import can resolve the distribution if the caller proceeds.
        if self._is_availability_probe():
            return None
        # Installed distributions own their dependencies. Their import-time
        # optional probes must observe absence rather than install a package.
        # Local modules and calls made after initialization may still resolve.
        if self._is_installed_package_initialization():
            return None
        # Some libraries append importers after Python initializes. Give only
        # that later suffix its ordinary chance before acting as the last finder.
        finders = self._sys.meta_path
        position = finders.index(self)
        specification = self._find_spec(
            finders[position + 1 :],
            fullname,
            path,
            target,
        )
        if specification is not None:
            return specification
        if self._pid != self._os.getpid():
            self._missing_module(
                fullname,
                f"No module named {fullname!r}.\n\n"
                "MCP Console automatic package resolution is available only in the "
                "main worker process. Prepare the distribution through "
                "`requirements.python` in the parent before starting the child, for "
                "example:\n\n" + self._explicit_requirement("distribution-name"),
            )
        if self._thread != self._threading.get_ident():
            self._missing_module(
                fullname,
                f"No module named {fullname!r}.\n\n"
                "MCP Console automatic package resolution is available only on the "
                "configuring thread for the Python worker. Prepare the distribution "
                "through `requirements.python` before starting the background thread, "
                "for example:\n\n" + self._explicit_requirement("distribution-name"),
            )
        if fullname != root and root in self._sys.modules:
            missing = (
                self._missing_submodule
                if self._is_fromlist_import(fullname)
                else self._missing_module
            )
            missing(
                fullname,
                f"No module named {fullname!r}.\n\n"
                f"MCP Console did not resolve the top-level import {root!r} again "
                "because it is already present. The missing submodule may require an "
                "optional extra or a different distribution. Pass the correct "
                "distribution through `requirements.python` in the `send` call, for "
                "example:\n\n" + self._explicit_requirement("distribution-name"),
            )
        if root in self._sys.stdlib_module_names:
            self._missing_module(
                fullname,
                f"No module named {fullname!r}.\n\n"
                f"{root!r} is a Python standard-library module, but it is unavailable "
                "in the selected Python build. MCP Console did not try to install a "
                "same-named PyPI distribution.",
            )
        if root in self._ambiguous_roots:
            self._raise_unsafe_inference(fullname, root)

        distribution = self._distributions.get(root)
        if distribution is None:
            safe = (
                root.isascii()
                and root.isidentifier()
                and root[0].isalnum()
                and root[-1].isalnum()
            )
            if not safe:
                self._raise_unsafe_inference(fullname, root)
            distribution = root

        self._state.resolving = True
        try:
            try:
                response = self._json.loads(self._callback(root, distribution))
            except Exception as error:
                self._raise_resolution_failure(fullname, root, distribution, str(error))

            kind = response.get("kind") if isinstance(response, dict) else None
            if kind == "failed" and isinstance(response.get("message"), str):
                self._raise_resolution_failure(
                    fullname,
                    root,
                    distribution,
                    response["message"],
                )
            if kind == "disabled" and isinstance(response.get("message"), str):
                self._missing_module(
                    fullname,
                    f"No module named {fullname!r}.\n\n{response['message']}",
                )
            if kind != "ready":
                raise RuntimeError("invalid automatic Python package resolver response")

            self._importlib.invalidate_caches()
            specification = self._find_spec(
                self._sys.meta_path,
                fullname,
                path,
                target,
            )
            if specification is not None:
                # A transient activation probe can fail before psutil loads.
                # Retry after module execution, before the import returns.
                if (
                    fullname == "psutil"
                    and self._psutil_callback is not None
                    and hasattr(specification.loader, "exec_module")
                ):
                    specification.loader = self._psutil_loader(
                        specification.loader,
                        self._psutil_callback,
                    )
                return specification
        finally:
            self._state.resolving = False

        self._missing_module(
            fullname,
            f"No module named {fullname!r}.\n\n"
            f"MCP Console prepared the inferred PyPI distribution `{distribution}`, "
            f"but it did not provide the import `{fullname}`.\n\n"
            "Pass the correct distribution through `requirements.python` in the "
            "`send` call:\n\n"
            + self._explicit_requirement("correct-distribution-name"),
        )

    def _is_installed_package_initialization(self):
        # Importlib holds the spec during loading, PyInit, and reload.
        # Deferred execution after this boundary remains eligible.
        frame = self._sys._getframe(1)
        importing_frame = None
        while frame is not None:
            if any(frame.f_code is code for code in self._load_codes):
                specification = frame.f_locals["spec"]
                break
            # The current import entrypoint may wrap importlib (as reticulate
            # does). Frames inside that entrypoint are import machinery.
            if frame.f_code is self._importlib._bootstrap._find_and_load.__code__:
                importing_frame = None
            elif (
                importing_frame is None
                and self._id(frame.f_code) not in self._private_codes
                and all(frame.f_globals is not scope for scope in self._import_globals)
            ):
                importing_frame = frame
            frame = frame.f_back
        else:
            return False
        # Cached helpers share initialization context; local callbacks own theirs.
        # Native callbacks expose no frame and inherit the visible initializer.
        importer = (
            specification
            if importing_frame is None
            else importing_frame.f_globals.get("__spec__")
        )
        if importer is None or importer.origin is None or specification.origin is None:
            return False
        root = specification.name.partition(".")[0]
        origins = {
            self._os.path.realpath(specification.origin),
            self._os.path.realpath(importer.origin),
        }
        module = self._sys.modules.get(root)
        root_spec = specification if module is None else module.__spec__
        locations = (
            root_spec.submodule_search_locations
            if module is None
            else getattr(module, "__path__", None)
        )
        # Use actual loaded paths retained by activation or extend_path.
        # Package paths may differ from the root spec. Do not cache metadata.
        metadata_paths = (
            [self._os.path.dirname(path) for path in locations]
            if locations is not None
            else [self._os.path.dirname(root_spec.origin)]
        )
        for distribution in self._metadata.distributions(path=metadata_paths):
            # Both selected files must belong to the same distribution. Names,
            # file-less metadata and indirect editable records cannot own them.
            # Managed installations use wheel RECORDs. Distribution.files also
            # stats every payload; read the record without traversing those files.
            record = distribution.read_text("RECORD")
            if record is None:
                continue
            # Canonicalize selected files across symlinked installation paths.
            location = self._os.path.realpath(str(distribution.locate_file("")))
            selected = {self._os.path.relpath(origin, location) for origin in origins}
            rows = self._csv_reader(record.splitlines(keepends=True), strict=True)
            # Resolve only matching RECORD entries, not unrelated payload paths.
            installed = {
                self._os.path.realpath(str(distribution.locate_file(row[0])))
                for row in rows
                if self._os.path.normpath(row[0]) in selected
            }
            if origins <= installed:
                return True
        return False

    def _is_availability_probe(self):
        probe_code = getattr(self._importlib_util.find_spec, "__code__", None)
        frame = self._sys._getframe(1)
        while frame is not None:
            if frame.f_code is probe_code:
                return True
            frame = frame.f_back
        return False

    def _is_fromlist_import(self, fullname):
        frame = self._sys._getframe(1)
        while frame is not None:
            if (
                frame.f_code is self._fromlist_code
                and frame.f_locals.get("from_name") == fullname
            ):
                return True
            frame = frame.f_back
        return False

    def _raise_unsafe_inference(self, fullname, root):
        self._missing_module(
            fullname,
            f"No module named {fullname!r}.\n\n"
            "MCP Console could not safely infer a PyPI distribution for the missing "
            f"import `{root}`.\n\n"
            "Pass the distribution through `requirements.python` in the `send` call, "
            "for example:\n\n" + self._explicit_requirement("distribution-name"),
        )

    def _raise_resolution_failure(self, fullname, root, distribution, diagnostic):
        self._missing_module(
            fullname,
            f"No module named {fullname!r}.\n\n"
            f"MCP Console inferred the PyPI distribution `{distribution}` from the "
            f"import `{root}`, but automatic package resolution failed:\n\n"
            f"{diagnostic}\n\n"
            "Import names and PyPI distribution names can differ. Retry with the "
            "correct distribution declared through `requirements.python` in the "
            "`send` call, for example:\n\n" + self._explicit_requirement(distribution),
        )


_mcp_console_import_finder = None
for _mcp_console_finder in _sys.meta_path:
    if _builtins.getattr(_mcp_console_finder, "_mcp_console_import_finder", False):
        _mcp_console_import_finder = _mcp_console_finder
        break
if _mcp_console_import_finder is None:
    _mcp_console_import_finder = _McpConsoleImportFinder(
        _importlib,
        _importlib_util,
        _json,
        _os,
        _sys,
        _threading,
        _MCP_CONSOLE_IMPORT_DISTRIBUTIONS,
        _MCP_CONSOLE_AMBIGUOUS_IMPORT_ROOTS,
        _importlib_metadata,
        _mcp_console_private_codes,
        _mcp_console_missing_module,
        _mcp_console_missing_submodule,
        _mcp_console_explicit_requirement,
        _McpConsoleModuleLoader,
    )
    # Display defaults and SQL suppression reuse this object's helpers. Only
    # implicit resolution registers it as an import finder, during configure().


def _mcp_console_print_exception(
    error,
    source_error=False,
    _traceback=_traceback,
    _sys=_sys,
    _private_codes=_mcp_console_private_codes,
    _services_globals=_services.__dict__,
    _getattr=_builtins.getattr,
    _id=_builtins.id,
    _zip=_builtins.zip,
    _record_error=_services.report_execution_error,
):
    # Build every frame first so filtering retains Python's source positions.
    rendered = _traceback.TracebackException.from_exception(error, limit=_sys.maxsize)

    def remove_private_frames(exception, summary):
        if exception is None or summary is None:
            return
        if source_error and exception is error:
            summary.stack = _traceback.StackSummary()
        else:
            frames = []
            traceback = exception.__traceback__
            for position in summary.stack:
                assert traceback is not None
                frame = traceback.tb_frame
                if (
                    _id(frame.f_code) not in _private_codes
                    and frame.f_globals is not _services_globals
                ):
                    frames.append(position)
                traceback = traceback.tb_next
            assert traceback is None
            limit = _getattr(_sys, "tracebacklimit", None)
            if limit is not None:
                frames = frames[:limit] if limit >= 0 else frames[limit:]
            summary.stack = _traceback.StackSummary(frames)
        remove_private_frames(exception.__cause__, summary.__cause__)
        remove_private_frames(exception.__context__, summary.__context__)
        for child, child_summary in _zip(
            _getattr(exception, "exceptions", ()),
            _getattr(summary, "exceptions", ()) or (),
        ):
            remove_private_frames(child, child_summary)

    remove_private_frames(error, rendered)
    traceback = list(rendered.format())
    _record_error(error, traceback)
    _sys.stderr.write("".join(traceback))


def _mcp_console_collect_plots(
    close=True,
    propagate_interrupt=False,
    _BaseException=_builtins.BaseException,
    _KeyboardInterrupt=_builtins.KeyboardInterrupt,
    _isinstance=_builtins.isinstance,
    _base64=_base64,
    _io=_io,
    _print_exception=_mcp_console_print_exception,
    _sorted=_builtins.sorted,
    _sys=_sys,
):
    pyplot = _sys.modules.get("matplotlib.pyplot")
    if pyplot is None:
        return ()

    images = []
    active_number = pyplot.gcf().number if not close and pyplot.get_fignums() else None
    try:
        for number in _sorted(pyplot.get_fignums()):
            if number not in pyplot.get_fignums():
                continue
            try:
                figure = pyplot.figure(number)
                output = _io.BytesIO()
                figure.savefig(output, format="png")
                images.append(_base64.b64encode(output.getvalue()).decode("ascii"))
            except _BaseException as error:
                if propagate_interrupt and _isinstance(error, _KeyboardInterrupt):
                    raise
                _print_exception(error)
    finally:
        if close:
            try:
                pyplot.close("all")
            except _BaseException as error:
                if propagate_interrupt and _isinstance(error, _KeyboardInterrupt):
                    raise
                _print_exception(error)
        elif active_number in pyplot.get_fignums():
            pyplot.figure(active_number)
    return tuple(images)


def _mcp_console_finalize_plots(
    close=True,
    propagate_interrupt=False,
    _collect_plots=_mcp_console_collect_plots,
    _publish_plot=_services.publish_plot,
    _BaseException=_builtins.BaseException,
    _KeyboardInterrupt=_builtins.KeyboardInterrupt,
    _isinstance=_builtins.isinstance,
    _print_exception=_mcp_console_print_exception,
):
    try:
        for image in _collect_plots(close, propagate_interrupt):
            _publish_plot(image)
    except _BaseException as error:
        if propagate_interrupt and _isinstance(error, _KeyboardInterrupt):
            raise
        _print_exception(error)
    return None


def _mcp_console_install_matplotlib_show(
    _finalize_plots=_mcp_console_finalize_plots,
    _setattr=_builtins.setattr,
    _getattr=_builtins.getattr,
    _sys=_sys,
):
    pyplot = _sys.modules.get("matplotlib.pyplot")
    if pyplot is not None:
        pause_code = _getattr(_getattr(pyplot, "pause", None), "__code__", None)

        def show(*args, **kwargs):
            # Native callers may have no Python frame; only pause() keeps figures open.
            caller = _sys._getframe().f_back
            return _finalize_plots(
                close=caller is None or caller.f_code is not pause_code,
                propagate_interrupt=True,
            )

        _setattr(pyplot, "show", show)
    return None


def _mcp_console_eval_cell(
    source,
    filename,
    _main=_main,
    _parse=_ast.parse,
    _Expr=_ast.Expr,
    _Expression=_ast.Expression,
    _isinstance=_builtins.isinstance,
    _compile=_builtins.compile,
    _exec=_builtins.exec,
    _eval=_builtins.eval,
    _BaseException=_builtins.BaseException,
    _SystemExit=_builtins.SystemExit,
    _finalize_plots=_mcp_console_finalize_plots,
    _sys=_sys,
    _print_exception=_mcp_console_print_exception,
    _ValueError=_builtins.ValueError,
    _SyntaxError=_builtins.SyntaxError,
    _display_result=_services.display_result,
):
    try:
        module = _parse(source, filename=filename, mode="exec")
        final = module.body[-1] if module.body else None
        if _isinstance(final, _Expr):
            module.body.pop()
            statements = _compile(module, filename, "exec") if module.body else None
            expression = _compile(_Expression(final.value), filename, "eval")
        else:
            statements = _compile(module, filename, "exec")
            expression = None
    except _BaseException as error:
        if _isinstance(error, _SystemExit):
            raise
        if "\0" in source and _isinstance(error, _ValueError):
            error = _SyntaxError("source code string cannot contain null bytes")
        _print_exception(error, source_error=_isinstance(error, _SyntaxError))
    else:
        try:
            if statements is not None:
                _exec(statements, _main.__dict__)
            if expression is not None:
                _display_result(_eval(expression, _main.__dict__))
        except _BaseException as error:
            if _isinstance(error, _SystemExit):
                raise
            _print_exception(error)
    _finalize_plots()
    return None


def _mcp_console_apply_psutil_process_group(
    _callable=_builtins.callable,
    _getattr=_builtins.getattr,
    _import_module=_importlib.import_module,
    _find_spec=_importlib_util.find_spec,
    _os=_os,
    _sys=_sys,
):
    psutil = _sys.modules.get("psutil")
    specification = (
        _find_spec("psutil") if psutil is None else _getattr(psutil, "__spec__", None)
    )
    if specification is None or specification.origin is None:
        return None
    metadata = _import_module("importlib.metadata")
    try:
        distribution = metadata.distribution("psutil")
    except metadata.PackageNotFoundError:
        return None
    resolved = _os.path.realpath(specification.origin)
    installed = _os.path.realpath(
        _os.fspath(distribution.locate_file("psutil/__init__.py"))
    )
    if resolved != installed:
        return None
    if psutil is None:
        psutil = _import_module("psutil")
    platform = _getattr(psutil, "_psplatform", None)
    pids = _getattr(platform, "pids", None)
    if not _callable(pids) or _getattr(pids, "_mcp_console_sandbox", False):
        return None

    ctypes = _import_module("ctypes")
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    list_process_group = libproc.proc_listpgrppids
    list_process_group.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    list_process_group.restype = ctypes.c_int
    process_group = _os.getpgrp()

    def process_group_ids():
        capacity = 16
        while True:
            buffer = (ctypes.c_int * capacity)()
            ctypes.set_errno(0)
            count = list_process_group(
                process_group,
                buffer,
                ctypes.sizeof(buffer),
            )
            if count <= 0:
                error = ctypes.get_errno()
                if error:
                    raise OSError(error, _os.strerror(error))
                if count < 0:
                    raise RuntimeError("process-group enumeration failed")
                return []
            if count < capacity:
                return buffer[:count]
            capacity *= 2

    process_group_ids._mcp_console_sandbox = True
    # Keep psutil's public wrapper so it retains its sorted-list contract. The
    # platform hook also survives activation during the first psutil import.
    platform.pids = process_group_ids
    return None


def _mcp_console_configure_psutil(
    _Exception=_builtins.Exception,
    _apply=_mcp_console_apply_psutil_process_group,
    _sandboxed=_os.environ.get("MCP_CONSOLE_SANDBOX") == "1",
):
    if not _sandboxed:
        return None
    # This adapter may run after reticulate has activated an environment, so
    # its optional probe and setup must not abort activation.
    try:
        _apply()
    except _Exception:
        pass
    return None


_mcp_console_import_finder.configure_psutil(_mcp_console_configure_psutil)


def _mcp_console_activate_process_environment(
    executable,
    _configure_psutil=_mcp_console_configure_psutil,
    _sys=_sys,
):
    _sys.executable = executable
    multiprocessing = _sys.modules.get("multiprocessing")
    if multiprocessing is not None:
        multiprocessing.set_executable(executable)
    _sys.modules["_mcp_console"]._configure_backends(executable, _configure_psutil)
    return None


_mcp_console = _sys.modules.get("_mcp_console") or _types.ModuleType("_mcp_console")
# Native setup may retain a site-hook error before evaluator installation.
# Keep the following embedded-source lines stable for public SQL tracebacks.


def _mcp_console_without_automatic_resolution(
    operation,
    *arguments,
    _finder=_mcp_console_import_finder,
):
    resolving = getattr(_finder._state, "resolving", False)
    try:
        _finder._state.resolving = True
        return operation(*arguments)
    finally:
        _finder._state.resolving = resolving


_mcp_console.activate_process_environment = _mcp_console_activate_process_environment
_mcp_console.install_matplotlib_show = _mcp_console_install_matplotlib_show
_mcp_console.configure_import_resolution = _mcp_console_import_finder.configure
_mcp_console.without_automatic_resolution = _mcp_console_without_automatic_resolution
_mcp_console.eval_cell = _mcp_console_eval_cell
_mcp_console.finalize_plots = _mcp_console_finalize_plots
_sys.modules[_mcp_console.__name__] = _mcp_console
# Native startup calls this module directly instead of using a dispatcher.
_mcp_console_configure_psutil()


def _mcp_console_raise_setup_error(_state=_mcp_console.__dict__):
    raise _state.pop("setup_error")


_mcp_console.raise_setup_error = _mcp_console_raise_setup_error


def _mcp_console_activate_environment(
    script,
    executable,
    _configure_process=_mcp_console_activate_process_environment,
):
    import runpy

    runpy.run_path(script)
    _configure_process(executable)
    return None


_mcp_console.activate_environment = _mcp_console_activate_environment


# The host inspection and embedded interpreter must agree on the complete
# environment, independently of whether a bridge attaches later.
def _mcp_console_configure_environment(
    configuration: str,
    _json=_json,
    _sys=_sys,
    _os=_os,
    _configure_process=_mcp_console_activate_process_environment,
) -> None:
    expected = _json.loads(configuration)
    for name in ("prefix", "exec_prefix", "base_prefix", "base_exec_prefix"):
        # Framework launchers and embedding can retain different spellings of
        # the same directory (for example /var and /private/var on macOS).
        try:
            matches = _os.path.samefile(getattr(_sys, name), expected[name])
        except OSError:
            matches = False
        if not matches:
            raise RuntimeError(
                f"embedded Python {name} differs from the selected environment: "
                f"{getattr(_sys, name)!r} != {expected[name]!r}"
            )
    executable = expected["embedding"]["python"]
    try:
        matches = _os.path.samefile(_sys.executable, executable)
    except OSError:
        matches = False
    if not matches:
        raise RuntimeError("embedded Python executable differs from host selection")
    # Match an interactive interpreter: imports follow the current workspace,
    # including a later os.chdir(), rather than the selected executable's bin.
    _sys.path.insert(0, "")
    _configure_process(executable)


_mcp_console.configure_environment = _mcp_console_configure_environment


# Setup and activation retain exceptions until their Rust caller reports them.
# The wrapper below routes them through Console's ordered diagnostic stream.
def _mcp_console_display_setup_exception(
    _state=_mcp_console.__dict__,
    _traceback=_traceback,
    _stderr=_sys.__stderr__,
) -> None:
    error = _state.pop("setup_error", None)
    if error is not None:
        _traceback.print_exception(
            type(error), error, error.__traceback__, file=_stderr
        )
        _stderr.flush()


_mcp_console.display_setup_exception = _mcp_console_display_setup_exception


def _mcp_console_display_activation_exception(
    _display=_mcp_console_display_setup_exception,
    _stderr=_sys.stderr,
) -> None:
    # After Ready, diagnostics must precede the preparation result on the
    # sideband. Capture the installed console stream, independent of fd 2.
    _display(_stderr=_stderr)


_mcp_console.display_activation_exception = _mcp_console_display_activation_exception


def _mcp_console_configure_native_child_environment(
    configuration: str,
    _json=_json,
    _os=_os,
    _sys=_sys,
) -> None:
    expected = _json.loads(configuration)
    # activate_this.py updates prefix, but does not update exec_prefix. Keep
    # the complete inspected virtualenv identity in sync with child Python.
    for name in ("prefix", "exec_prefix", "base_prefix", "base_exec_prefix"):
        setattr(_sys, name, expected[name])
    executable = expected["embedding"]["python"]
    directory = _os.path.dirname(executable)
    inherited = _os.environ.get("PATH", "")
    _os.environ["PATH"] = directory + (_os.pathsep + inherited if inherited else "")
    if expected["prefix"] != expected["base_prefix"]:
        _os.environ["VIRTUAL_ENV"] = expected["prefix"]
    else:
        _os.environ.pop("VIRTUAL_ENV", None)


_mcp_console.configure_native_child_environment = (
    _mcp_console_configure_native_child_environment
)


class _McpConsoleModuleDefaults:
    """Apply Python-only defaults once, after a module's ordinary loader runs."""

    def __init__(
        self,
        sys,
        threading,
        finder,
        loader,
        install_show,
    ) -> None:
        self._sys = sys
        self._state = threading.local()
        self._finder = finder
        self._loader = loader
        self._install_show = install_show
        self._pending = {"numpy", "pandas", "matplotlib.pyplot"}

    def apply(self, name: str) -> None:
        module = self._sys.modules.get(name)
        if name not in self._pending or module is None:
            return
        if name == "numpy":
            # A startup hook may already have selected a display width.
            if module.get_printoptions()["linewidth"] == 75:
                module.set_printoptions(linewidth=200)
        elif name == "pandas":
            if module.get_option("display.width") == 80:
                module.set_option("display.width", 200)
        elif getattr(module.show, "__module__", None) == "matplotlib.pyplot":
            self._install_show()
        self._pending.remove(name)

    def find_spec(self, fullname: str, path=None, target=None):
        if fullname not in self._pending or getattr(self._state, "finding", False):
            return None
        self._state.finding = True
        try:
            # Reuse ordinary finder order. This lookup excludes the resolver;
            # a missing module reaches it through the original import instead.
            specification = self._finder._find_spec(
                self._sys.meta_path, fullname, path, target
            )
        finally:
            self._state.finding = False
        if specification is not None and hasattr(specification.loader, "exec_module"):
            specification.loader = self._loader(
                specification.loader, lambda: self.apply(fullname)
            )
        return specification


_mcp_console_module_defaults = _McpConsoleModuleDefaults(
    _sys,
    _threading,
    _mcp_console_import_finder,
    _McpConsoleModuleLoader,
    _mcp_console_install_matplotlib_show,
)
_sys.meta_path.insert(0, _mcp_console_module_defaults)


def _mcp_console_configure_module_defaults(
    _defaults=_mcp_console_module_defaults,
) -> None:
    for name in tuple(_defaults._pending):
        _defaults.apply(name)


_mcp_console.configure_module_defaults = _mcp_console_configure_module_defaults


def _mcp_console_conversion_metadata(
    _sys=_sys,
    _os=_os,
    _json=_json,
    _metadata=_importlib_metadata,
    _util=_importlib_util,
    _without_resolution=_mcp_console_without_automatic_resolution,
    _numpy_version=_re.compile(r"[0-9]+(?:\.[0-9]+)*(?:\.?[A-Za-z_+].*)?"),
) -> str:
    def describe() -> str:
        import struct

        numpy = None
        module = _sys.modules.get("numpy")
        if module is not None:
            # Already imported NumPy can outlive a managed path activation.
            namespace = vars(module)
            version = namespace.get("__version__")
            if (
                namespace.get("__path__")
                and isinstance(version, str)
                and _numpy_version.fullmatch(version)
            ):
                numpy = {
                    "path": _os.path.realpath(namespace["__path__"][0]),
                    "version": version,
                }
        else:
            specification = _util.find_spec("numpy")
            if specification is not None and specification.submodule_search_locations:
                try:
                    distribution = _metadata.distribution("numpy")
                    path = _os.path.realpath(distribution.locate_file("numpy"))
                    version = distribution.version
                    # A workspace module/package may shadow the installed
                    # distribution. Metadata never executes that candidate.
                    if (
                        isinstance(version, str)
                        and _numpy_version.fullmatch(version)
                        and list(
                            map(
                                _os.path.realpath,
                                specification.submodule_search_locations,
                            )
                        )
                        == [path]
                    ):
                        numpy = {"path": path, "version": version}
                except Exception:
                    # Optional distribution metadata may be stale or unreadable.
                    pass
        return _json.dumps(
            {
                "base_executable": _sys._base_executable,
                "pythonpath": _os.pathsep.join(_sys.path),
                "version": _sys.version.replace("\n", " "),
                "version_number": f"{_sys.version_info.major}.{_sys.version_info.minor}",
                "architecture": f"{struct.calcsize('P') * 8}bit",
                "conda": _os.path.isdir(_os.path.join(_sys.prefix, "conda-meta")),
                "numpy": numpy,
            }
        )

    # Metadata describes the live import environment and never prepares packages.
    return _without_resolution(describe)


_mcp_console.conversion_metadata = _mcp_console_conversion_metadata


def _configure_process_backends(executable, configure_psutil, _sys=_sys):
    # Loky also captures the executable. A Windows virtualenv redirector is
    # bypassed only when that capture matches the current interpreter; stale
    # captures duplicate semaphore handles into the redirector instead of the
    # process that executes the task after live environment activation.
    loky_spawn = _sys.modules.get("joblib.externals.loky.backend.spawn")
    if loky_spawn is not None:
        loky_spawn._python_exe = executable
    configure_psutil()


_mcp_console._configure_backends = _configure_process_backends


# The runtime runs with __main__ globals and private locals. Remember its code
# objects so cell tracebacks can omit our frames without hiding user exec() code.
_mcp_console_codes_to_record = []
for _mcp_console_value in tuple(locals().values()):
    if isinstance(_mcp_console_value, _types.FunctionType):
        _mcp_console_codes_to_record.append(_mcp_console_value.__code__)
    elif isinstance(_mcp_console_value, type):
        for _mcp_console_member in vars(_mcp_console_value).values():
            if isinstance(_mcp_console_member, _types.FunctionType):
                _mcp_console_codes_to_record.append(_mcp_console_member.__code__)
while _mcp_console_codes_to_record:
    _mcp_console_code = _mcp_console_codes_to_record.pop()
    _mcp_console_private_codes.add(id(_mcp_console_code))
    for _mcp_console_constant in _mcp_console_code.co_consts:
        if isinstance(_mcp_console_constant, _types.CodeType):
            _mcp_console_codes_to_record.append(_mcp_console_constant)
