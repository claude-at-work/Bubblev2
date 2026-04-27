"""subprocess-isolated substrate handler.

A second Python interpreter lives in a child process with its own
sys.modules, its own GIL, its own malloc arena, and its own everything
that an OS process gets. A package loaded inside that child has no
namespace collision with a same-named package loaded in the parent —
the diamond conflict dissolves at the OS-process level rather than at
the link-namespace level.

What this handler ships:
  - SubprocessInterp: a context-managed isolated interpreter living in
    a child process, with a length-prefixed pickle channel over stdin/
    stdout. Per-interp state in `modules` (real_name → loaded module)
    so a single child can host multiple distributions if asked.
  - is_available(): probes whether `sys.executable` can be re-spawned
    with -I (isolated mode). True almost everywhere Python is here.
  - install_module(vault_path, real_name) — make sure `real_name` is
    imported in the child with `vault_path` on its sys.path.
  - get_attr / call_attr — the marshalling primitives.
  - load_module(alias, vault_path, real_name) -> IsolatedModule:
    a types.ModuleType subclass whose attribute access marshals into
    the child interpreter. Module-level attributes (constants, classes,
    functions) are reachable. Function calls with picklable args and
    returns work end-to-end. The result looks like a normal Python
    module to the calling code, so `import alias` returns something the
    rest of the program can use.

Where this sits relative to the dlmopen handler:
  - dlmopen isolates inside one OS process via link namespaces (~5MB
    overhead, glibc-only, no concurrent threading across the boundary
    yet)
  - subprocess isolates across OS processes (~30MB overhead, portable
    everywhere Python runs, full thread/signal isolation by construction)

The structural property the substrate handler preserves: bubble does
not run installation scripts on the parent side, and the child
interpreter only imports already-vaulted bytes. The substrate is for
*runtime isolation between aliases*, not for *sandboxing untrusted
build code* — that latter use exists as a future move that builds on
this primitive but is not what this handler is.

Boundary discipline shared with dlmopen:
  - picklable args/returns only
  - module-level attribute access via path walk
  - object identity across calls is not yet plumbed (the handle table
    is a separate stretch — calling instance.method() repeatedly where
    instance was created in a prior call needs identity tracking on
    both sides; today every call ends with the value being pickled
    back, so identity does not survive the boundary)
"""

from __future__ import annotations

import atexit
import os
import pickle
import struct
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Optional


# Worker program. Runs in the child process. Reads length-prefixed
# pickle frames from stdin, writes length-prefixed pickle responses to
# stdout. Kept short — the marshalling discipline lives on the parent
# side, the child is a thin import-and-eval loop.
_WORKER_SOURCE = r"""
import importlib, pickle, struct, sys, traceback

_modules = {}

def _read_frame():
    raw = sys.stdin.buffer.read(4)
    if not raw or len(raw) < 4:
        return None
    n = struct.unpack(">I", raw)[0]
    data = b""
    while len(data) < n:
        chunk = sys.stdin.buffer.read(n - len(data))
        if not chunk:
            return None
        data += chunk
    return pickle.loads(data)

def _write_frame(obj):
    try:
        data = pickle.dumps(obj)
    except Exception as exc:
        # The value couldn't pickle; report the kind back instead.
        data = pickle.dumps({
            "ok": False, "kind": "unpicklable",
            "error": f"{type(exc).__name__}: {exc}",
        })
    sys.stdout.buffer.write(struct.pack(">I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()

def _walk(obj, path):
    for seg in path:
        obj = getattr(obj, seg)
    return obj

while True:
    msg = _read_frame()
    if msg is None:
        break
    op = msg.get("op")
    try:
        if op == "install":
            vault_path = msg["vault_path"]
            real = msg["real_name"]
            if vault_path not in sys.path:
                sys.path.insert(0, vault_path)
            _modules[real] = importlib.import_module(real)
            _write_frame({"ok": True})
        elif op == "get":
            real = msg["real_name"]
            path = msg["path"]
            mod = _modules.get(real)
            if mod is None:
                _write_frame({
                    "ok": False, "kind": "missing",
                    "error": f"module {real!r} not installed in worker",
                })
                continue
            try:
                val = _walk(mod, path)
            except AttributeError as exc:
                _write_frame({
                    "ok": False, "kind": "attribute",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
            _write_frame({"ok": True, "value": val})
        elif op == "call":
            real = msg["real_name"]
            path = msg["path"]
            args = msg["args"]
            kwargs = msg["kwargs"]
            mod = _modules.get(real)
            if mod is None:
                _write_frame({
                    "ok": False, "kind": "missing",
                    "error": f"module {real!r} not installed in worker",
                })
                continue
            try:
                fn = _walk(mod, path)
                result = fn(*args, **kwargs)
            except Exception as exc:
                _write_frame({
                    "ok": False, "kind": "exception",
                    "type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                })
                continue
            _write_frame({"ok": True, "value": result})
        elif op == "shutdown":
            _write_frame({"ok": True})
            break
        else:
            _write_frame({
                "ok": False, "kind": "unknown_op",
                "error": f"unknown op {op!r}",
            })
    except Exception as exc:
        _write_frame({
            "ok": False, "kind": "worker_error",
            "type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        })
"""


