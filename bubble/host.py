"""host — read the self-portrait, record observations back to it.

The probe writes ~/.bubble/host.toml. This module *reads* it, so the rest of
bubble can consult what was learned. And it provides an append-observation
path so the runtime can write back what it discovers — closing the loop.

Keep zero-dep: tomllib is in stdlib (3.11+); but we read with the same
hand-parser we wrote for shell manifests, since bubble's other manifest
formats are also hand-parsed and the format is small.

# the register

Every failure recorded here speaks in one shape — the same shape the docs
already establish: *what happened, why it happened, what would help, what
flips this if you accept the trust boundary.* Callers reach for
`record_failure(kind, target, detail)` with `kind` drawn from
`FAILURE_KINDS` (the vocabulary below). New kinds extend the vocabulary
through `register_kind()`; ad-hoc strings are tolerated for back-compat
but get a deprecation note on stderr. The vocabulary is the loom every
other organ weaves through.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import config


# ─────────────────────── failure-kind vocabulary ───────────────────────
#
# This is the warp every other organ joins. Each entry is a tag the
# rest of bubble uses when something doesn't go through the membrane.
# A new kind registers here once, then any caller can use it without
# inventing a string. The names are short enough for tooling, specific
# enough to act on.

FAILURE_KINDS: set[str] = {
    # ─ network / index (fetcher, meta_finder._fault_to_pypi)
    "pypi_fetch_failed",
    "pypi_no_compatible_release",
    "pypi_index_refused",          # off-host, http://, name swap

    # ─ vault integrity (store.verify; wired by C1 second half)
    "vault_drift_modified",        # on-disk file's hash differs from vault_files
    "vault_drift_missing",         # vault_files row exists, file gone
    "vault_drift_extra",           # file present, no vault_files row
    "vault_drift_size_mismatch",   # fast-path stat caught a size change

    # ─ shell ops (shell.add)
    "shell_pkg_missing",           # spec couldn't resolve in vault
    "shell_version_conflict",      # shell already pins a different version

    # ─ runtime (runner.run, meta_finder)
    "wheel_load_segfault",
    "abi_mismatch",
    "import_after_link_failed",

    # ─ substrate (C5 second half)
    "substrate_unavailable",
    "substrate_downgraded",
    "sub_interp_reject",
    "dlmopen_unavailable",
}


def register_kind(kind: str) -> None:
    """Extend the vocabulary at runtime. Used by organs that want a kind
    that isn't yet in the seed set. Idempotent."""
    FAILURE_KINDS.add(kind)


def is_known_kind(kind: str) -> bool:
    return kind in FAILURE_KINDS


def host_toml_path() -> Path:
    return config.BUBBLE_HOME / "host.toml"


# ─────────────────────── reading the portrait ──────────────────────────


def load() -> dict:
    """Parse host.toml into a dict. Returns {} if the file doesn't exist —
    callers should treat absence as 'haven't probed yet' and fall back to
    safe defaults."""
    path = host_toml_path()
    if not path.exists():
        return {}
    return _parse_toml(path.read_text())


def substrates() -> list[dict]:
    """The substrate menu derived at probe time. Each entry has:
        name, cost_mb, applies_to, status[, detail]
    """
    return load().get("substrates", [])


def has_substrate(name: str) -> bool:
    """Quick check: is this substrate marked available on this machine?"""
    for s in substrates():
        if s.get("name") == name:
            return str(s.get("status", "")).startswith("available")
    return False


def known_failures() -> list[dict]:
    """Failures observed at runtime and recorded back. Each entry:
        recorded_at, kind, target, detail
    """
    return load().get("failures", [])


def is_known_failure(kind: str, target: str) -> Optional[dict]:
    """Has this exact (kind, target) failure been recorded before?"""
    for f in known_failures():
        if f.get("kind") == kind and f.get("target") == target:
            return f
    return None


# ─────────────────────── writing observations back ─────────────────────


