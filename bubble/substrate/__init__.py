"""Substrate handlers — the runtime layer beneath route.py's decisions.

The router decides *which* substrate hosts an alias; the handlers in
this package are *how* each substrate actually serves bytes. Today
only `in_process` is exercised inside meta_finder.py directly; the
substrate package exposes the dlmopen_isolated handler as a verified
capability (the namespace and interpreter initialize on hosts that
support it) and names truthfully what is still missing for end-to-end
routing — the cross-namespace module proxy.

The handlers are queried by route.py through `is_implemented(name)`,
which reads from this package's `_HANDLERS` registry. Adding a
substrate is: write a handler module, expose `is_available()` and
`status()` from it, and register here.
"""

from __future__ import annotations

from typing import Optional


def is_implemented(substrate: str) -> bool:
    """True if the substrate has any working code path on this host.

    `in_process` is always implemented (it's the default route).
    `dlmopen_isolated` is implemented in the sense that a fresh
    interpreter can be initialized in an isolated namespace; whether
    that interpreter can serve a module to the calling interpreter is
    a separate question (the proxy layer, not yet shipped). Routes to
    dlmopen_isolated still downgrade today, but the status string
    names exactly what is and isn't ready.
    """
    if substrate == "in_process":
        return True
    handler = _HANDLERS.get(substrate)
    if handler is None:
        return False
    return handler.full_routing_implemented()


def status(substrate: str) -> str:
    """Human-readable substrate status for inclusion in route reasons.

    Used by route.py to compose the `reason` field of Decision objects
    so host.toml's recorded downgrades carry actionable detail."""
    if substrate == "in_process":
        return "ready"
    handler = _HANDLERS.get(substrate)
    if handler is None:
        return f"no handler module registered for {substrate!r}"
    return handler.status()


# Lazy-load the heavy handlers (each may probe ctypes / dlopen on import).
_HANDLERS: dict = {}


def _register_dlmopen() -> None:
    try:
        from . import dlmopen as _dlmopen_mod
    except Exception as exc:
        return  # handler module itself failed to import; skip silently
    _HANDLERS["dlmopen_isolated"] = _dlmopen_mod


_register_dlmopen()