class SubprocessInterp:
    """One isolated Python interpreter per instance, hosted in a child
    process. Communication is length-prefixed pickle over stdin/stdout."""

    def __init__(self) -> None:
        # -I = isolated mode: ignore PYTHON* env, no user site, no implicit
        # cwd on sys.path. The child starts as clean as we can make it.
        env = dict(os.environ)
        env.setdefault("PYTHONNOUSERSITE", "1")
        env.setdefault("PYTHONSAFEPATH", "1")
        self._proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _WORKER_SOURCE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            close_fds=True,
        )
        self._closed = False

    def __enter__(self) -> "SubprocessInterp":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _send(self, msg: dict) -> dict:
        if self._closed:
            raise RuntimeError("interpreter already closed")
        data = pickle.dumps(msg)
        try:
            self._proc.stdin.write(struct.pack(">I", len(data)))
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._closed = True
            raise RuntimeError(f"subprocess interp pipe broken: {exc}")
        raw = self._proc.stdout.read(4)
        if not raw or len(raw) < 4:
            stderr = self._proc.stderr.read().decode("utf-8", errors="replace")
            self._closed = True
            raise RuntimeError(
                f"subprocess interp closed unexpectedly; stderr:\n{stderr}"
            )
        n = struct.unpack(">I", raw)[0]
        body = b""
        while len(body) < n:
            chunk = self._proc.stdout.read(n - len(body))
            if not chunk:
                stderr = self._proc.stderr.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"subprocess interp short read; stderr:\n{stderr}"
                )
            body += chunk
        return pickle.loads(body)

    def install_module(self, vault_path: Path, real_module_name: str) -> None:
        """Make sure `real_module_name` is imported in the child with
        `vault_path` on its sys.path. Idempotent."""
        resp = self._send({
            "op": "install",
            "vault_path": str(vault_path),
            "real_name": real_module_name,
        })
        if not resp.get("ok"):
            raise RuntimeError(
                f"failed to install {real_module_name!r} from {vault_path} "
                f"into subprocess interp: "
                f"{resp.get('error', 'unknown error')}"
            )

    def get_attr(self, real_module_name: str, attr_path: tuple[str, ...]) -> Any:
        """Walk `attr_path` on `real_module_name` in the child interpreter,
        pickle the result, return the unpickled value to the caller."""
        resp = self._send({
            "op": "get",
            "real_name": real_module_name,
            "path": list(attr_path),
        })
        if resp.get("ok"):
            return resp["value"]
        kind = resp.get("kind")
        err = resp.get("error", "unknown")
        if kind == "attribute":
            raise _IsolatedAttrError(err)
        if kind == "unpicklable":
            raise _UnpicklableAcrossBoundary(err)
        raise RuntimeError(f"subprocess get failed ({kind}): {err}")

    def call_attr(self, real_module_name: str, attr_path: tuple[str, ...],
                  args: tuple, kwargs: dict) -> Any:
        """Invoke `real_module_name.<attr_path>(*args, **kwargs)` in the
        child. Args and return are pickle-marshalled."""
        resp = self._send({
            "op": "call",
            "real_name": real_module_name,
            "path": list(attr_path),
            "args": tuple(args),
            "kwargs": dict(kwargs),
        })
        if resp.get("ok"):
            return resp["value"]
        kind = resp.get("kind")
        err = resp.get("error", "unknown")
        if kind == "exception":
            raise _IsolatedAttrError(
                f"{resp.get('type', 'Exception')} in subprocess call: {err}"
            )
        if kind == "unpicklable":
            raise _UnpicklableAcrossBoundary(err)
        raise RuntimeError(f"subprocess call failed ({kind}): {err}")

    def close(self) -> None:
        if self._closed:
            return
        try:
            # Best-effort graceful shutdown.
            self._send({"op": "shutdown"})
        except Exception:
            pass
        finally:
            self._closed = True
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass


