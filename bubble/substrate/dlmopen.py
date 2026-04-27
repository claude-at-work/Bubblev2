"""dlmopen-isolated substrate handler.

dlmopen(LM_ID_NEWLM, libpython.so, RTLD_NOW) opens a fresh link namespace
holding its own copy of libpython. The interpreter inside that copy is
a different process-local Python state than the calling interpreter:
its sys.modules, its GIL token, its runtime malloc arena. A package
loaded *inside* that fresh interpreter has no namespace collision with
a same-named package loaded in the caller's interpreter — the diamond
conflict dissolves at the link-namespace level rather than at the
import-graph level.

What this handler ships:
  - DlmopenInterp: a context-managed isolated interpreter.
  - is_available(): probes the kernel + libpython for support.
  - run_simple(code): executes a code string in the isolated interpreter.
  - import_and_eval(vault_path, module_name, expr): imports a vaulted
    package and evaluates an expression in its namespace, returning a
    string. The boundary-crossing primitive.
  - load_module(alias, vault_path, real_name) -> IsolatedModule:
    a types.ModuleType subclass whose __getattr__ marshals into the
    isolated interpreter. Module-level attributes (constants, classes,
    functions) are reachable. Function calls with primitive / pickle-
    able arguments work end-to-end. The result is shaped like a normal
    Python module to the calling code, so `import alias` returns
    something the rest of the program can use.

Scope: module-level attributes, function calls with picklable args and
returns. Object identity across calls (instance.method() chains where
instance was created in a prior call) requires a handle table — that's
the next stretch above this one. The proxy works for the demonstration
the README has been pointing at: two versions of the same package
serving callable surfaces in one process, kernel-isolated.

GIL discipline: the dlmopen interpreter shares the process with the
calling Python. There is one OS thread; the calling Python's GIL and
the isolated interpreter's GIL alternate cooperatively because every
call goes through PyRun_SimpleString, which acquires the isolated
interpreter's GIL for the duration of the call and releases on return.
Concurrent threading across the boundary is not yet supported (the
README's named open problem).
"""

from __future__ import annotations

import atexit
import base64
import ctypes
import os
import pickle
import sys
import sysconfig
import tempfile
import types
from pathlib import Path
from typing import Any, Optional


# dlmopen flags
_LM_ID_NEWLM = -1
_RTLD_NOW = 2