def record_failure(kind: str, target: str, detail: str = "") -> None:
    """Append a failure observation to host.toml's [[failures]] list.

    'kind' should be a member of FAILURE_KINDS — the vocabulary at the top
    of this module names every shape the rest of bubble produces. New
    kinds register through `register_kind()`. An unknown kind is still
    recorded (we'd rather have a noisy log than a silent loss) but emits
    a one-line note on stderr so the caller can lift the string into the
    vocabulary or pick an existing entry.

    'target' identifies what failed (package name, wheel tag, etc).
    'detail' is the raw error or other diagnostic context.
    """
    if kind not in FAILURE_KINDS and not os.environ.get("BUBBLE_QUIET"):
        sys.stderr.write(
            f"[bubble] note: record_failure({kind!r}, ...) — kind not in "
            f"FAILURE_KINDS; consider host.register_kind({kind!r}) or "
            f"choose an existing kind from host.py\n"
        )
    _append_observation("failures", {
        "recorded_at": datetime.now().isoformat(),
        "kind": kind,
        "target": target,
        "detail": detail,
    })


def record_observation(section: str, fact: dict) -> None:
    """Generic — append to any [[section]] in host.toml.

    Keeps the rest of host.toml intact (rebuilt from current parse + new entry).
    """
    _append_observation(section, fact)


# ─────────────────────── implementation ────────────────────────────────


def _append_observation(section: str, fact: dict) -> None:
    """Read host.toml, add the entry to the named [[section]] list, write back."""
    path = host_toml_path()
    if not path.exists():
        # No portrait yet — don't auto-probe; just create a thin stub so the
        # observation isn't lost. The user can run `bubble probe` to enrich.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_emit_stub() + _emit_array_table(section, fact))
        return
    current = path.read_text()
    addition = _emit_array_table(section, fact)
    path.write_text(current.rstrip() + "\n\n" + addition)


def _emit_stub() -> str:
    return (
        "# bubble host portrait — observations only (probe not yet run)\n"
        f'observations_started_at = "{datetime.now().isoformat()}"\n\n'
    )


def _emit_array_table(section: str, fact: dict) -> str:
    lines = [f"[[{section}]]"]
    for k, v in fact.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, int):
            lines.append(f"{k} = {v}")
        elif v is None:
            continue
        else:
            s = str(v).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{k} = "{s}"')
    return "\n".join(lines) + "\n"


# ─────────────────────── tiny TOML parser ──────────────────────────────


_KV_RE     = re.compile(r'^([A-Za-z_][\w]*)\s*=\s*(.+)$')
_TABLE_RE  = re.compile(r'^\[([A-Za-z_][\w]*)\]\s*$')
_ARRAY_RE  = re.compile(r'^\[\[([A-Za-z_][\w]*)\]\]\s*$')


def _parse_toml(text: str) -> dict:
    """Minimal TOML parser that handles what we emit:
       top-level scalars, [section] tables, [[section]] array-tables.
       String/int/bool values; quoted strings only.
    """
    out: dict = {}
    current_table: Optional[dict] = out
    current_array_name: Optional[str] = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        m = _ARRAY_RE.match(line)
        if m:
            name = m.group(1)
            new_entry: dict = {}
            out.setdefault(name, []).append(new_entry)
            current_table = new_entry
            current_array_name = name
            continue

        m = _TABLE_RE.match(line)
        if m:
            name = m.group(1)
            current_table = out.setdefault(name, {})
            current_array_name = None
            continue

        m = _KV_RE.match(line)
        if m and current_table is not None:
            key, raw_val = m.group(1), m.group(2).strip()
            current_table[key] = _parse_value(raw_val)

    return out


def _parse_value(s: str) -> Any:
    s = s.strip()
    if s in ("true", "false"):
        return s == "true"
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if s.startswith("[") and s.endswith("]"):
        # Array of strings/ints/bools — minimal handling
        inner = s[1:-1].strip()
        if not inner:
            return []
        items = []
        for part in re.split(r',\s*(?=(?:[^"]*"[^"]*")*[^"]*$)', inner):
            items.append(_parse_value(part.strip()))
        return items
    try:
        return int(s)
    except ValueError:
        return s