class _IsolatedAttrError(AttributeError):
    """Raised when an isolated-interpreter attribute access fails. Subclass
    of AttributeError so importlib's normal error paths handle it."""


class _UnpicklableAcrossBoundary(TypeError):
    """The attribute exists in the child interpreter but its value cannot
    be marshalled across the boundary. Hard refusal today; the handle
    table is the next stretch beyond this."""


# ─────────────────── proxy module — caller-facing surface ────────────────────


class IsolatedModule(types.ModuleType):
    """A module-shaped object whose attribute access marshals to a child
    interpreter. Looks like an ordinary Python module to the rest of
    the program; under the hood, every getattr is a round-trip.

    Mirrors bubble.substrate.dlmopen.IsolatedModule so callers see a
    consistent shape regardless of which substrate is hosting the
    alias."""

    def __init__(self, alias_name: str, interp: SubprocessInterp,
                 vault_path: Path, real_module_name: str) -> None:
        super().__init__(alias_name)
        self.__dict__["_bubble_interp"] = interp
        self.__dict__["_bubble_vault_path"] = vault_path
        self.__dict__["_bubble_real"] = real_module_name
        self.__dict__["_bubble_path"] = ()
        self.__dict__["__file__"] = (
            f"<subprocess-isolated:{real_module_name}@{vault_path}>"
        )

    def __getattr__(self, attr: str):
        if attr in _MODULE_INTERNAL_DUNDERS:
            raise AttributeError(attr)
        interp = self.__dict__["_bubble_interp"]
        real = self.__dict__["_bubble_real"]
        path = self.__dict__["_bubble_path"] + (attr,)
        try:
            return interp.get_attr(real, path)
        except _UnpicklableAcrossBoundary:
            return _IsolatedRef(self, path)


# Module attributes Python's machinery manages directly. Same set as
# the dlmopen handler; the proxy shape is shared.
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
    """A reference to a value in the child interpreter that didn't
    marshal cleanly. Supports __call__ (forwards into the child) and
    __getattr__ (extends the path)."""

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


_INTERP_REGISTRY: dict[str, SubprocessInterp] = {}


def load_module(alias: str, vault_path: Path,
                real_module_name: str) -> IsolatedModule:
    """Get or create a child interpreter for `alias`, install
    `real_module_name` into it from `vault_path`, return the proxy
    module the caller can use as if it were the real thing.

    Per-alias interpreter: each alias gets its own child process, so
    two aliases backed by the same dist (e.g. `numpy_old` and
    `numpy_legacy` both pinned to numpy 1.26) get separate isolation
    rings if both declare substrate=subprocess."""
    interp = _INTERP_REGISTRY.get(alias)
    if interp is None:
        interp = SubprocessInterp()
        _INTERP_REGISTRY[alias] = interp
    interp.install_module(vault_path, real_module_name)
    return IsolatedModule(alias, interp, vault_path, real_module_name)


def _shutdown_registry() -> None:
    """atexit: cleanly tear down every child interpreter."""
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
    """True if a child Python can be spawned. Almost always True since
    sys.executable is by definition runnable, but probe for completeness:
    a frozen / restricted host (some embedded contexts) may refuse to
    fork. Cached so repeat queries don't pay the spawn cost."""
    global _AVAIL_CACHE, _AVAIL_REASON
    if _AVAIL_CACHE is not None:
        return _AVAIL_CACHE
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", "print('ok')"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _AVAIL_REASON = f"cannot spawn child python: {exc}"
        _AVAIL_CACHE = False
        return False
    if proc.returncode != 0 or "ok" not in proc.stdout:
        _AVAIL_REASON = (
            f"child python returned rc={proc.returncode}; "
            f"stderr={proc.stderr!r}"
        )
        _AVAIL_CACHE = False
        return False
    _AVAIL_REASON = (
        "subprocess substrate ready: child python spawnable, "
        "pickle channel + proxy module bridge online (picklable attrs "
        "+ primitive calls); object-identity-across-calls not yet plumbed"
    )
    _AVAIL_CACHE = True
    return True


def full_routing_implemented() -> bool:
    """True if alias resolution can route to subprocess and return a
    usable module to the caller. True when is_available() — the proxy
    module bridge is implemented for picklable module-level attributes
    and primitive function calls. Object-identity-across-calls is the
    next stretch, not blocking this."""
    return is_available()


def status() -> str:
    """Human-readable substrate status for inclusion in routing reasons."""
    if not is_available():
        return f"unavailable: {_AVAIL_REASON or 'unknown'}"
    return _AVAIL_REASON