class DlmopenInterp:
    """One isolated Python interpreter per instance.

    The constructor initializes the namespace, dlopens libpython into
    it, resolves the C API symbols against the isolated handle (so they
    point at *this* interpreter, not the caller's), and runs
    Py_Initialize. The destructor calls Py_FinalizeEx and closes the
    handle.
    """

    def __init__(self) -> None:
        self._libc = ctypes.CDLL("libc.so.6")
        self._libc.dlmopen.restype = ctypes.c_void_p
        self._libc.dlmopen.argtypes = [ctypes.c_long, ctypes.c_char_p, ctypes.c_int]
        self._libc.dlsym.restype = ctypes.c_void_p
        self._libc.dlsym.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._libc.dlerror.restype = ctypes.c_char_p
        self._libc.dlclose.restype = ctypes.c_int
        self._libc.dlclose.argtypes = [ctypes.c_void_p]

        libpath = _libpython_path()
        if libpath is None:
            raise RuntimeError(
                "could not locate libpython under sysconfig LIBDIR/LDLIBRARY"
            )
        self._handle = self._libc.dlmopen(_LM_ID_NEWLM, str(libpath).encode(), _RTLD_NOW)
        if not self._handle:
            err = self._libc.dlerror()
            raise RuntimeError(
                f"dlmopen(libpython) failed: {err.decode() if err else 'unknown'}"
            )

        self._py_init = self._sym("Py_Initialize", ctypes.CFUNCTYPE(None))
        self._py_run = self._sym(
            "PyRun_SimpleString",
            ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p),
        )
        self._py_fin = self._sym(
            "Py_FinalizeEx", ctypes.CFUNCTYPE(ctypes.c_int),
        )
        if not (self._py_init and self._py_run):
            raise RuntimeError(
                "isolated libpython missing Py_Initialize / PyRun_SimpleString — "
                "embedding API not available on this build"
            )
        self._py_init()
        self._initialized = True

    def __enter__(self) -> "DlmopenInterp":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _sym(self, name: str, ftype):
        addr = self._libc.dlsym(self._handle, name.encode())
        if not addr:
            return None
        return ftype(addr)

    def run_simple(self, code: str) -> int:
        """Execute `code` in the isolated interpreter via
        PyRun_SimpleString. Returns 0 on success, -1 on uncaught
        exception. The isolated interpreter's stdout/stderr write to
        the same fds as the caller's (we don't redirect)."""
        if not self._initialized:
            raise RuntimeError("interpreter not initialized")
        return self._py_run(code.encode())

    def import_and_eval(self, vault_path: Path, module_name: str,
                        expr: str) -> Optional[str]:
        """Import `module_name` from `vault_path` in the isolated
        interpreter, evaluate `expr` in that namespace, write str(value)
        to a tempfile, return the read-back value as a string."""
        with tempfile.NamedTemporaryFile(
            mode="r", suffix=".out", delete=False,
        ) as tf:
            tf_path = tf.name
        try:
            code = (
                f"import sys\n"
                f"sys.path.insert(0, {str(vault_path)!r})\n"
                f"import {module_name} as __m\n"
                f"__v = ({expr})\n"
                f"with open({tf_path!r}, 'w') as __out:\n"
                f"    __out.write(str(__v))\n"
            )
            rc = self._py_run(code.encode())
            if rc != 0:
                return None
            with open(tf_path) as out:
                return out.read()
        finally:
            try:
                os.unlink(tf_path)
            except OSError:
                pass

    # ─────────────────── pickle-marshalled call channel ────────────────────

    def install_module(self, vault_path: Path, real_module_name: str) -> None:
        """Make sure `real_module_name` is imported in the isolated
        interpreter with `vault_path` on its sys.path. Idempotent."""
        code = (
            f"import sys\n"
            f"_p = {str(vault_path)!r}\n"
            f"if _p not in sys.path:\n"
            f"    sys.path.insert(0, _p)\n"
            f"import {real_module_name}\n"
            f"globals()['__bubble_target_{real_module_name}'] = {real_module_name}\n"
        )
        rc = self._py_run(code.encode())
        if rc != 0:
            raise RuntimeError(
                f"failed to install module {real_module_name!r} from "
                f"{vault_path} into isolated interpreter"
            )

    def get_attr(self, real_module_name: str, attr_path: tuple[str, ...]) -> Any:
        """Walk attr_path on `real_module_name` in the isolated
        interpreter, pickle the result, return the unpickled value to
        the caller. Raises if any segment is missing or the attribute
        isn't picklable.

        Two-step protocol via tempfile: the isolated interpreter pickles
        to a file, the caller unpickles. This is robust across the
        namespace boundary because pickle bytes are interpreter-agnostic
        (each side uses its own pickle module producing the same wire
        format)."""
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as tf:
            tf_path = tf.name
        try:
            walk = ".".join(attr_path) if attr_path else ""
            target_expr = f"__bubble_target_{real_module_name}"
            if walk:
                target_expr = f"{target_expr}.{walk}"
            code = (
                f"import pickle\n"
                f"try:\n"
                f"    _v = {target_expr}\n"
                f"    with open({tf_path!r}, 'wb') as _f:\n"
                f"        pickle.dump(('ok', _v), _f)\n"
                f"except Exception as _e:\n"
                f"    with open({tf_path!r}, 'wb') as _f:\n"
                f"        pickle.dump(('err', type(_e).__name__, str(_e)), _f)\n"
            )
            rc = self._py_run(code.encode())
            if rc != 0:
                raise RuntimeError(
                    f"isolated interpreter failed to evaluate "
                    f"{real_module_name}.{walk}"
                )
            with open(tf_path, "rb") as f:
                packet = pickle.load(f)
            if packet[0] == "ok":
                return packet[1]
            if packet[0] == "err":
                exc_type, exc_msg = packet[1], packet[2]
                raise _IsolatedAttrError(
                    f"{exc_type} in isolated interpreter: {exc_msg}"
                )
            raise RuntimeError(f"unexpected packet shape: {packet[:1]}")
        except pickle.UnpicklingError as exc:
            # Object exists but isn't picklable across the boundary —
            # this is the limit of the picklable-marshalling design.
            raise _UnpicklableAcrossBoundary(
                f"{real_module_name}.{walk} returned a value that does "
                f"not pickle across the namespace boundary: {exc}"
            )
        finally:
            try:
                os.unlink(tf_path)
            except OSError:
                pass

    def call_attr(self, real_module_name: str, attr_path: tuple[str, ...],
                  args: tuple, kwargs: dict) -> Any:
        """Invoke `real_module_name.<attr_path>(*args, **kwargs)` in the
        isolated interpreter. Args and return are pickle-marshalled."""
        with tempfile.NamedTemporaryFile(suffix=".args.pkl", delete=False) as af:
            args_path = af.name
        with tempfile.NamedTemporaryFile(suffix=".ret.pkl", delete=False) as rf:
            ret_path = rf.name
        try:
            with open(args_path, "wb") as f:
                pickle.dump((args, kwargs), f)
            walk = ".".join(attr_path) if attr_path else ""
            target = f"__bubble_target_{real_module_name}"
            if walk:
                target = f"{target}.{walk}"
            code = (
                f"import pickle\n"
                f"with open({args_path!r}, 'rb') as _af:\n"
                f"    _args, _kwargs = pickle.load(_af)\n"
                f"try:\n"
                f"    _r = {target}(*_args, **_kwargs)\n"
                f"    with open({ret_path!r}, 'wb') as _rf:\n"
                f"        pickle.dump(('ok', _r), _rf)\n"
                f"except Exception as _e:\n"
                f"    with open({ret_path!r}, 'wb') as _rf:\n"
                f"        pickle.dump(('err', type(_e).__name__, str(_e)), _rf)\n"
            )
            rc = self._py_run(code.encode())
            if rc != 0:
                raise RuntimeError(
                    f"isolated call to {real_module_name}.{walk} returned rc={rc}"
                )
            with open(ret_path, "rb") as f:
                packet = pickle.load(f)
            if packet[0] == "ok":
                return packet[1]
            if packet[0] == "err":
                raise _IsolatedAttrError(
                    f"{packet[1]} in isolated call: {packet[2]}"
                )
            raise RuntimeError(f"unexpected return packet: {packet[:1]}")
        finally:
            for p in (args_path, ret_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def close(self) -> None:
        if not self._initialized:
            return
        if self._py_fin:
            try:
                self._py_fin()
            except Exception:
                pass
        if self._handle:
            self._libc.dlclose(self._handle)
            self._handle = 0
        self._initialized = False


class _IsolatedAttrError(AttributeError):
    """Raised when an isolated-interpreter attribute access fails. Subclass
    of AttributeError so importlib's normal error paths handle it."""


class _UnpicklableAcrossBoundary(TypeError):
    """The attribute exists in the isolated interpreter but its value
    cannot be marshalled across the namespace boundary. Today this is a
    hard refusal — the next stretch (handle table) addresses it."""


# ─────────────────── proxy module — caller-facing surface ────────────────────


class IsolatedModule(types.ModuleType):
    """A module-shaped object whose attribute access marshals to an
    isolated interpreter. Looks like an ordinary Python module to the
    rest of the program; under the hood, every getattr is a round-trip.

    Per-instance state stored via __dict__ slots prefixed with `_bubble_`
    so user attribute names can't collide. We don't override __setattr__
    — caller-side mutation lands on the proxy and never reaches the
    isolated module, which is the right semantics: the proxy is read-
    mostly, and callers who want to feed values in do so through call
    arguments, not by overwriting attributes.
    """

    def __init__(self, alias_name: str, interp: DlmopenInterp,
                 vault_path: Path, real_module_name: str) -> None:
        super().__init__(alias_name)
        self.__dict__["_bubble_interp"] = interp
        self.__dict__["_bubble_vault_path"] = vault_path
        self.__dict__["_bubble_real"] = real_module_name
        self.__dict__["_bubble_path"] = ()
        self.__dict__["__file__"] = (
            f"<dlmopen-isolated:{real_module_name}@{vault_path}>"
        )

    def __getattr__(self, attr: str):
        # Dunders the import machinery owns — let those raise so
        # types.ModuleType's normal handling takes over. Everything
        # else (including user-facing dunders like __version__,
        # __all__, __author__) goes through the marshalling channel.
        if attr in _MODULE_INTERNAL_DUNDERS:
            raise AttributeError(attr)
        interp = self.__dict__["_bubble_interp"]
        real = self.__dict__["_bubble_real"]
        path = self.__dict__["_bubble_path"] + (attr,)
        try:
            value = interp.get_attr(real, path)
        except _UnpicklableAcrossBoundary:
            return _IsolatedRef(self, path)
        return value


# Module attributes Python's machinery manages directly. We don't
# intercept these — let ModuleType resolve them through __dict__.
_MODULE_INTERNAL_DUNDERS = frozenset({
    "__name__", "__doc__", "__package__", "__loader__", "__spec__",
    "__file__", "__path__", "__dict__", "__class__", "__builtins__",
    "__cached__", "__getattr__", "__init__", "__new__", "__repr__",
    "__str__", "__hash__", "__eq__", "__ne__", "__weakref__",
    "__subclasshook__", "__init_subclass__", "__sizeof__", "__reduce__",
    "__reduce_ex__", "__dir__", "__delattr__", "__setattr__",
    "__format__", "__getattribute__",
})


class _IsolatedRef:
    """A reference to a value in the isolated namespace that didn't
    marshal cleanly. Supports __call__ (forwards into the isolated
    namespace) and __getattr__ (extends the path)."""

    def __init__(self, root: IsolatedModule, path: tuple[str, ...]) -> None:
        self._root = root
        self._path = path

    def __call__(self, *args, **kwargs):
        interp = self._root.__dict__["_bubble_interp"]
        real = self._root.__dict__["_bubble_real"]
        return interp.call_attr(real, self._path, args, kwargs)

    def __getattr__(self, attr: str):
        return _IsolatedRef(self._root, self._path + (attr,))


# ─────────────────── per-alias interpreter registry ────────────────────


_INTERP_REGISTRY: dict[str, DlmopenInterp] = {}


def load_module(alias: str, vault_path: Path,
                real_module_name: str) -> IsolatedModule:
    """Get or create an isolated interpreter for `alias`, install
    `real_module_name` into it from `vault_path`, return the proxy
    module the caller can use as if it were the real thing.

    Per-alias interpreter: each alias gets its own dlmopen namespace,
    so two aliases backed by the same dist (e.g. `numpy_old` and
    `numpy_legacy` both pinned to numpy 1.26) get separate isolation
    rings if both declare substrate=dlmopen_isolated.
    """
    interp = _INTERP_REGISTRY.get(alias)
    if interp is None:
        interp = DlmopenInterp()
        _INTERP_REGISTRY[alias] = interp
    interp.install_module(vault_path, real_module_name)
    return IsolatedModule(alias, interp, vault_path, real_module_name)


def _shutdown_registry() -> None:
    """atexit: cleanly tear down every isolated interpreter."""
    for alias, interp in list(_INTERP_REGISTRY.items()):
        try:
            interp.close()
        except Exception:
            pass
    _INTERP_REGISTRY.clear()


atexit.register(_shutdown_registry)


# ─────────────────── module-level capability surface ────────────────────


_AVAIL_CACHE: Optional[bool] = None
_AVAIL_REASON: str = ""


def is_available() -> bool:
    """True if dlmopen + libpython embedding work on this host. Cached
    so repeat queries don't spin up a real interpreter every time."""
    global _AVAIL_CACHE, _AVAIL_REASON
    if _AVAIL_CACHE is not None:
        return _AVAIL_CACHE

    try:
        libc = ctypes.CDLL("libc.so.6")
    except OSError as exc:
        _AVAIL_REASON = f"libc not loadable: {exc}"
        _AVAIL_CACHE = False
        return False
    if not hasattr(libc, "dlmopen"):
        _AVAIL_REASON = "dlmopen symbol absent (musl or non-glibc?)"
        _AVAIL_CACHE = False
        return False

    libpath = _libpython_path()
    if libpath is None:
        _AVAIL_REASON = "libpython not found via sysconfig LIBDIR/LDLIBRARY"
        _AVAIL_CACHE = False
        return False

    libc.dlmopen.restype = ctypes.c_void_p
    libc.dlmopen.argtypes = [ctypes.c_long, ctypes.c_char_p, ctypes.c_int]
    libc.dlsym.restype = ctypes.c_void_p
    libc.dlsym.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    libc.dlclose.argtypes = [ctypes.c_void_p]

    h = libc.dlmopen(_LM_ID_NEWLM, str(libpath).encode(), _RTLD_NOW)
    if not h:
        _AVAIL_REASON = "dlmopen(libpython) returned NULL"
        _AVAIL_CACHE = False
        return False

    py_init_addr = libc.dlsym(h, b"Py_Initialize")
    if not py_init_addr:
        _AVAIL_REASON = "isolated libpython lacks Py_Initialize symbol"
        libc.dlclose(h)
        _AVAIL_CACHE = False
        return False

    libc.dlclose(h)
    _AVAIL_REASON = "dlmopen + libpython embedding ready; proxy layer not yet built"
    _AVAIL_CACHE = True
    return True


def full_routing_implemented() -> bool:
    """True if alias resolution can route to dlmopen_isolated and return
    a usable module to the caller. True when is_available() — the proxy
    module bridge is implemented for picklable module-level attributes
    and primitive function calls. Object-identity-across-calls (the
    handle table) is the next stretch, not blocking this."""
    return is_available()


def status() -> str:
    """Human-readable status for inclusion in routing reasons."""
    if not is_available():
        return f"unavailable: {_AVAIL_REASON or 'unknown'}"
    return (
        "namespace + interpreter init verified; "
        "proxy module bridge online (picklable attrs + primitive calls); "
        "object-identity-across-calls not yet plumbed"
    )


def _libpython_path() -> Optional[Path]:
    libdir = sysconfig.get_config_var("LIBDIR")
    libname = sysconfig.get_config_var("LDLIBRARY")
    if not (libdir and libname):
        return None
    for suffix in ("", ".1", ".1.0"):
        p = Path(libdir) / (libname + suffix)
        if p.exists():
            return p
    return None
