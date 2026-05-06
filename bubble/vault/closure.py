"""Dependency closure resolution against the vault.

Given a vaulted (name, version, wheel_tag), walk the `dependencies`
table for required (non-optional, non-extra) deps and produce the
closure of vault paths. Used by substrate-routed aliases that own
their own sys.path — each isolated interpreter needs the closure on
its path because bubble's meta-finder doesn't run inside it.

Version specs: `==X.Y.Z` is honored as a strict pin. Anything else
(>=, ~=, range expressions) falls through to "highest available
version of this name" via `best_version`. That's intentionally loose:
the deeper version-spec parser belongs in a dependency resolver, not
here. If a strict pin can't be satisfied from the vault, the missing
dep is reported so the caller can decide (fetch on demand, refuse,
or carry on with what's resolvable)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..run.shell import best_version


_PIN_RE = re.compile(r"^\s*==\s*([A-Za-z0-9_.\-+!]+)\s*$")


@dataclass
class Closure:
    paths: list[Path] = field(default_factory=list)
    missing: list[tuple[str, Optional[str]]] = field(default_factory=list)
    # ordered traversal of (name, version, wheel_tag) to support callers
    # that want to do something with each resolved node (e.g. integrity
    # check, log line); paths above is the dedup'd path list.
    resolved: list[tuple[str, str, str]] = field(default_factory=list)


def resolve_closure(conn: sqlite3.Connection, name: str, version: str,
                    wheel_tag: str) -> Closure:
    """Walk the dependency closure rooted at (name, version, wheel_tag).

    The root itself is not included in the returned paths — only its
    transitive deps, in BFS order, deduplicated by (name, version,
    wheel_tag). The caller already knows the root path."""
    out = Closure()
    seen: set[tuple[str, str, str]] = {(name, version, wheel_tag)}
    queue: list[tuple[str, str, str]] = [(name, version, wheel_tag)]

    while queue:
        cur_name, cur_ver, cur_tag = queue.pop(0)
        rows = conn.execute(
            "SELECT dep_name, dep_version_spec, optional, extra "
            "FROM dependencies "
            "WHERE package = ? AND version = ? AND wheel_tag = ?",
            (cur_name, cur_ver, cur_tag),
        ).fetchall()
        for dep_name, dep_spec, optional, extra in rows:
            if optional or extra:
                continue
            pinned = _strict_pin(dep_spec) if dep_spec else None
            picked = best_version(conn, dep_name, pinned_version=pinned)
            if not picked:
                out.missing.append((dep_name, pinned))
                continue
            dep_ver, dep_tag, dep_path = picked
            key = (dep_name, dep_ver, dep_tag)
            if key in seen:
                continue
            seen.add(key)
            out.paths.append(Path(dep_path))
            out.resolved.append(key)
            queue.append(key)

    return out


def _strict_pin(spec: str) -> Optional[str]:
    """Return the pinned version if `spec` is exactly `==X`. Otherwise
    None — meaning the caller should use highest-available."""
    m = _PIN_RE.match(spec)
    return m.group(1) if m else None
